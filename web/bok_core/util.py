from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class _SharedFileLockState:
    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.handle = None


_FILE_LOCK_STATES: dict[str, _SharedFileLockState] = {}
_FILE_LOCK_STATES_GUARD = threading.Lock()


class InterProcessFileLock:
    """Dependency-free, re-entrant lock shared by threads and local processes.

    A process-local registry makes separate Bok components that point at the same
    lock file share one re-entrant state. The one-byte OS lock then serializes the
    same critical section across UI, MCP and standalone API processes.
    """

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = max(0.1, float(timeout))
        key = os.path.normcase(os.path.abspath(os.fspath(self.path)))
        with _FILE_LOCK_STATES_GUARD:
            self._state = _FILE_LOCK_STATES.setdefault(key, _SharedFileLockState())

    def _open_lock_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise PermissionError(f"Lock file cannot be a symbolic link: {self.path}")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(self.path), flags, 0o600)
        try:
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        except Exception:
            os.close(descriptor)
            raise
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return handle

    @staticmethod
    def _try_os_lock(handle) -> bool:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    @staticmethod
    def _unlock_os(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> "InterProcessFileLock":
        started = time.monotonic()
        if not self._state.thread_lock.acquire(timeout=self.timeout):
            raise TimeoutError(f"Timed out waiting for local lock: {self.path}")
        try:
            if self._state.depth == 0:
                handle = self._open_lock_file()
                while not self._try_os_lock(handle):
                    if time.monotonic() - started >= self.timeout:
                        handle.close()
                        raise TimeoutError(f"Timed out waiting for process lock: {self.path}")
                    time.sleep(0.025)
                self._state.handle = handle
            self._state.depth += 1
            return self
        except Exception:
            self._state.thread_lock.release()
            raise

    def release(self) -> None:
        if self._state.depth <= 0:
            raise RuntimeError("Cannot release an unlocked InterProcessFileLock")
        try:
            self._state.depth -= 1
            if self._state.depth == 0:
                handle = self._state.handle
                self._state.handle = None
                if handle is not None:
                    try:
                        self._unlock_os(handle)
                    finally:
                        handle.close()
        finally:
            self._state.thread_lock.release()

    def __enter__(self) -> "InterProcessFileLock":
        return self.acquire()

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.release()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for mixed Chinese and Latin text."""
    if not text:
        return 0
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    remaining = max(0, len(text) - chinese)
    return chinese + (remaining + 3) // 4


def truncate_to_token_budget(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def slugify(value: str, fallback: str = "memory") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\u3400-\u9fff-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-_")
    return (value[:80] or fallback).strip("-")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError, TypeError):
        return default


def append_jsonl(path: Path, value, *, max_bytes: int = 8 * 1024 * 1024, rotations: int = 3) -> None:
    """Append one durable JSONL record and keep bounded local history segments."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        current_size = 0
    if current_size and current_size + len(line) > max(1024, int(max_bytes)):
        for index in range(max(1, int(rotations)), 0, -1):
            source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
            destination = path.with_name(f"{path.name}.{index}")
            if source.exists():
                os.replace(str(source), str(destination))
    with path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def tail_text_lines(path: Path, limit: int, *, max_scan_bytes: int = 16 * 1024 * 1024) -> list[str]:
    """Read only the end of a text file instead of materializing its full history."""
    wanted = max(1, int(limit))
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks = []
            scanned = 0
            line_count = 0
            while position > 0 and line_count <= wanted and scanned < max_scan_bytes:
                size = min(64 * 1024, position, max_scan_bytes - scanned)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                scanned += len(chunk)
                line_count += chunk.count(b"\n")
            data = b"".join(reversed(chunks))
    except FileNotFoundError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()[-wanted:]


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(str(temporary), str(path))
        try:
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, value, *, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=mode)
