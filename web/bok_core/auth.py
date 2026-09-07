from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path

from .errors import BokError, NotFoundError
from .util import InterProcessFileLock, atomic_write_json, read_json, sha256_text, utc_now


ALLOWED_AGENT_SCOPES = {
    "vault:read",
    "memory:capture",
    "context:read",
    "conversation:observe",
    "impact:write",
    "outcome:write",
}


class AgentCredentialStore:
    """Hashed, revocable credentials for local Agent clients.

    The plaintext token is returned once. Only its SHA-256 digest is persisted in
    Bok's private runtime directory. The loopback API remains the network trust
    boundary; scopes narrow what a token can do after authentication.
    """

    def __init__(self, state_dir: Path):
        self.path = state_dir / "state" / "agent-credentials.json"
        self.lock = InterProcessFileLock(state_dir / "write.lock")

    @staticmethod
    def _agent_id(value: str) -> str:
        agent_id = str(value or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", agent_id):
            raise BokError("invalid_agent_id", "agent_id must use 1-80 lowercase letters, numbers, dots, dashes or underscores")
        return agent_id

    @staticmethod
    def _scopes(values) -> list[str]:
        if values is None:
            return sorted(ALLOWED_AGENT_SCOPES)
        if not isinstance(values, (list, tuple)):
            raise BokError("invalid_agent_scopes", "Agent scopes must be an array")
        scopes = list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))
        if not scopes or any(scope not in ALLOWED_AGENT_SCOPES for scope in scopes):
            raise BokError("invalid_agent_scopes", "Agent scopes contain an unsupported or empty value")
        return scopes

    def _load(self) -> dict:
        value = read_json(self.path, {})
        return value if isinstance(value, dict) else {}

    def issue(self, agent_id: str, *, scopes=None) -> dict:
        agent_id = self._agent_id(agent_id)
        scopes = self._scopes(scopes)
        token = "bok_agent_" + secrets.token_urlsafe(32)
        with self.lock:
            records = self._load()
            records[agent_id] = {
                "agent_id": agent_id,
                "token_hash": sha256_text(token),
                "scopes": scopes,
                "status": "active",
                "issued_at": utc_now(),
                "revoked_at": "",
            }
            atomic_write_json(self.path, records)
        return {"agent_id": agent_id, "token": token, "scopes": scopes, "issued": True}

    def verify(self, token: str) -> dict | None:
        supplied = str(token or "")
        if not supplied.startswith("bok_agent_"):
            return None
        supplied_hash = sha256_text(supplied)
        for record in self._load().values():
            if not isinstance(record, dict) or record.get("status") != "active":
                continue
            stored = str(record.get("token_hash", ""))
            if stored and hmac.compare_digest(supplied_hash, stored):
                return {
                    "kind": "agent",
                    "agent_id": str(record.get("agent_id", "")),
                    "scopes": list(record.get("scopes") or []),
                }
        return None

    def revoke(self, agent_id: str) -> dict:
        agent_id = self._agent_id(agent_id)
        with self.lock:
            records = self._load()
            record = records.get(agent_id)
            if not isinstance(record, dict):
                raise NotFoundError("Agent credential does not exist", details={"agent_id": agent_id})
            record["status"] = "revoked"
            record["revoked_at"] = utc_now()
            records[agent_id] = record
            atomic_write_json(self.path, records)
        return {"agent_id": agent_id, "revoked": True}

    def list(self) -> dict:
        items = []
        for record in self._load().values():
            if not isinstance(record, dict):
                continue
            items.append(
                {
                    "agent_id": str(record.get("agent_id", "")),
                    "scopes": list(record.get("scopes") or []),
                    "status": str(record.get("status", "")),
                    "issued_at": str(record.get("issued_at", "")),
                    "revoked_at": str(record.get("revoked_at", "")),
                }
            )
        items.sort(key=lambda item: item["agent_id"])
        return {"items": items, "allowed_scopes": sorted(ALLOWED_AGENT_SCOPES)}
