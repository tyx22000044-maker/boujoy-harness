from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .errors import BokError
from .markdown import list_value, parse_frontmatter, render_frontmatter
from .util import utc_now


CLAIM_TYPES = {
    "identity",
    "long_term_goal",
    "communication_preference",
    "work_preference",
    "decision_pattern",
    "authority_rule",
    "public_identity",
    "capability_claim",
    "project_experience",
    "knowledge_claim",
    "negative_preference",
    "temporary_state",
    "behavior_hypothesis",
}
EPISTEMIC_STATUSES = {
    "explicit",
    "observed",
    "hypothesis",
    "learned",
    "confirmed",
    "contradicted",
    "superseded",
    "expired",
    "rejected",
}
QUIET_LEARNABLE_CLAIM_TYPES = {
    "communication_preference",
    "work_preference",
    "decision_pattern",
    "capability_claim",
    "project_experience",
    "knowledge_claim",
    "negative_preference",
    "behavior_hypothesis",
}
REVIEW_REQUIRED_CLAIM_TYPES = {
    "identity",
    "long_term_goal",
    "authority_rule",
    "public_identity",
}
SCOPE_KINDS = {"global", "project", "task_type", "agent", "context"}
SENSITIVITIES = {"none", "private", "sensitive"}
IMPORTANT_CLAIM_TYPES = {
    "identity",
    "long_term_goal",
    "communication_preference",
    "work_preference",
    "decision_pattern",
    "authority_rule",
    "public_identity",
    "negative_preference",
    "behavior_hypothesis",
}


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() == "true"


class PersonalClaimCodec:
    """Canonical Markdown encoding and fail-closed validation for personal claims."""

    @staticmethod
    def _clean(value, *, field: str, limit: int, required: bool = False) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if required and not text:
            raise BokError(f"invalid_{field}", f"{field} is required")
        if len(text) > limit:
            raise BokError(f"invalid_{field}", f"{field} exceeds the {limit} character limit")
        return text

    @classmethod
    def _clean_list(cls, values, *, field: str, item_limit: int = 240, maximum: int = 50) -> List[str]:
        if values is None:
            return []
        if not isinstance(values, (list, tuple)):
            raise BokError(f"invalid_{field}", f"{field} must be an array")
        result = []
        for value in values[: maximum + 1]:
            cleaned = cls._clean(value, field=field, limit=item_limit)
            if cleaned and cleaned not in result:
                result.append(cleaned)
        if len(result) > maximum:
            raise BokError(f"invalid_{field}", f"{field} exceeds the {maximum} item limit")
        return result

    @staticmethod
    def _claim_id(value: str) -> str:
        claim_id = str(value or "")
        if not re.fullmatch(r"person-[0-9a-f]{32}", claim_id):
            raise BokError("invalid_claim_id", "claim_id is invalid")
        return claim_id

    @staticmethod
    def _quote(value: str) -> str:
        return "\n".join(">" if not line else f"> {line}" for line in str(value).splitlines())

    @staticmethod
    def _section(body: str, title: str) -> str:
        found = re.search(rf"^##\s+{re.escape(title)}\s*$\n(.*?)(?=^##\s+|\Z)", body, flags=re.MULTILINE | re.DOTALL)
        if not found:
            return ""
        lines = []
        for line in found.group(1).strip().splitlines():
            if line == ">":
                lines.append("")
            elif line.startswith("> "):
                lines.append(line[2:])
            else:
                lines.append(line)
        return "\n".join(lines).strip()

    def _render(self, record: dict) -> str:
        frontmatter = render_frontmatter(
            {
                "id": record["id"],
                "type": "personal-claim",
                "title": record["statement"][:80],
                "claim_type": record["claim_type"],
                "epistemic_status": record["epistemic_status"],
                "scope_kind": record["scope_kind"],
                "scope_value": record["scope_value"] or None,
                "confidence": round(float(record["confidence"]), 4),
                "importance": record["importance"],
                "sensitivity": record["sensitivity"],
                "access_scope": record["access_scope"],
                "support_count": int(record["support_count"]),
                "contradiction_count": int(record["contradiction_count"]),
                "source_refs": record["source_refs"],
                "contradiction_refs": record["contradiction_refs"],
                "statement_history": record["statement_history"],
                "first_seen": record["first_seen"],
                "last_seen": record["last_seen"],
                "valid_from": record["valid_from"],
                "valid_to": record["valid_to"] or None,
                "expires_at": record["expires_at"] or None,
                "confirmed_by_user": bool(record["confirmed_by_user"]),
                "supersedes": record["supersedes"] or None,
                "superseded_by": record["superseded_by"] or None,
                "positive_outcomes": record["positive_outcomes"],
                "negative_outcomes": record["negative_outcomes"],
                "last_used": record["last_used"] or None,
                "last_influenced_answer": record["last_influenced_answer"] or None,
                "version": int(record["version"]),
                "created": record["created"],
                "updated": record["updated"],
            }
        )
        history = str(record.get("_history", "")).strip()
        return (
            f"{frontmatter}# Personal Claim\n\n"
            f"## 主张\n\n{self._quote(record['statement'])}\n\n"
            f"## 适用范围\n\n- 类型：{record['scope_kind']}\n"
            f"- 值：{record['scope_value'] or '全局'}\n\n"
            f"## 来源\n\n"
            + ("\n".join(f"- {item}" for item in record["source_refs"]) or "- 无")
            + "\n\n## 更新记录\n\n"
            + (self._quote(history) if history else "> 无")
            + "\n"
        )

    def _record(self, text: str, *, path: Path) -> dict:
        frontmatter, body = parse_frontmatter(text)
        if str(frontmatter.get("type", "")) != "personal-claim":
            raise BokError("invalid_personal_claim", "Personal claim Markdown has an invalid type", status=500)
        statement = self._section(body, "主张")
        claim_id = self._claim_id(str(frontmatter.get("id", "")))
        if not statement:
            raise BokError("invalid_personal_claim", "Personal claim has no statement", status=500)
        if path.stem != claim_id:
            raise BokError("invalid_personal_claim", "Personal claim filename does not match its identifier", status=500)
        record = {
            "id": claim_id,
            "path": f"Claims/{path.name}",
            "statement": statement,
            "claim_type": str(frontmatter.get("claim_type", "")),
            "epistemic_status": str(frontmatter.get("epistemic_status", "")),
            "scope_kind": str(frontmatter.get("scope_kind", "global")),
            "scope_value": str(frontmatter.get("scope_value") or ""),
            "confidence": _float(frontmatter.get("confidence"), -1.0),
            "importance": str(frontmatter.get("importance", "")),
            "sensitivity": str(frontmatter.get("sensitivity", "private")),
            "access_scope": list_value(frontmatter.get("access_scope")),
            "support_count": safe_int(frontmatter.get("support_count")),
            "contradiction_count": safe_int(frontmatter.get("contradiction_count")),
            "source_refs": list_value(frontmatter.get("source_refs")),
            "contradiction_refs": list_value(frontmatter.get("contradiction_refs")),
            "statement_history": self._clean_list(
                list_value(frontmatter.get("statement_history")),
                field="statement_history",
                item_limit=1000,
                maximum=20,
            ),
            "first_seen": str(frontmatter.get("first_seen", "")),
            "last_seen": str(frontmatter.get("last_seen", "")),
            "valid_from": str(frontmatter.get("valid_from", "")),
            "valid_to": str(frontmatter.get("valid_to") or ""),
            "expires_at": str(frontmatter.get("expires_at") or ""),
            "confirmed_by_user": _bool(frontmatter.get("confirmed_by_user")),
            "supersedes": str(frontmatter.get("supersedes") or ""),
            "superseded_by": str(frontmatter.get("superseded_by") or ""),
            "positive_outcomes": list_value(frontmatter.get("positive_outcomes")),
            "negative_outcomes": list_value(frontmatter.get("negative_outcomes")),
            "last_used": str(frontmatter.get("last_used") or ""),
            "last_influenced_answer": str(frontmatter.get("last_influenced_answer") or ""),
            "version": safe_int(frontmatter.get("version"), 1),
            "created": str(frontmatter.get("created", "")),
            "updated": str(frontmatter.get("updated", "")),
            "_history": self._section(body, "更新记录"),
        }
        validated = self._validate_claim_values(
            statement=record["statement"],
            claim_type=record["claim_type"],
            scope_kind=record["scope_kind"],
            scope_value=record["scope_value"],
            confidence=record["confidence"],
            sensitivity=record["sensitivity"],
            access_scope=record["access_scope"],
            source_refs=record["source_refs"],
            expires_at=record["expires_at"],
        )
        record.update(validated)
        if record["epistemic_status"] not in EPISTEMIC_STATUSES:
            raise BokError("invalid_personal_claim", "Personal claim has an invalid epistemic status", status=500)
        if record["epistemic_status"] == "confirmed" and not record["confirmed_by_user"]:
            raise BokError("invalid_personal_claim", "Confirmed personal claims require user confirmation", status=500)
        if record["epistemic_status"] == "superseded" and not record["superseded_by"]:
            raise BokError("invalid_personal_claim", "Superseded personal claims require a successor", status=500)
        if record["superseded_by"] and record["epistemic_status"] != "superseded":
            raise BokError("invalid_personal_claim", "A personal claim with a successor must be superseded", status=500)
        for link in (record["supersedes"], record["superseded_by"]):
            if link:
                self._claim_id(link)
        for field in ("first_seen", "last_seen", "valid_from", "created", "updated"):
            if self._parse_time(record[field]) is None:
                raise BokError("invalid_personal_claim", f"Personal claim has an invalid {field} timestamp", status=500)
        if record["valid_to"] and self._parse_time(record["valid_to"]) is None:
            raise BokError("invalid_personal_claim", "Personal claim has an invalid valid_to timestamp", status=500)
        if record["version"] < 1 or record["support_count"] < 0 or record["contradiction_count"] < 0:
            raise BokError("invalid_personal_claim", "Personal claim counters are invalid", status=500)
        record["importance"] = "important" if record["claim_type"] in IMPORTANT_CLAIM_TYPES else "ordinary"
        return record

    @staticmethod
    def _parse_time(value: str) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else None
        except (TypeError, ValueError):
            return None

    def _effective_reason(self, record: dict) -> str:
        learned = record["epistemic_status"] == "learned" and record["claim_type"] in QUIET_LEARNABLE_CLAIM_TYPES
        confirmed = record["epistemic_status"] == "confirmed" and record["confirmed_by_user"]
        if not learned and not confirmed:
            return "not_confirmed"
        if record["superseded_by"]:
            return "superseded"
        now = datetime.now(timezone.utc)
        valid_to = self._parse_time(record["valid_to"])
        expires_at = self._parse_time(record["expires_at"])
        if valid_to and valid_to <= now:
            return "validity_ended"
        if expires_at and expires_at <= now:
            return "expired"
        return "active"

    def _public(self, record: dict) -> dict:
        value = {key: item for key, item in record.items() if not key.startswith("_")}
        reason = self._effective_reason(record)
        value["effective"] = reason == "active"
        value["inactive_reason"] = "" if reason == "active" else reason
        return value

    @staticmethod
    def _history(record: dict, action: str, detail: str) -> None:
        line = f"{utc_now()} · {action} · {detail}"
        existing = str(record.get("_history", "")).strip()
        record["_history"] = f"{existing}\n{line}".strip()

    def _validate_claim_values(
        self,
        *,
        statement: str,
        claim_type: str,
        scope_kind: str,
        scope_value: str,
        confidence: float,
        sensitivity: str,
        access_scope,
        source_refs,
        expires_at: str = "",
    ) -> dict:
        statement = self._clean(statement, field="statement", limit=1000, required=True)
        claim_type = self._clean(claim_type, field="claim_type", limit=40, required=True).casefold()
        scope_kind = self._clean(scope_kind, field="scope_kind", limit=30, required=True).casefold()
        scope_value = self._clean(scope_value, field="scope_value", limit=240)
        sensitivity = self._clean(sensitivity, field="sensitivity", limit=20, required=True).casefold()
        if claim_type not in CLAIM_TYPES:
            raise BokError("invalid_claim_type", "claim_type is not supported")
        if scope_kind not in SCOPE_KINDS:
            raise BokError("invalid_scope_kind", "scope_kind is not supported")
        if scope_kind != "global" and not scope_value:
            raise BokError("invalid_scope_value", "scope_value is required for non-global claims")
        if scope_kind == "global":
            scope_value = ""
        if sensitivity not in SENSITIVITIES:
            raise BokError("invalid_sensitivity", "sensitivity is not supported")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as error:
            raise BokError("invalid_confidence", "confidence must be a number between 0 and 1") from error
        if not 0 <= confidence <= 1:
            raise BokError("invalid_confidence", "confidence must be a number between 0 and 1")
        sources = self._clean_list(source_refs, field="source_refs", maximum=50)
        if not sources:
            raise BokError("missing_source_refs", "At least one source reference is required")
        access = self._clean_list(access_scope or ["personal-core"], field="access_scope", item_limit=320, maximum=20)
        allowed_access = []
        for value in access:
            if value in {"personal-core", "all-agents"}:
                allowed_access.append(value)
            elif value.startswith("agent:") and value.removeprefix("agent:").strip():
                allowed_access.append(value)
            elif value.startswith("project:") and value.removeprefix("project:").strip():
                allowed_access.append(value)
        if len(allowed_access) != len(access):
            raise BokError("invalid_access_scope", "access_scope contains an unsupported value")
        expires_at = self._clean(expires_at, field="expires_at", limit=40)
        if expires_at and self._parse_time(expires_at) is None:
            raise BokError("invalid_expires_at", "expires_at must be an ISO-8601 timestamp")
        return {
            "statement": statement,
            "claim_type": claim_type,
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "confidence": confidence,
            "sensitivity": sensitivity,
            "access_scope": access,
            "source_refs": sources,
            "expires_at": expires_at,
        }
