from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import BokConfig
from .errors import BokError, ConflictError, NotFoundError
from .memory import MemoryInbox
from .util import atomic_write_json, canonical_json, read_json, sha256_text, utc_now


ALLOWED_ROLES = {"user", "assistant", "tool", "system"}
ALLOWED_MEMORY_MODES = {"default", "session_only", "do_not_remember"}


class ConversationLedger:
    """Durable, non-blocking receipts for conversation turns.

    The ledger is runtime state, not long-term memory. Each event is stored in its
    own private JSON record so a large conversation history never becomes one
    rewrite-heavy queue file. Only eligible user turns are handed to MemoryInbox.
    """

    def __init__(self, config: BokConfig, memory: MemoryInbox):
        self.config = config
        self.memory = memory
        self.events_dir = config.state_dir / "state" / "conversations" / "events"
        self.summary_path = config.state_dir / "state" / "conversations" / "summary.json"
        self.lock = memory.storage.lock

    @staticmethod
    def _clean(value, *, field: str, limit: int, required: bool = False) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if required and not text:
            raise BokError(f"invalid_{field}", f"{field} is required")
        if len(text) > limit:
            raise BokError(f"invalid_{field}", f"{field} exceeds the {limit} character limit")
        return text

    @staticmethod
    def _event_id(conversation_id: str, turn_id: str) -> str:
        return sha256_text(f"{conversation_id}\n{turn_id}")[:32]

    def _path(self, event_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", event_id):
            raise BokError("invalid_event_id", "event_id must be a 32 character lowercase hex identifier")
        return self.events_dir / event_id[:2] / f"{event_id}.json"

    @staticmethod
    def _expires_at(days: int) -> str:
        value = datetime.now(timezone.utc) + timedelta(days=days)
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _read(self, path: Path) -> dict:
        value = read_json(path, {})
        if not isinstance(value, dict) or not value.get("id"):
            raise BokError("conversation_event_corrupt", "Conversation event record is invalid", status=500)
        return value

    def _write(self, event: dict) -> None:
        path = self._path(str(event["id"]))
        previous = read_json(path, {})
        previous_status = str(previous.get("status", "")) if isinstance(previous, dict) else ""
        atomic_write_json(path, event)
        new_status = str(event.get("status", "unknown"))
        if previous_status != new_status:
            summary = read_json(self.summary_path, {})
            if not isinstance(summary, dict) or not isinstance(summary.get("counts"), dict):
                summary = {"total": 0, "counts": {}}
            counts = {str(key): int(value) for key, value in summary["counts"].items() if isinstance(value, int) and value > 0}
            if previous_status:
                counts[previous_status] = max(0, counts.get(previous_status, 0) - 1)
                if not counts[previous_status]:
                    counts.pop(previous_status, None)
            else:
                summary["total"] = int(summary.get("total", 0)) + 1
            counts[new_status] = counts.get(new_status, 0) + 1
            summary["counts"] = counts
            summary["updated_at"] = utc_now()
            atomic_write_json(self.summary_path, summary)

    def _iter_events(self) -> List[dict]:
        if not self.events_dir.is_dir():
            return []
        events: List[dict] = []
        for path in self.events_dir.glob("*/*.json"):
            value = read_json(path, {})
            if isinstance(value, dict) and value.get("id"):
                events.append(value)
        return sorted(events, key=lambda item: (str(item.get("created_at", "")), str(item.get("id", ""))))

    def _capture_status(self, capture_id: str) -> str:
        if not capture_id:
            return ""
        try:
            return str(self.memory.capture_status(capture_id).get("status", ""))
        except BokError:
            return "unknown"

    def _public(self, event: dict) -> dict:
        hidden = {"content", "explicit_cloud_consent", "request_fingerprint"}
        value = {key: item for key, item in event.items() if key not in hidden}
        if event.get("capture_id"):
            value["capture_status"] = self._capture_status(str(event["capture_id"]))
        return value

    def _queue_eligible(self, event: dict) -> bool:
        return (
            event.get("role") == "user"
            and event.get("memory_mode") == "default"
            and not event.get("external_content")
            and bool(event.get("content"))
        )

    def _queue(self, event: dict) -> None:
        if not self._queue_eligible(event):
            return
        capture = self.memory.capture(
            str(event["content"]),
            source={
                "type": "conversation",
                "ref": f"{event['conversation_id']}:{event['turn_id']}",
                "at": event["created_at"],
            },
            explicit_cloud_consent=bool(event.get("explicit_cloud_consent")),
        )
        event["capture_id"] = capture["id"]
        event["status"] = "queued_for_analysis"
        event["last_error"] = ""
        event["updated_at"] = utc_now()

    def observe(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        role: str,
        content: str,
        memory_mode: str = "default",
        external_content: bool = False,
        client: str = "",
        agent: str = "",
        project: str = "",
        person_signal_hash: str = "",
        explicit_cloud_consent: bool = False,
    ) -> dict:
        conversation_id = self._clean(conversation_id, field="conversation_id", limit=200, required=True)
        turn_id = self._clean(turn_id, field="turn_id", limit=200, required=True)
        role = self._clean(role, field="role", limit=20, required=True).casefold()
        memory_mode = self._clean(memory_mode, field="memory_mode", limit=30, required=True).casefold()
        content = str(content or "").strip()
        if role not in ALLOWED_ROLES:
            raise BokError("invalid_role", "role must be user, assistant, tool or system")
        if memory_mode not in ALLOWED_MEMORY_MODES:
            raise BokError("invalid_memory_mode", "memory_mode must be default, session_only or do_not_remember")
        if not content:
            raise BokError("empty_content", "Conversation turn content cannot be empty")
        if len(content) > 40000:
            raise BokError("content_too_large", "Conversation turn exceeds the 40,000 character safety limit", status=413)

        event_id = self._event_id(conversation_id, turn_id)
        content_hash = sha256_text(content)
        request_value = {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "role": role,
            "content_hash": content_hash,
            "memory_mode": memory_mode,
            "external_content": bool(external_content),
            "client": self._clean(client, field="client", limit=80),
            "agent": self._clean(agent, field="agent", limit=80),
            "project": self._clean(project, field="project", limit=240),
            "person_signal_hash": self._clean(person_signal_hash, field="person_signal_hash", limit=64),
            "explicit_cloud_consent": bool(explicit_cloud_consent),
        }
        fingerprint = sha256_text(canonical_json(request_value))

        with self.lock:
            path = self._path(event_id)
            if path.is_file():
                existing = self._read(path)
                if existing.get("request_fingerprint") != fingerprint:
                    raise ConflictError(
                        "conversation_id and turn_id were reused with different content or policy",
                        details={"conversation_id": conversation_id, "turn_id": turn_id},
                    )
                result = self._public(existing)
                result["idempotent_replay"] = True
                return result

            now = utc_now()
            event = {
                "id": event_id,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "role": role,
                "created_at": now,
                "updated_at": now,
                "status": "received",
                "content_hash": content_hash if memory_mode != "do_not_remember" else "",
                "content": content if memory_mode != "do_not_remember" else "",
                "content_expires_at": self._expires_at(self.config.conversation_retention_days),
                "memory_mode": memory_mode,
                "external_content": bool(external_content),
                "client": request_value["client"],
                "agent": request_value["agent"],
                "project": request_value["project"],
                "person_signal_hash": request_value["person_signal_hash"],
                "explicit_cloud_consent": bool(explicit_cloud_consent),
                "capture_id": "",
                "last_error": "",
                "request_fingerprint": fingerprint,
            }
            if memory_mode == "do_not_remember":
                event.pop("content", None)
                event["status"] = "excluded_do_not_remember"
                event["content_expires_at"] = now
            elif external_content:
                event["status"] = "excluded_external_content"
            elif role != "user":
                event["status"] = "recorded_non_user"
            elif memory_mode == "session_only":
                event["status"] = "session_only"

            # Write the receipt before touching the downstream queue. A crash in
            # the next step is repaired by reconcile() without losing the turn.
            self._write(event)
            try:
                self._queue(event)
            except BokError as error:
                event["status"] = "received_unqueued"
                event["last_error"] = error.code
                event["updated_at"] = utc_now()
            self._write(event)
            return self._public(event)

    def reconcile(self, *, limit: int = 100) -> dict:
        repaired = []
        with self.lock:
            candidates = [
                item
                for item in self._iter_events()
                if item.get("status") in {"received", "received_unqueued"} and self._queue_eligible(item)
            ][:max(1, min(limit, 500))]
            for event in candidates:
                try:
                    self._queue(event)
                except BokError as error:
                    event["status"] = "received_unqueued"
                    event["last_error"] = error.code
                    event["updated_at"] = utc_now()
                self._write(event)
                repaired.append(self._public(event))
        return {"reconciled": repaired, "remaining": self.counts().get("received_unqueued", 0)}

    def status(
        self,
        *,
        event_id: str = "",
        conversation_id: str = "",
        turn_id: str = "",
        limit: int = 100,
    ) -> dict:
        if not event_id and conversation_id and turn_id:
            event_id = self._event_id(
                self._clean(conversation_id, field="conversation_id", limit=200, required=True),
                self._clean(turn_id, field="turn_id", limit=200, required=True),
            )
        if event_id:
            path = self._path(event_id)
            if not path.is_file():
                raise NotFoundError("Conversation event does not exist", details={"event_id": event_id})
            return self._public(self._read(path))
        events = self._iter_events()
        if conversation_id:
            cleaned = self._clean(conversation_id, field="conversation_id", limit=200, required=True)
            events = [item for item in events if item.get("conversation_id") == cleaned]
        selected = list(reversed(events[-max(1, min(limit, 500)) :]))
        return {"items": [self._public(item) for item in selected], "counts": self.counts(events)}

    def counts(self, events: Optional[List[dict]] = None) -> Dict[str, int]:
        if events is None:
            summary = read_json(self.summary_path, {})
            if isinstance(summary, dict) and isinstance(summary.get("counts"), dict):
                return {str(key): int(value) for key, value in summary["counts"].items() if isinstance(value, int) and value > 0}
            events = self._iter_events()
        result: Dict[str, int] = {}
        for item in events:
            status = str(item.get("status", "unknown"))
            result[status] = result.get(status, 0) + 1
        return result

    def repair_summary(self) -> dict:
        events = self._iter_events()
        counts = self.counts(events)
        atomic_write_json(
            self.summary_path,
            {"total": len(events), "counts": counts, "updated_at": utc_now()},
        )
        return {"total": len(events), "counts": counts}

    def purge_expired_content(self) -> dict:
        purged = 0
        now = datetime.now(timezone.utc)
        with self.lock:
            for event in self._iter_events():
                expires = self._parse_time(str(event.get("content_expires_at", "")))
                if not event.get("content") or expires is None or expires > now:
                    continue
                if event.get("capture_id"):
                    try:
                        capture = self.memory.discard_capture(str(event["capture_id"]), reason="conversation_content_expired")
                        if capture.get("status") == "discarded":
                            event["status"] = "expired_unprocessed"
                    except BokError:
                        pass
                event.pop("content", None)
                event.pop("explicit_cloud_consent", None)
                event["content_purged_at"] = utc_now()
                if event.get("status") in {"received", "received_unqueued", "session_only"}:
                    event["status"] = "expired_unprocessed"
                event["updated_at"] = utc_now()
                self._write(event)
                purged += 1
        return {"purged": purged}

    def forget_turn(self, *, conversation_id: str, turn_id: str) -> dict:
        """Erase a turn's raw content while retaining a content-free receipt."""
        conversation_id = self._clean(conversation_id, field="conversation_id", limit=200, required=True)
        turn_id = self._clean(turn_id, field="turn_id", limit=200, required=True)
        event_id = self._event_id(conversation_id, turn_id)
        with self.lock:
            path = self._path(event_id)
            if not path.is_file():
                return {"event_id": event_id, "forgotten": False, "reason": "event_not_found"}
            event = self._read(path)
            capture_result = None
            if event.get("capture_id"):
                try:
                    capture_result = self.memory.forget_capture(str(event["capture_id"]))
                except BokError as error:
                    capture_result = {"forgotten": False, "reason": error.code}
            for field in (
                "content", "content_hash", "content_expires_at", "explicit_cloud_consent",
                "person_signal_hash", "request_fingerprint", "last_error",
            ):
                event.pop(field, None)
            event["memory_mode"] = "do_not_remember"
            event["status"] = "forgotten"
            event["forgotten_at"] = utc_now()
            event["updated_at"] = event["forgotten_at"]
            self._write(event)
            return {"event_id": event_id, "forgotten": True, "capture": capture_result}
