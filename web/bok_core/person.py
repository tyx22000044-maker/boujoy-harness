from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Dict

from .config import BokConfig
from .errors import BokError, ConflictError, NotFoundError, PermissionDeniedError
from .person_claim import (
    CLAIM_TYPES,
    EPISTEMIC_STATUSES,
    IMPORTANT_CLAIM_TYPES,
    PersonalClaimCodec,
    QUIET_LEARNABLE_CLAIM_TYPES,
    safe_int,
)
from .restore import TransactionalMarkdownRestore
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    compact_timestamp,
    estimate_tokens,
    InterProcessFileLock,
    append_jsonl,
    read_json,
    sha256_bytes,
    sha256_text,
    utc_now,
)


PERSONAL_DATA_DIRECTORIES = ("Claims", "Observations", "Outcomes", "Impacts", "Archive")


class PersonalClaimStore(PersonalClaimCodec):
    """Versioned Markdown claims in a physically separate Personal Core."""

    def __init__(self, config: BokConfig):
        self.config = config
        self.root = config.personal_core_path
        lock_root = self.root / ".bok" if self.root is not None else config.state_dir / "state"
        self.lock = InterProcessFileLock(lock_root / "write.lock")
        self._cache_signature = None
        self._cache_records = None

    @property
    def configured(self) -> bool:
        return self.root is not None

    def _require_root(self) -> Path:
        if self.root is None:
            raise BokError(
                "personal_core_not_configured",
                "Personal Core is not configured; choose a separate local folder before creating personal claims",
                status=503,
            )
        if self.root.is_symlink():
            raise BokError("unsafe_personal_core", "Personal Core root cannot be a symbolic link", status=403)
        return self.root

    def _assert_safe_paths(self) -> None:
        root = self._require_root()
        data_directories = tuple(root / name for name in PERSONAL_DATA_DIRECTORIES)
        for path in (
            root,
            *data_directories,
            root / ".bok",
            root / ".bok/versions",
            root / ".bok/backups",
            root / ".bok/forget-transactions",
        ):
            if path.is_symlink():
                raise BokError("unsafe_personal_core", "Personal Core directories cannot be symbolic links", status=403)
        for path in (root / "PERSONAL-CORE.md", root / ".gitignore", root / ".bok/activity.jsonl"):
            if path.is_symlink():
                raise BokError("unsafe_personal_core", "Personal Core control files cannot be symbolic links", status=403)
        for candidate in (root, *root.parents):
            if (candidate / ".git").exists():
                raise BokError("unsafe_personal_core", "Personal Core cannot be placed inside a Git repository", status=403)

    @property
    def claims_dir(self) -> Path:
        return self._require_root() / "Claims"

    @property
    def state_dir(self) -> Path:
        return self._require_root() / ".bok"

    @property
    def versions_dir(self) -> Path:
        return self.state_dir / "versions"

    @property
    def fingerprint_dir(self) -> Path:
        return self.state_dir / "cache" / "claim-fingerprints"

    @property
    def fingerprint_marker(self) -> Path:
        return self.fingerprint_dir / ".complete.json"

    @property
    def forget_transactions_dir(self) -> Path:
        return self.state_dir / "forget-transactions"

    @property
    def activity_path(self) -> Path:
        return self.state_dir / "activity.jsonl"

    def initialize(self) -> dict:
        if not self.configured:
            return {"configured": False, "ready": False}
        root = self._require_root()
        self._assert_safe_paths()
        if root.exists() and not root.is_dir():
            raise BokError("invalid_personal_core", "Personal Core path is not a directory")
        directories = (root, *(root / name for name in PERSONAL_DATA_DIRECTORIES), self.state_dir, self.versions_dir, self.state_dir / "backups", self.fingerprint_dir, self.forget_transactions_dir)
        for directory in directories:
            if directory.is_symlink():
                raise BokError("unsafe_personal_core", "Personal Core directories cannot be symbolic links")
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self._assert_safe_paths()
        marker = root / "PERSONAL-CORE.md"
        if not marker.exists():
            atomic_write_text(
                marker,
                "# Bok Personal Core\n\n"
                "This folder is a private Markdown fact source for confirmed personal claims.\n"
                "Do not place it inside a public repository or mount it as a general Agent workspace.\n",
            )
        gitignore = root / ".gitignore"
        if not gitignore.exists():
            atomic_write_text(gitignore, ".bok/\n")
        self.repair_restore_transactions()
        self.repair_forget_transactions()
        self.repair_versions()
        self.rebuild_fingerprint_index()
        self.repair_links()
        return self.health()

    def _path(self, claim_id: str) -> Path:
        return self.claims_dir / f"{self._claim_id(claim_id)}.md"

    def _read(self, claim_id: str) -> dict:
        self._assert_safe_paths()
        path = self._path(claim_id)
        if path.is_symlink():
            raise BokError("unsafe_personal_claim", "Personal claim files cannot be symbolic links", status=403)
        if not path.is_file():
            raise NotFoundError("Personal claim does not exist", details={"claim_id": claim_id})
        try:
            return self._record(path.read_text(encoding="utf-8-sig"), path=path)
        except (OSError, UnicodeError) as error:
            raise BokError("personal_claim_read_failed", "Could not read personal claim", status=500) from error

    def _activity(self, action: str, record: dict, version_id: str) -> None:
        if self.activity_path.is_symlink():
            raise BokError("unsafe_personal_core", "Personal Core activity file cannot be a symbolic link", status=403)
        append_jsonl(
            self.activity_path,
            {
                "at": utc_now(),
                "action": action,
                "claim_id": record["id"],
                "epistemic_status": record["epistemic_status"],
                "version_id": version_id,
            },
        )

    def _write(self, record: dict, *, operation: str) -> dict:
        self._assert_safe_paths()
        path = self._path(record["id"])
        if path.is_symlink():
            raise BokError("unsafe_personal_claim", "Personal claim files cannot be symbolic links", status=403)
        before = path.read_bytes() if path.is_file() else None
        old_record = None
        if before is not None:
            try:
                old_record = self._record(before.decode("utf-8-sig"), path=path)
            except (BokError, UnicodeError):
                old_record = None
        after = self._render(record).encode("utf-8")
        if before == after:
            result = self._public(record)
            result["unchanged"] = True
            result["version_id"] = ""
            return result
        version_id = f"{compact_timestamp()}-{uuid.uuid4().hex[:10]}"
        version_dir = self.versions_dir / version_id
        version_dir.mkdir(parents=True, exist_ok=False)
        try:
            os.chmod(version_dir, 0o700)
        except OSError:
            pass
        if before is not None:
            atomic_write_bytes(version_dir / "before.md", before)
        metadata = {
            "version_id": version_id,
            "claim_id": record["id"],
            "operation": operation,
            "created_at": utc_now(),
            "before_exists": before is not None,
            "before_hash": sha256_bytes(before) if before is not None else None,
            "after_hash": sha256_bytes(after),
            "status": "pending",
        }
        atomic_write_json(version_dir / "meta.json", metadata)
        atomic_write_bytes(path, after)
        self._cache_signature = None
        self._cache_records = None
        metadata["status"] = "committed"
        atomic_write_json(version_dir / "meta.json", metadata)
        try:
            self._update_fingerprint_index(record, old_record=old_record)
        except (BokError, OSError, UnicodeError, ValueError, TypeError):
            self.fingerprint_marker.unlink(missing_ok=True)
        try:
            self._activity(operation, record, version_id)
        except OSError:
            pass
        result = self._public(record)
        result["version_id"] = version_id
        result["unchanged"] = False
        return result

    def _all(self) -> list[dict]:
        self._assert_safe_paths()
        with self.lock:
            paths = sorted(self.claims_dir.glob("person-*.md"), key=lambda item: item.name)
            signature = []
            for path in paths:
                try:
                    stat = path.lstat()
                    signature.append((path.name, stat.st_mtime_ns, stat.st_size, path.is_symlink()))
                except OSError:
                    signature.append((path.name, 0, 0, True))
            signature_value = tuple(signature)
            if signature_value == self._cache_signature and self._cache_records is not None:
                return list(self._cache_records)
            records = []
            for path in paths:
                if path.is_symlink():
                    continue
                try:
                    records.append(self._record(path.read_text(encoding="utf-8-sig"), path=path))
                except (BokError, OSError, UnicodeError):
                    continue
            records.sort(key=lambda item: (item["updated"], item["id"]), reverse=True)
            self._cache_signature = signature_value
            self._cache_records = records
            return list(records)

    def repair_links(self) -> dict:
        """Finish a successor link if a process stopped between the two atomic writes."""
        if not self.configured:
            return {"repaired": 0}
        repaired = 0
        with self.lock:
            records = self._all()
            by_id = {item["id"]: item for item in records}
            successors_by_old = {}
            for candidate in records:
                if candidate["supersedes"]:
                    successors_by_old.setdefault(candidate["supersedes"], []).append(candidate)
            for old_id, successors in successors_by_old.items():
                if old_id not in by_id or len(successors) != 1:
                    continue
                successor = successors[0]
                old = by_id[old_id]
                if old["superseded_by"] == successor["id"] and old["epistemic_status"] == "superseded":
                    continue
                old["epistemic_status"] = "superseded"
                old["superseded_by"] = successor["id"]
                old["valid_to"] = successor["valid_from"]
                old["updated"] = utc_now()
                old["version"] += 1
                self._history(old, "supersede_link_repaired", successor["id"])
                self._write(old, operation="person_claim_supersede_repair")
                repaired += 1
        return {"repaired": repaired}

    def repair_versions(self) -> dict:
        """Resolve version journals left pending by an interrupted atomic write."""
        if not self.configured:
            return {"committed": 0, "aborted": 0}
        self._assert_safe_paths()
        repaired = {"committed": 0, "aborted": 0}
        for meta_path in self.versions_dir.glob("*/meta.json"):
            try:
                if meta_path.is_symlink() or meta_path.parent.is_symlink():
                    continue
                value = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("status", "committed") != "pending":
                    continue
                claim_id = self._claim_id(str(value.get("claim_id", "")))
                claim_path = self._path(claim_id)
                if claim_path.is_symlink():
                    raise BokError("unsafe_personal_claim", "Personal claim files cannot be symbolic links", status=403)
                current_hash = sha256_bytes(claim_path.read_bytes()) if claim_path.is_file() else None
                status = "committed" if current_hash == value.get("after_hash") else "aborted"
                value["status"] = status
                value["repaired_at"] = utc_now()
                atomic_write_json(meta_path, value)
                repaired[status] += 1
            except (BokError, OSError, UnicodeError, ValueError, TypeError):
                continue
        return repaired

    def repair_forget_transactions(self) -> dict:
        """Finish or roll back a claim erasure interrupted between file moves."""
        if not self.configured:
            return {"completed": 0, "rolled_back": 0, "failed": []}
        result = {"completed": 0, "rolled_back": 0, "failed": []}
        self.forget_transactions_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            for transaction in sorted(self.forget_transactions_dir.iterdir()):
                if not transaction.is_dir() or transaction.is_symlink():
                    continue
                journal_path = transaction / "journal.json"
                try:
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    if not isinstance(journal, dict) or journal.get("kind") != "personal-claim-forget":
                        raise ValueError("invalid journal")
                    if journal.get("status") == "staged":
                        shutil.rmtree(transaction)
                        result["completed"] += 1
                        continue
                    for item in reversed(journal.get("items", [])):
                        source = self._require_root() / str(item["source"])
                        staged = transaction / str(item["staged"])
                        if staged.exists() and not source.exists():
                            source.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(staged, source)
                    shutil.rmtree(transaction)
                    result["rolled_back"] += 1
                except (KeyError, OSError, ValueError, TypeError) as error:
                    result["failed"].append({"transaction_id": transaction.name, "error": type(error).__name__})
        return result

    def forget_claim_artifacts(self, claim_id: str, *, related_paths=None) -> dict:
        """Logically erase a claim, its derivations, versions and matching backups.

        Files are first moved into a private transaction. A crash either rolls the
        move back or finishes deletion during initialize(), so a half-forgotten
        claim is never silently presented as complete.
        """
        claim_id = self._claim_id(claim_id)
        with self.lock:
            record = self._read(claim_id)
            root = self._require_root()
            relative_targets = {f"Claims/{claim_id}.md"}
            for value in related_paths or []:
                candidate = PurePosixPath(str(value or ""))
                if candidate.is_absolute() or any(part in {"", ".", "..", ".bok"} for part in candidate.parts):
                    raise PermissionDeniedError("Forget target is outside Personal Core")
                relative_targets.add(candidate.as_posix())

            paths = [root.joinpath(*PurePosixPath(relative).parts) for relative in sorted(relative_targets)]
            for version_dir in self.versions_dir.iterdir():
                meta_path = version_dir / "meta.json"
                if not version_dir.is_dir() or version_dir.is_symlink() or meta_path.is_symlink():
                    continue
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError, TypeError):
                    continue
                if isinstance(meta, dict) and meta.get("claim_id") == claim_id:
                    paths.append(version_dir)

            removed_backups = []
            uninspectable_backups = []
            for backup in self.backups_dir.glob("personal-backup-*.zip"):
                if backup.is_symlink() or not backup.is_file():
                    continue
                try:
                    with zipfile.ZipFile(str(backup), "r") as archive:
                        if relative_targets.intersection(archive.namelist()):
                            paths.append(backup)
                            removed_backups.append(backup.stem)
                except (OSError, zipfile.BadZipFile):
                    uninspectable_backups.append(backup.stem)

            for other in self._all():
                if other["id"] == claim_id:
                    continue
                changed = False
                if other.get("supersedes") == claim_id:
                    other["supersedes"] = ""
                    changed = True
                if other.get("superseded_by") == claim_id:
                    other["superseded_by"] = ""
                    if other.get("epistemic_status") == "superseded":
                        other["epistemic_status"] = "expired"
                    changed = True
                if changed:
                    other["updated"] = utc_now()
                    other["version"] += 1
                    self._history(other, "forgotten_claim_link_removed", claim_id)
                    self._write(other, operation="person_claim_forget_unlink")

            transaction_id = f"forget-{compact_timestamp()}-{uuid.uuid4().hex[:8]}"
            transaction = self.forget_transactions_dir / transaction_id
            staged_root = transaction / "staged"
            staged_root.mkdir(parents=True, exist_ok=False)
            try:
                os.chmod(transaction, 0o700)
            except OSError:
                pass
            journal = {
                "kind": "personal-claim-forget",
                "transaction_id": transaction_id,
                "claim_id": claim_id,
                "status": "staging",
                "created_at": utc_now(),
                "items": [],
            }
            atomic_write_json(transaction / "journal.json", journal)
            try:
                unique_paths = sorted({path for path in paths if path.exists()}, key=lambda item: len(item.parts), reverse=True)
                for index, source in enumerate(unique_paths):
                    if source.is_symlink():
                        raise PermissionDeniedError("Forget cannot follow symbolic links")
                    source_relative = source.relative_to(root).as_posix()
                    staged_relative = f"staged/{index:06d}"
                    staged = transaction / staged_relative
                    journal["items"].append({"source": source_relative, "staged": staged_relative})
                    atomic_write_json(transaction / "journal.json", journal)
                    os.replace(source, staged)
                journal["status"] = "staged"
                journal["staged_at"] = utc_now()
                atomic_write_json(transaction / "journal.json", journal)
            except Exception:
                for item in reversed(journal["items"]):
                    source = root / item["source"]
                    staged = transaction / item["staged"]
                    if staged.exists() and not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(staged, source)
                shutil.rmtree(transaction, ignore_errors=True)
                raise
            shutil.rmtree(transaction)
            self._cache_signature = None
            self._cache_records = None
            self.fingerprint_marker.unlink(missing_ok=True)
            self.rebuild_fingerprint_index(force=True)
            try:
                self._activity("person_claim_forgotten", record, "")
            except OSError:
                pass
            return {
                "claim_id": claim_id,
                "forgotten": True,
                "removed_records": len(relative_targets),
                "removed_versions": sum(1 for path in paths if path.parent == self.versions_dir),
                "removed_backups": removed_backups,
                "uninspectable_backups": uninspectable_backups,
            }

    def _version_health(self) -> dict:
        counts = {"committed": 0, "pending": 0, "aborted": 0, "corrupt": 0}
        try:
            directories = list(self.versions_dir.iterdir())
        except OSError:
            counts["corrupt"] += 1
            return counts
        for directory in directories:
            meta_path = directory / "meta.json"
            if not directory.is_dir() or directory.is_symlink() or meta_path.is_symlink():
                counts["corrupt"] += 1
                continue
            try:
                value = json.loads(meta_path.read_text(encoding="utf-8"))
                status = value.get("status", "committed") if isinstance(value, dict) else ""
            except (OSError, UnicodeError, ValueError, TypeError):
                status = ""
            if status in {"committed", "pending", "aborted"}:
                counts[status] += 1
            else:
                counts["corrupt"] += 1
        return counts

    @staticmethod
    def _fingerprint(statement: str, claim_type: str, scope_kind: str, scope_value: str) -> str:
        return sha256_text("\n".join((statement.casefold(), claim_type, scope_kind, scope_value.casefold())))

    def _record_fingerprints(self, record: dict) -> set[str]:
        return {
            self._fingerprint(statement, record["claim_type"], record["scope_kind"], record["scope_value"])
            for statement in [record["statement"], *record.get("statement_history", [])]
            if str(statement).strip()
        }

    def _fingerprint_path(self, fingerprint: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise BokError("invalid_claim_fingerprint", "Personal claim fingerprint is invalid")
        return self.fingerprint_dir / fingerprint[:2] / f"{fingerprint}.json"

    def _claims_directory_mtime(self) -> int:
        try:
            return self.claims_dir.stat().st_mtime_ns
        except OSError:
            return 0

    def _write_fingerprint_ids(self, fingerprint: str, claim_ids: list[str]) -> None:
        path = self._fingerprint_path(fingerprint)
        values = sorted(dict.fromkeys(claim_ids))
        if values:
            atomic_write_json(path, {"fingerprint": fingerprint, "claim_ids": values})
        else:
            path.unlink(missing_ok=True)

    def _read_fingerprint_ids(self, fingerprint: str) -> list[str]:
        value = read_json(self._fingerprint_path(fingerprint), {})
        if not isinstance(value, dict) or value.get("fingerprint") != fingerprint:
            return []
        return [str(item) for item in value.get("claim_ids", []) if re.fullmatch(r"person-[0-9a-f]{32}", str(item))]

    def _fingerprint_ids(self, fingerprint: str) -> list[str]:
        marker = read_json(self.fingerprint_marker, {})
        if not isinstance(marker, dict) or marker.get("claims_dir_mtime_ns") != self._claims_directory_mtime():
            self.rebuild_fingerprint_index(force=True)
        return self._read_fingerprint_ids(fingerprint)

    def rebuild_fingerprint_index(self, *, force: bool = False) -> dict:
        if not self.configured:
            return {"rebuilt": False, "claims": 0, "fingerprints": 0}
        self.fingerprint_dir.mkdir(parents=True, exist_ok=True)
        marker = read_json(self.fingerprint_marker, {})
        directory_mtime = self._claims_directory_mtime()
        if not force and isinstance(marker, dict) and marker.get("claims_dir_mtime_ns") == directory_mtime:
            return {"rebuilt": False, "claims": int(marker.get("claims", 0)), "fingerprints": int(marker.get("fingerprints", 0))}
        with self.lock:
            for child in self.fingerprint_dir.iterdir():
                if child.name == self.fingerprint_marker.name:
                    continue
                if child.is_dir() and not child.is_symlink():
                    for path in child.glob("*.json"):
                        path.unlink(missing_ok=True)
                elif child.is_file():
                    child.unlink(missing_ok=True)
            records = self._all()
            mapping: Dict[str, list[str]] = {}
            for record in records:
                for fingerprint in self._record_fingerprints(record):
                    mapping.setdefault(fingerprint, []).append(record["id"])
            for fingerprint, claim_ids in mapping.items():
                self._write_fingerprint_ids(fingerprint, claim_ids)
            atomic_write_json(self.fingerprint_marker, {
                "version": 1,
                "claims_dir_mtime_ns": self._claims_directory_mtime(),
                "claims": len(records),
                "fingerprints": len(mapping),
                "rebuilt_at": utc_now(),
            })
            return {"rebuilt": True, "claims": len(records), "fingerprints": len(mapping)}

    def _update_fingerprint_index(self, record: dict, *, old_record: dict | None = None) -> None:
        marker = read_json(self.fingerprint_marker, {})
        if not isinstance(marker, dict) or not marker:
            self.rebuild_fingerprint_index(force=True)
            return
        old_fingerprints = self._record_fingerprints(old_record) if old_record else set()
        new_fingerprints = self._record_fingerprints(record)
        fingerprint_count = int(marker.get("fingerprints", 0))
        for fingerprint in old_fingerprints | new_fingerprints:
            claim_ids = self._read_fingerprint_ids(fingerprint)
            existed = bool(claim_ids)
            claim_ids = [item for item in claim_ids if item != record["id"]]
            if fingerprint in new_fingerprints:
                claim_ids.append(record["id"])
            self._write_fingerprint_ids(fingerprint, claim_ids)
            if existed != bool(claim_ids):
                fingerprint_count += 1 if claim_ids else -1
        marker["claims_dir_mtime_ns"] = self._claims_directory_mtime()
        marker["updated_at"] = utc_now()
        if not old_record:
            marker["claims"] = int(marker.get("claims", 0)) + 1
        marker["fingerprints"] = max(0, fingerprint_count)
        atomic_write_json(self.fingerprint_marker, marker)

    def _propose_validated(self, values: dict, *, epistemic_status: str, history_action: str) -> dict:
        fingerprint = self._fingerprint(
            values["statement"],
            values["claim_type"],
            values["scope_kind"],
            values["scope_value"],
        )
        for claim_id in self._fingerprint_ids(fingerprint):
            try:
                existing = self._read(claim_id)
            except (BokError, OSError, UnicodeError):
                continue
            for candidate in [existing["statement"], *existing["statement_history"]]:
                if self._fingerprint(candidate, existing["claim_type"], existing["scope_kind"], existing["scope_value"]) != fingerprint:
                    continue
                result = self._public(existing)
                result["deduplicated"] = True
                result["rejected_guard"] = existing["epistemic_status"] == "rejected"
                result["historical_guard"] = candidate != existing["statement"]
                return result
        now = utc_now()
        record = {
            "id": f"person-{uuid.uuid4().hex}",
            "path": "",
            **values,
            "epistemic_status": epistemic_status,
            "importance": "important" if values["claim_type"] in IMPORTANT_CLAIM_TYPES else "ordinary",
            "support_count": len(values["source_refs"]),
            "contradiction_count": 0,
            "contradiction_refs": [],
            "statement_history": [],
            "first_seen": now,
            "last_seen": now,
            "valid_from": now,
            "valid_to": "",
            "confirmed_by_user": False,
            "supersedes": "",
            "superseded_by": "",
            "positive_outcomes": [],
            "negative_outcomes": [],
            "last_used": "",
            "last_influenced_answer": "",
            "version": 1,
            "created": now,
            "updated": now,
            "_history": "",
        }
        record["path"] = f"Claims/{record['id']}.md"
        self._history(record, history_action, ", ".join(values["source_refs"]))
        return self._write(record, operation=f"person_claim_propose_{epistemic_status}")

    def propose_explicit(
        self,
        *,
        statement: str,
        claim_type: str,
        scope_kind: str = "global",
        scope_value: str = "",
        confidence: float = 1.0,
        sensitivity: str = "private",
        access_scope=None,
        source_refs=None,
        expires_at: str = "",
    ) -> dict:
        self._require_root()
        values = self._validate_claim_values(
            statement=statement,
            claim_type=claim_type,
            scope_kind=scope_kind,
            scope_value=scope_value,
            confidence=confidence,
            sensitivity=sensitivity,
            access_scope=access_scope or ["personal-core"],
            source_refs=source_refs,
            expires_at=expires_at,
        )
        if values["access_scope"] != ["personal-core"]:
            raise BokError(
                "authorization_requires_confirmed_claim",
                "Agent access can only be granted after the personal claim is confirmed",
                status=409,
            )
        with self.lock:
            return self._propose_validated(values, epistemic_status="explicit", history_action="proposed_explicit")

    def propose_hypothesis(self, **values) -> dict:
        self._require_root()
        values["access_scope"] = ["personal-core"]
        validated = self._validate_claim_values(**values)
        with self.lock:
            return self._propose_validated(validated, epistemic_status="hypothesis", history_action="proposed_hypothesis")

    def add_evidence(self, claim_id: str, *, source_refs=None, contradiction_refs=None) -> dict:
        with self.lock:
            record = self._read(claim_id)
            added_sources = [item for item in self._clean_list(source_refs or [], field="source_refs", maximum=50) if item not in record["source_refs"]]
            added_contradictions = [item for item in self._clean_list(contradiction_refs or [], field="contradiction_refs", maximum=50) if item not in record["contradiction_refs"]]
            if not added_sources and not added_contradictions:
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            record["source_refs"] = self._clean_list([*record["source_refs"], *added_sources], field="source_refs", maximum=50)
            record["contradiction_refs"] = self._clean_list([*record["contradiction_refs"], *added_contradictions], field="contradiction_refs", maximum=50)
            record["support_count"] = len(record["source_refs"])
            record["contradiction_count"] = len(record["contradiction_refs"])
            if added_contradictions and record["epistemic_status"] in {"learned", "confirmed"}:
                record["epistemic_status"] = "contradicted"
            record["last_seen"] = utc_now()
            record["updated"] = utc_now()
            record["version"] += 1
            detail = f"support +{len(added_sources)}, contradiction +{len(added_contradictions)}"
            self._history(record, "evidence_updated", detail)
            return self._write(record, operation="person_claim_evidence")

    def adopt_learned(self, claim_id: str, *, reason: str = "evidence_threshold_met") -> dict:
        """Make a low-risk, evidence-backed understanding locally effective.

        This does not claim user confirmation and never expands the claim beyond
        Personal Core. Identity, authority and other protected claim types cannot
        enter this state.
        """
        with self.lock:
            record = self._read(claim_id)
            if record["claim_type"] not in QUIET_LEARNABLE_CLAIM_TYPES:
                raise ConflictError("This personal claim type requires user review")
            if record["epistemic_status"] in {"rejected", "superseded", "expired", "contradicted"}:
                raise ConflictError("Inactive or contradicted claims cannot be quietly learned")
            if record["contradiction_count"]:
                raise ConflictError("Claims with contradictory evidence require user review")
            if record["epistemic_status"] == "learned":
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            if record["epistemic_status"] == "confirmed" and record["confirmed_by_user"]:
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            record["epistemic_status"] = "learned"
            record["confirmed_by_user"] = False
            record["access_scope"] = ["personal-core"]
            record["last_seen"] = utc_now()
            record["updated"] = utc_now()
            record["version"] += 1
            self._history(record, "quietly_learned", self._clean(reason, field="reason", limit=240) or "evidence_threshold_met")
            return self._write(record, operation="person_claim_learn")

    def get(self, claim_id: str) -> dict:
        return self._public(self._read(claim_id))

    def list(self, *, status: str = "all", claim_type: str = "", limit: int = 100) -> dict:
        status = self._clean(status, field="status", limit=30).casefold() or "all"
        claim_type = self._clean(claim_type, field="claim_type", limit=40).casefold()
        if status != "all" and status not in EPISTEMIC_STATUSES:
            raise BokError("invalid_claim_status", "Personal claim status is not supported")
        if claim_type and claim_type not in CLAIM_TYPES:
            raise BokError("invalid_claim_type", "claim_type is not supported")
        records = self._all()
        if status and status != "all":
            records = [item for item in records if item["epistemic_status"] == status]
        if claim_type:
            records = [item for item in records if item["claim_type"] == claim_type]
        selected = records[: max(1, min(safe_int(limit, 100), 10000))]
        counts: Dict[str, int] = {}
        for item in records:
            counts[item["epistemic_status"]] = counts.get(item["epistemic_status"], 0) + 1
        return {"items": [self._public(item) for item in selected], "counts": counts}

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @staticmethod
    def _normalize_backup_relative(relative: str) -> str:
        value = str(relative or "").replace("\\", "/").strip()
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or any(part in {"", ".", "..", ".bok"} for part in candidate.parts):
            raise PermissionDeniedError("Personal Core backup contains an unsafe path", details={"path": value})
        normalized = candidate.as_posix()
        if not normalized.casefold().endswith(".md") or "\x00" in normalized:
            raise PermissionDeniedError("Personal Core backups may only contain Markdown", details={"path": value})
        return normalized

    def _markdown_files(self):
        root = self._require_root()
        roots = [root / "PERSONAL-CORE.md", *(root / name for name in PERSONAL_DATA_DIRECTORIES)]
        marker = roots[0]
        if marker.is_file() and not marker.is_symlink():
            yield marker
        for base in roots[1:]:
            if not base.is_dir() or base.is_symlink():
                continue
            for current, directories, filenames in os.walk(str(base)):
                current_path = Path(current)
                directories[:] = sorted(name for name in directories if not (current_path / name).is_symlink())
                for name in sorted(filenames):
                    path = current_path / name
                    if not name.casefold().endswith(".md") or path.is_symlink() or not path.is_file():
                        continue
                    try:
                        path.resolve(strict=True).relative_to(root)
                    except (OSError, RuntimeError, ValueError):
                        continue
                    yield path

    def _backup_path(self, backup_id: str) -> Path:
        value = str(backup_id or "")
        if not re.fullmatch(r"personal-backup-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{8}", value):
            raise BokError("invalid_personal_backup_id", "Invalid Personal Core backup identifier")
        return self.backups_dir / f"{value}.zip"

    def _restorer(self) -> TransactionalMarkdownRestore:
        root = self._require_root()
        return TransactionalMarkdownRestore(
            root=root,
            state_dir=self.state_dir,
            lock=self.lock,
            resolve_target=self._resolve_backup_target,
            current_files=self._markdown_files,
            relative_path=lambda path: path.relative_to(root).as_posix(),
            file_mode=0o600,
            namespace="personal-restore",
        )

    def _resolve_backup_target(self, relative: str) -> Path:
        normalized = self._normalize_backup_relative(relative)
        root = self._require_root()
        target = root.joinpath(*PurePosixPath(normalized).parts)
        current = root
        for part in PurePosixPath(normalized).parts:
            current = current / part
            if current.is_symlink():
                raise PermissionDeniedError("Personal Core restore cannot follow symbolic links", details={"path": normalized})
        try:
            target.parent.resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise PermissionDeniedError("Personal Core restore path escapes the core", details={"path": normalized}) from error
        return target

    def repair_restore_transactions(self) -> dict:
        if not self.configured:
            return {"repaired": 0, "failed": []}
        return self._restorer().repair_pending()

    def create_backup(self) -> dict:
        self.initialize()
        self._assert_safe_paths()
        backup_id = f"personal-backup-{compact_timestamp()}-{uuid.uuid4().hex[:8]}"
        destination = self._backup_path(backup_id)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{backup_id}.", suffix=".tmp", dir=str(self.backups_dir))
        os.close(descriptor)
        temporary = Path(temporary_name)
        manifest: Dict[str, str] = {}
        try:
            with self.lock, zipfile.ZipFile(str(temporary), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for path in self._markdown_files():
                    relative = path.relative_to(self._require_root()).as_posix()
                    data = path.read_bytes()
                    manifest[relative] = sha256_bytes(data)
                    archive.writestr(relative, data)
                archive.writestr(
                    ".bok-personal-backup-manifest.json",
                    json.dumps(
                        {"backup_id": backup_id, "created_at": utc_now(), "personal_core": self._require_root().name, "files": manifest},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
            os.replace(str(temporary), str(destination))
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "backup_id": backup_id,
            "file_count": len(manifest),
            "manifest_hash": sha256_bytes(json.dumps(manifest, sort_keys=True).encode("utf-8")),
        }

    def verify_backup(self, backup_id: str) -> dict:
        self._assert_safe_paths()
        path = self._backup_path(backup_id)
        if path.is_symlink():
            raise PermissionDeniedError("Personal Core backup cannot be a symbolic link")
        if not path.is_file():
            raise NotFoundError("Personal Core backup does not exist", details={"backup_id": backup_id})
        errors = []
        try:
            with zipfile.ZipFile(str(path), "r") as archive:
                try:
                    manifest = json.loads(archive.read(".bok-personal-backup-manifest.json"))
                except (KeyError, ValueError, zipfile.BadZipFile) as error:
                    raise BokError("personal_backup_corrupt", "Personal Core backup manifest is missing or invalid", status=422) from error
                files = manifest.get("files") if isinstance(manifest, dict) else None
                if not isinstance(files, dict) or manifest.get("backup_id") != backup_id or manifest.get("personal_core") != self._require_root().name:
                    raise BokError("personal_backup_corrupt", "Personal Core backup manifest does not match this core", status=422)
                if len(files) > 50000:
                    raise BokError("personal_backup_too_large", "Personal Core backup contains too many files", status=422)
                total_size = 0
                infos = archive.infolist()
                names = {item.filename: item for item in infos}
                expected_names = set(files) | {".bok-personal-backup-manifest.json"}
                if len(names) != len(infos) or set(names) != expected_names:
                    errors.append({"path": "", "error": "unexpected_or_duplicate_members"})
                for relative, expected in files.items():
                    try:
                        normalized = self._normalize_backup_relative(relative)
                        info = names.get(relative)
                        if normalized != relative or info is None or info.file_size > 20 * 1024 * 1024:
                            raise ValueError("invalid member")
                        total_size += info.file_size
                        if total_size > 512 * 1024 * 1024 or sha256_bytes(archive.read(relative)) != expected:
                            raise ValueError("invalid content")
                    except (KeyError, OSError, ValueError, PermissionDeniedError):
                        errors.append({"path": str(relative), "error": "invalid_or_missing"})
        except zipfile.BadZipFile as error:
            raise BokError("personal_backup_corrupt", "Personal Core backup is not a valid ZIP archive", status=422) from error
        return {"backup_id": backup_id, "valid": not errors, "file_count": len(files), "created_at": manifest.get("created_at", ""), "total_size": total_size, "errors": errors}

    def list_backups(self, *, limit: int = 100) -> dict:
        if not self.configured:
            return {"configured": False, "items": []}
        self.initialize()
        items = []
        for path in sorted(self.backups_dir.glob("personal-backup-*.zip"), reverse=True):
            if path.is_symlink():
                continue
            backup_id = path.stem
            try:
                result = self.verify_backup(backup_id)
                items.append({"backup_id": backup_id, "created_at": result["created_at"], "file_count": result["file_count"], "valid": result["valid"]})
            except BokError:
                items.append({"backup_id": backup_id, "created_at": "", "file_count": 0, "valid": False})
            if len(items) >= max(1, min(safe_int(limit, 100), 500)):
                break
        return {"configured": True, "items": items}

    def restore_backup(self, backup_id: str, *, confirm_personal_core: str, mode: str = "exact") -> dict:
        root = self._require_root()
        if confirm_personal_core != root.name:
            raise PermissionDeniedError("Personal Core restore requires the exact core name", details={"expected": root.name})
        with self.lock:
            verification = self.verify_backup(backup_id)
            if not verification["valid"]:
                raise BokError("personal_backup_corrupt", "Personal Core backup verification failed", status=422, details=verification)
            safety = self.create_backup()
            with zipfile.ZipFile(str(self._backup_path(backup_id)), "r") as archive:
                manifest = json.loads(archive.read(".bok-personal-backup-manifest.json"))
                desired = {relative: archive.read(relative) for relative in manifest["files"]}
            result = self._restorer().restore(
                desired,
                mode=mode,
                metadata={"backup_id": backup_id, "safety_backup": safety["backup_id"]},
            )
            self._cache_signature = None
            self._cache_records = None
            self.repair_links()
            return {"backup_id": backup_id, "safety_backup": safety["backup_id"], **result}

    def confirm(self, claim_id: str, *, source_ref: str = "") -> dict:
        with self.lock:
            record = self._read(claim_id)
            if record["epistemic_status"] in {"rejected", "superseded", "expired"}:
                raise ConflictError("Rejected, superseded or expired claims cannot be confirmed")
            changed = False
            cleaned_source = self._clean(source_ref, field="source_ref", limit=240)
            if cleaned_source and cleaned_source not in record["source_refs"]:
                record["source_refs"] = self._clean_list(
                    [*record["source_refs"], cleaned_source],
                    field="source_refs",
                    maximum=50,
                )
                record["support_count"] = len(record["source_refs"])
                changed = True
            already_confirmed = record["epistemic_status"] == "confirmed" and record["confirmed_by_user"]
            if not already_confirmed and record["access_scope"] != ["personal-core"]:
                # Migrate pending claims created by older clients to the new
                # least-privilege contract. Confirmation never grants access.
                record["access_scope"] = ["personal-core"]
                changed = True
            if already_confirmed and not changed:
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            record["epistemic_status"] = "confirmed"
            record["confirmed_by_user"] = True
            record["last_seen"] = utc_now()
            record["updated"] = utc_now()
            record["version"] += 1
            self._history(record, "confirmation_updated" if already_confirmed else "confirmed", cleaned_source or "user")
            return self._write(record, operation="person_claim_confirm")

    def authorize(self, claim_id: str, *, access_scope, source_ref: str = "") -> dict:
        with self.lock:
            record = self._read(claim_id)
            is_confirmed = record["epistemic_status"] == "confirmed" and record["confirmed_by_user"]
            if record["epistemic_status"] != "learned" and not is_confirmed:
                raise ConflictError("Personal claims must be effective before Agent access can change")
            validated_access = self._validate_claim_values(
                statement=record["statement"],
                claim_type=record["claim_type"],
                scope_kind=record["scope_kind"],
                scope_value=record["scope_value"],
                confidence=record["confidence"],
                sensitivity=record["sensitivity"],
                access_scope=access_scope,
                source_refs=record["source_refs"],
                expires_at=record["expires_at"],
            )["access_scope"]
            if validated_access == record["access_scope"]:
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            record["access_scope"] = validated_access
            record["updated"] = utc_now()
            record["version"] += 1
            cleaned_source = self._clean(source_ref, field="source_ref", limit=240)
            self._history(record, "authorization_updated", cleaned_source or ", ".join(validated_access))
            return self._write(record, operation="person_claim_authorize")

    def correct(
        self,
        claim_id: str,
        *,
        statement: str,
        source_ref: str,
        scope_kind: str = "",
        scope_value: str = "",
    ) -> dict:
        with self.lock:
            record = self._read(claim_id)
            if record["epistemic_status"] == "superseded":
                raise ConflictError("Superseded claims must be replaced through their active successor")
            cleaned_source = self._clean(source_ref, field="source_ref", limit=240, required=True)
            values = self._validate_claim_values(
                statement=statement,
                claim_type=record["claim_type"],
                scope_kind=scope_kind or record["scope_kind"],
                scope_value=scope_value if scope_kind else record["scope_value"],
                confidence=1.0,
                sensitivity=record["sensitivity"],
                access_scope=record["access_scope"],
                source_refs=list(record["source_refs"]) + [cleaned_source],
                expires_at=record["expires_at"],
            )
            old_statement = record["statement"]
            if old_statement != values["statement"] and old_statement not in record["statement_history"]:
                record["statement_history"].append(old_statement)
            was_learned = record["epistemic_status"] == "learned"
            record.update(values)
            record["epistemic_status"] = "learned" if was_learned else "confirmed"
            record["confirmed_by_user"] = True
            record["support_count"] = len(record["source_refs"])
            record["last_seen"] = utc_now()
            record["updated"] = utc_now()
            record["version"] += 1
            self._history(record, "corrected", f"{cleaned_source} · previous: {old_statement}")
            return self._write(record, operation="person_claim_correct")

    def reject(self, claim_id: str, *, reason: str, source_ref: str = "") -> dict:
        with self.lock:
            record = self._read(claim_id)
            if record["epistemic_status"] == "superseded":
                raise ConflictError("Superseded claims cannot be rejected")
            cleaned_reason = self._clean(reason, field="reason", limit=500, required=True)
            cleaned_source = self._clean(source_ref, field="source_ref", limit=240)
            if record["epistemic_status"] == "rejected":
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            record["epistemic_status"] = "rejected"
            record["confirmed_by_user"] = False
            record["updated"] = utc_now()
            record["version"] += 1
            self._history(record, "rejected", f"{cleaned_reason} · {cleaned_source or 'user'}")
            return self._write(record, operation="person_claim_reject")

    def expire(self, claim_id: str, *, reason: str, confirm_important: bool = False) -> dict:
        with self.lock:
            record = self._read(claim_id)
            if record["importance"] == "important" and not confirm_important:
                raise PermissionDeniedError("Expiring an important personal claim requires explicit confirmation")
            if record["epistemic_status"] in {"rejected", "superseded"}:
                raise ConflictError("Rejected or superseded claims cannot be expired")
            if record["epistemic_status"] == "expired":
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            cleaned_reason = self._clean(reason, field="reason", limit=500, required=True)
            now = utc_now()
            record["epistemic_status"] = "expired"
            record["confirmed_by_user"] = False
            record["valid_to"] = now
            record["updated"] = now
            record["version"] += 1
            self._history(record, "expired", cleaned_reason)
            return self._write(record, operation="person_claim_expire")

    def record_outcome(self, claim_id: str, *, outcome_id: str, outcome: str) -> dict:
        with self.lock:
            record = self._read(claim_id)
            outcome_id = self._clean(outcome_id, field="outcome_id", limit=80, required=True)
            outcome = self._clean(outcome, field="outcome", limit=20, required=True).casefold()
            if outcome not in {"positive", "negative", "neutral"}:
                raise BokError("invalid_outcome", "outcome must be positive, negative or neutral")
            target = record["positive_outcomes"] if outcome == "positive" else record["negative_outcomes"] if outcome == "negative" else None
            if target is None or outcome_id in target:
                result = self._public(record)
                result["unchanged"] = True
                result["version_id"] = ""
                return result
            target.append(outcome_id)
            record["updated"] = utc_now()
            record["version"] += 1
            self._history(record, "outcome_recorded", f"{outcome}: {outcome_id}")
            return self._write(record, operation="person_claim_outcome")

    def supersede(
        self,
        claim_id: str,
        *,
        statement: str,
        source_ref: str,
        scope_kind: str = "",
        scope_value: str = "",
    ) -> dict:
        with self.lock:
            old = self._read(claim_id)
            if old["epistemic_status"] in {"rejected", "superseded"}:
                raise ConflictError("Rejected or superseded claims cannot be superseded again")
            cleaned_source = self._clean(source_ref, field="source_ref", limit=240, required=True)
            values = self._validate_claim_values(
                statement=statement,
                claim_type=old["claim_type"],
                scope_kind=scope_kind or old["scope_kind"],
                scope_value=scope_value if scope_kind else old["scope_value"],
                confidence=1.0,
                sensitivity=old["sensitivity"],
                access_scope=old["access_scope"],
                source_refs=list(old["source_refs"]) + [cleaned_source],
                expires_at=old["expires_at"],
            )
            now = utc_now()
            new = {
                "id": f"person-{uuid.uuid4().hex}",
                "path": "",
                **values,
                "epistemic_status": "confirmed",
                "importance": old["importance"],
                "support_count": len(values["source_refs"]),
                "contradiction_count": 0,
                "contradiction_refs": [],
                "statement_history": [],
                "first_seen": now,
                "last_seen": now,
                "valid_from": now,
                "valid_to": "",
                "confirmed_by_user": True,
                "supersedes": old["id"],
                "superseded_by": "",
                "positive_outcomes": [],
                "negative_outcomes": [],
                "last_used": "",
                "last_influenced_answer": "",
                "version": 1,
                "created": now,
                "updated": now,
                "_history": "",
            }
            new["path"] = f"Claims/{new['id']}.md"
            self._history(new, "created_as_successor", f"{old['id']} · {cleaned_source}")
            new_result = self._write(new, operation="person_claim_supersede_create")
            old["epistemic_status"] = "superseded"
            old["superseded_by"] = new["id"]
            old["valid_to"] = now
            old["updated"] = now
            old["version"] += 1
            self._history(old, "superseded", f"{new['id']} · {cleaned_source}")
            old_result = self._write(old, operation="person_claim_supersede_old")
            return {"old": old_result, "replacement": new_result}

    def explain(self, claim_id: str) -> dict:
        record = self._read(claim_id)
        result = self._public(record)
        result["history"] = record.get("_history", "")
        result["explanation"] = {
            "basis": "user_confirmed" if record["confirmed_by_user"] else record["epistemic_status"],
            "sources": record["source_refs"],
            "contradictions": record["contradiction_refs"],
            "scope": {"kind": record["scope_kind"], "value": record["scope_value"]},
            "effective_reason": self._effective_reason(record),
        }
        return result

    def versions(self, claim_id: str, *, limit: int = 100) -> dict:
        self._assert_safe_paths()
        claim_id = self._claim_id(claim_id)
        items = []
        if self.versions_dir.is_dir():
            for path in self.versions_dir.glob("*/meta.json"):
                try:
                    if path.is_symlink() or path.parent.is_symlink():
                        continue
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    continue
                if isinstance(value, dict) and value.get("claim_id") == claim_id:
                    if value.get("status", "committed") != "committed":
                        continue
                    value["rollback_supported"] = bool(value.get("before_exists"))
                    items.append(value)
        items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {"items": items[: max(1, min(safe_int(limit, 100), 500))]}

    def rollback(self, version_id: str, *, confirm_important: bool) -> dict:
        self._assert_safe_paths()
        if not confirm_important:
            raise PermissionDeniedError("Personal claim rollback requires explicit confirmation")
        version_id = self._clean(version_id, field="version_id", limit=100, required=True)
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]+Z-[0-9a-f]{10}", version_id):
            raise BokError("invalid_version_id", "Personal claim version identifier is invalid")
        directory = self.versions_dir / version_id
        meta_path = directory / "meta.json"
        before_path = directory / "before.md"
        if directory.is_symlink() or meta_path.is_symlink() or before_path.is_symlink():
            raise BokError("unsafe_personal_version", "Personal claim version paths cannot be symbolic links", status=403)
        if not meta_path.is_file() or not before_path.is_file():
            raise NotFoundError("Personal claim rollback version does not exist")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise BokError("invalid_version", "Personal claim version metadata is invalid", status=500) from error
        if not isinstance(meta, dict) or meta.get("status", "committed") != "committed":
            raise ConflictError("Personal claim version is not committed")
        try:
            before = before_path.read_bytes()
        except OSError as error:
            raise BokError("invalid_version", "Personal claim rollback snapshot cannot be read", status=500) from error
        if meta.get("before_hash") != sha256_bytes(before):
            raise ConflictError("Personal claim rollback snapshot hash does not match its version record")
        claim_id = self._claim_id(str(meta.get("claim_id", "")))
        with self.lock:
            current = self._read(claim_id)
            try:
                restored_text = before.decode("utf-8-sig")
            except UnicodeError as error:
                raise BokError("invalid_version", "Personal claim rollback snapshot is not valid UTF-8", status=500) from error
            restored = self._record(restored_text, path=self._path(claim_id))
            if any((current["supersedes"], current["superseded_by"], restored["supersedes"], restored["superseded_by"])):
                raise ConflictError("Linked claims cannot be rolled back independently; correct or supersede the active claim instead")
            restored["version"] = current["version"] + 1
            restored["updated"] = utc_now()
            self._history(restored, "rolled_back", version_id)
            return self._write(restored, operation="person_claim_rollback")

    @staticmethod
    def _task_tokens(value: str) -> set:
        folded = str(value or "").casefold()
        latin = set(re.findall(r"[a-z0-9_/-]+", folded))
        chinese = "".join(re.findall(r"[\u3400-\u9fff]", folded))
        grams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
        return latin | grams

    @staticmethod
    def _authorized(record: dict, *, agent: str, project: str) -> bool:
        scopes = set(record["access_scope"])
        return (
            (record.get("epistemic_status") == "learned" and "personal-core" in scopes and bool(agent))
            or
            "all-agents" in scopes
            or (agent and f"agent:{agent}" in scopes)
            or (project and f"project:{project}" in scopes)
        )

    def context(
        self,
        *,
        task: str,
        agent: str,
        project: str = "",
        limit: int = 6,
        token_budget: int = 1500,
    ) -> dict:
        task = self._clean(task, field="task", limit=4000, required=True)
        agent = self._clean(agent, field="agent", limit=80, required=True)
        project = self._clean(project, field="project", limit=240)
        limit = max(1, min(safe_int(limit, 6), 12))
        token_budget = max(256, min(safe_int(token_budget, 1500), self.config.max_context_tokens))
        task_tokens = self._task_tokens(task)
        ranked = []
        for record in self._all():
            if self._effective_reason(record) != "active" or not self._authorized(record, agent=agent, project=project):
                continue
            if record["scope_kind"] == "project" and record["scope_value"] != project:
                continue
            if record["scope_kind"] == "agent" and record["scope_value"] != agent:
                continue
            claim_tokens = self._task_tokens(
                " ".join([record["statement"], record["claim_type"], record["scope_value"]])
            )
            overlap = len(task_tokens & claim_tokens)
            if record["scope_kind"] in {"task_type", "context"} and not overlap:
                continue
            score = overlap * 3.0
            if record["scope_kind"] == "project" and record["scope_value"] == project:
                score += 10.0
            elif record["scope_kind"] == "agent" and record["scope_value"] == agent:
                score += 8.0
            elif record["scope_kind"] == "global":
                score += 1.0
            if record["claim_type"] in {"authority_rule", "communication_preference", "work_preference"}:
                score += 2.0
            score += float(record["confidence"])
            ranked.append((score, record))
        ranked.sort(key=lambda item: (item[0], item[1]["updated"], item[1]["id"]), reverse=True)
        selected = []
        lines = []
        used_tokens = 0
        for score, record in ranked:
            if len(selected) >= limit:
                break
            citation = f"P{len(selected) + 1}"
            scope = record["scope_kind"] + (f":{record['scope_value']}" if record["scope_value"] else "")
            line = f"[{citation}] {record['statement']}（{record['claim_type']}；{scope}）"
            line_tokens = estimate_tokens(line)
            if used_tokens + line_tokens > token_budget:
                continue
            used_tokens += line_tokens
            lines.append(line)
            selected.append(
                {
                    "citation": citation,
                    "claim_id": record["id"],
                    "statement": record["statement"],
                    "claim_type": record["claim_type"],
                    "scope": {"kind": record["scope_kind"], "value": record["scope_value"]},
                    "confidence": record["confidence"],
                    "updated": record["updated"],
                    "score": round(score, 4),
                    "why": "learned_scope_and_task_match" if record["epistemic_status"] == "learned" else "confirmed_scope_and_task_match",
                }
            )
        return {
            "task": task,
            "agent": agent,
            "project": project,
            "context": "\n".join(lines),
            "claims": selected,
            "token_estimate": used_tokens,
            "token_budget": token_budget,
        }

    def health(self) -> dict:
        if not self.configured:
            return {
                "configured": False,
                "ready": False,
                "reason": "personal_core_not_configured",
                "counts": {},
                "broken_links": 0,
                "inconsistent_links": 0,
                "corrupt_claims": 0,
                "versions": {"committed": 0, "pending": 0, "aborted": 0, "corrupt": 0},
            }
        try:
            root = self._require_root()
            self._assert_safe_paths()
        except BokError as error:
            return {
                "configured": True,
                "ready": False,
                "reason": error.code,
                "counts": {},
                "broken_links": 0,
                "inconsistent_links": 0,
                "corrupt_claims": 0,
                "versions": {"committed": 0, "pending": 0, "aborted": 0, "corrupt": 0},
            }
        required = (
            root,
            *(root / name for name in PERSONAL_DATA_DIRECTORIES),
            root / ".bok",
            root / ".bok/versions",
            root / ".bok/backups",
        )
        if not all(path.is_dir() for path in required):
            return {
                "configured": True,
                "ready": False,
                "reason": "personal_core_not_initialized",
                "counts": {},
                "broken_links": 0,
                "inconsistent_links": 0,
                "corrupt_claims": 0,
                "versions": {"committed": 0, "pending": 0, "aborted": 0, "corrupt": 0},
            }
        claim_files = list(self.claims_dir.glob("person-*.md"))
        records = self._all()
        ids = {item["id"] for item in records}
        by_id = {item["id"]: item for item in records}
        counts: Dict[str, int] = {}
        broken = 0
        inconsistent = 0
        for item in records:
            counts[item["epistemic_status"]] = counts.get(item["epistemic_status"], 0) + 1
            if item["supersedes"] and item["supersedes"] not in ids:
                broken += 1
            if item["superseded_by"] and item["superseded_by"] not in ids:
                broken += 1
            if item["superseded_by"]:
                successor = by_id.get(item["superseded_by"])
                if successor and successor["supersedes"] != item["id"]:
                    inconsistent += 1
            if item["supersedes"]:
                previous = by_id.get(item["supersedes"])
                if previous and previous["superseded_by"] != item["id"]:
                    inconsistent += 1
        corrupt = max(0, len(claim_files) - len(records))
        versions = self._version_health()
        try:
            pending_forget_transactions = sum(
                1 for item in self.forget_transactions_dir.iterdir()
                if item.is_dir() and not item.is_symlink()
            )
        except OSError:
            pending_forget_transactions = 1
        ready = (
            broken == 0
            and inconsistent == 0
            and corrupt == 0
            and versions["pending"] == 0
            and versions["corrupt"] == 0
            and pending_forget_transactions == 0
        )
        return {
            "configured": True,
            "ready": ready,
            "name": self._require_root().name,
            "counts": counts,
            "broken_links": broken,
            "inconsistent_links": inconsistent,
            "corrupt_claims": corrupt,
            "versions": versions,
            "pending_forget_transactions": pending_forget_transactions,
        }
