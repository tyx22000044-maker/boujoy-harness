from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, Optional

from .errors import BokError
from .util import atomic_write_bytes, atomic_write_json, compact_timestamp, sha256_bytes, utc_now


@dataclass(frozen=True)
class RestoreChange:
    relative: str
    before: Optional[bytes]
    after: Optional[bytes]


class TransactionalMarkdownRestore:
    """Crash-repairable Markdown restore with automatic rollback on failure."""

    def __init__(
        self,
        *,
        root: Path,
        state_dir: Path,
        lock,
        resolve_target: Callable[[str], Path],
        current_files: Callable[[], Iterable[Path]],
        relative_path: Callable[[Path], str],
        file_mode: int,
        namespace: str,
    ) -> None:
        self.root = Path(root)
        self.state_dir = Path(state_dir)
        self.lock = lock
        self.resolve_target = resolve_target
        self.current_files = current_files
        self.relative_path = relative_path
        self.file_mode = file_mode
        self.namespace = namespace
        self.transactions_dir = self.state_dir / "restore-transactions"

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        return tuple(PurePosixPath(relative).parts)

    def _transaction_dir(self, transaction_id: str) -> Path:
        return self.transactions_dir / transaction_id

    def _snapshot_path(self, transaction: Path, relative: str) -> Path:
        return transaction / "before" / Path(*self._parts(relative))

    def _stage_path(self, transaction: Path, relative: str) -> Path:
        return transaction / "after" / Path(*self._parts(relative))

    def _journal(self, transaction: Path, value: dict) -> None:
        value["updated_at"] = utc_now()
        atomic_write_json(transaction / "journal.json", value)

    def _cleanup(self, transaction: Path) -> None:
        try:
            shutil.rmtree(transaction)
        except FileNotFoundError:
            pass

    def _rollback(self, transaction: Path, journal: dict) -> list[dict]:
        errors = []
        for item in journal.get("changes", []):
            relative = str(item.get("path", ""))
            try:
                target = self.resolve_target(relative)
                if item.get("before_exists"):
                    before_path = self._snapshot_path(transaction, relative)
                    before = before_path.read_bytes()
                    if sha256_bytes(before) != item.get("before_hash"):
                        raise OSError("restore snapshot hash mismatch")
                    atomic_write_bytes(target, before, mode=self.file_mode)
                elif target.exists():
                    if target.is_symlink() or not target.is_file():
                        raise OSError("unsafe rollback target")
                    target.unlink()
            except (BokError, OSError, RuntimeError, ValueError) as error:
                errors.append({"path": relative, "error": type(error).__name__})
        return errors

    def repair_pending(self) -> dict:
        repaired = 0
        failed = []
        self.transactions_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.transactions_dir, 0o700)
        except OSError:
            pass
        with self.lock:
            for transaction in sorted(self.transactions_dir.iterdir()):
                if not transaction.is_dir() or transaction.is_symlink():
                    continue
                try:
                    journal = json.loads((transaction / "journal.json").read_text(encoding="utf-8"))
                    if not isinstance(journal, dict) or journal.get("namespace") != self.namespace:
                        failed.append({"transaction_id": transaction.name, "error": "invalid_journal"})
                        continue
                    status = str(journal.get("status", ""))
                    if status == "committed" or status == "rolled_back":
                        self._cleanup(transaction)
                        continue
                    errors = self._rollback(transaction, journal)
                    if errors:
                        failed.append({"transaction_id": transaction.name, "error": "rollback_failed", "details": errors})
                        continue
                    journal["status"] = "rolled_back"
                    journal["repaired_at"] = utc_now()
                    self._journal(transaction, journal)
                    self._cleanup(transaction)
                    repaired += 1
                except (OSError, ValueError, TypeError):
                    failed.append({"transaction_id": transaction.name, "error": "invalid_journal"})
        return {"repaired": repaired, "failed": failed}

    def restore(
        self,
        desired: Dict[str, bytes],
        *,
        mode: str = "exact",
        metadata: Optional[dict] = None,
        prepare_versions: Optional[Callable[[list[RestoreChange]], object]] = None,
        commit_versions: Optional[Callable[[object], None]] = None,
        abort_versions: Optional[Callable[[object, str], None]] = None,
    ) -> dict:
        if mode not in {"exact", "merge"}:
            raise BokError("invalid_restore_mode", "Restore mode must be exact or merge")
        with self.lock:
            current = {self.relative_path(path): path for path in self.current_files()}
            paths = set(desired)
            if mode == "exact":
                paths.update(current)
            changes = []
            for relative in sorted(paths):
                target = self.resolve_target(relative)
                before = target.read_bytes() if target.is_file() else None
                after = desired.get(relative)
                if before != after:
                    changes.append(RestoreChange(relative, before, after))
            if not changes:
                return {"mode": mode, "changed": 0, "restored": [], "removed": [], "unchanged": True}

            transaction_id = f"{self.namespace}-{compact_timestamp()}-{uuid.uuid4().hex[:8]}"
            transaction = self._transaction_dir(transaction_id)
            transaction.mkdir(parents=True, exist_ok=False)
            try:
                os.chmod(transaction, 0o700)
            except OSError:
                pass
            journal = {
                "transaction_id": transaction_id,
                "namespace": self.namespace,
                "status": "preparing",
                "mode": mode,
                "created_at": utc_now(),
                "metadata": metadata or {},
                "changes": [],
            }
            version_state = None
            try:
                for change in changes:
                    target = self.resolve_target(change.relative)
                    if target.is_symlink():
                        raise BokError("unsafe_restore_target", "Restore cannot follow symbolic links", status=403)
                    entry = {
                        "path": change.relative,
                        "before_exists": change.before is not None,
                        "before_hash": sha256_bytes(change.before) if change.before is not None else None,
                        "after_exists": change.after is not None,
                        "after_hash": sha256_bytes(change.after) if change.after is not None else None,
                    }
                    journal["changes"].append(entry)
                    if change.before is not None:
                        atomic_write_bytes(self._snapshot_path(transaction, change.relative), change.before, mode=0o600)
                    if change.after is not None:
                        atomic_write_bytes(self._stage_path(transaction, change.relative), change.after, mode=0o600)
                journal["status"] = "prepared"
                self._journal(transaction, journal)
                if prepare_versions:
                    version_state = prepare_versions(changes)
                journal["status"] = "applying"
                self._journal(transaction, journal)
                for change in changes:
                    target = self.resolve_target(change.relative)
                    if change.after is None:
                        if target.is_file():
                            target.unlink()
                    else:
                        staged = self._stage_path(transaction, change.relative).read_bytes()
                        if sha256_bytes(staged) != sha256_bytes(change.after):
                            raise OSError("staged restore content changed")
                        atomic_write_bytes(target, staged, mode=self.file_mode)
                if commit_versions:
                    commit_versions(version_state)
                journal["status"] = "committed"
                journal["committed_at"] = utc_now()
                self._journal(transaction, journal)
            except Exception as error:
                journal["status"] = "rolling_back"
                journal["last_error"] = type(error).__name__
                try:
                    self._journal(transaction, journal)
                except OSError:
                    pass
                rollback_errors = self._rollback(transaction, journal)
                if abort_versions:
                    abort_versions(version_state, type(error).__name__)
                if rollback_errors:
                    raise BokError(
                        "restore_rollback_failed",
                        "Restore failed and automatic rollback was incomplete; use the safety backup",
                        status=500,
                        details={"transaction_id": transaction_id, "rollback_errors": rollback_errors},
                    ) from error
                journal["status"] = "rolled_back"
                journal["rolled_back_at"] = utc_now()
                try:
                    self._journal(transaction, journal)
                finally:
                    self._cleanup(transaction)
                if isinstance(error, BokError):
                    raise
                raise BokError(
                    "restore_failed",
                    "Restore failed; all changed Markdown was rolled back automatically",
                    status=500,
                    details={"transaction_id": transaction_id},
                ) from error
            self._cleanup(transaction)
            return {
                "transaction_id": transaction_id,
                "mode": mode,
                "changed": len(changes),
                "restored": [change.relative for change in changes if change.after is not None],
                "removed": [change.relative for change in changes if change.after is None],
                "unchanged": False,
            }
