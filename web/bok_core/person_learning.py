from __future__ import annotations

import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable

from .config import BokConfig
from .errors import BokError, ConflictError, NotFoundError, PermissionDeniedError
from .markdown import list_value, parse_frontmatter, render_frontmatter
from .person import PersonalClaimStore
from .person_claim import (
    CLAIM_TYPES,
    QUIET_LEARNABLE_CLAIM_TYPES,
    REVIEW_REQUIRED_CLAIM_TYPES,
    SCOPE_KINDS,
    safe_int,
)
from .util import atomic_write_json, atomic_write_text, read_json, sha256_text, utc_now


OBSERVATION_STATUSES = {"pending", "accumulating", "projected", "excluded_sensitive", "ignored"}
SIGNAL_KINDS = {"explicit", "observed"}
POLARITIES = {"support", "contradict"}
OUTCOMES = {"positive", "negative", "neutral"}


class PersonalLearningStore:
    """Observation, impact, outcome and cleanup layer for Personal Core.

    Conversation text is never copied wholesale. The store keeps only a short,
    validated candidate statement or a content-free exclusion receipt. Formal
    claims remain owned by PersonalClaimStore. Safe, well-supported patterns may
    become locally effective as ``learned`` without being mislabeled as user
    confirmation; protected or conflicting claims still require review.
    """

    def __init__(self, config: BokConfig, claims: PersonalClaimStore):
        self.config = config
        self.claims = claims
        self.lock = claims.lock
        self._cache: Dict[str, tuple] = {}
        self._initialized = False

    @property
    def configured(self) -> bool:
        return self.claims.configured

    @property
    def root(self) -> Path:
        return self.claims._require_root()

    @property
    def observations_dir(self) -> Path:
        return self.root / "Observations"

    @property
    def outcomes_dir(self) -> Path:
        return self.root / "Outcomes"

    @property
    def impacts_dir(self) -> Path:
        return self.root / "Impacts"

    @property
    def decisions_path(self) -> Path:
        return self.root / ".bok" / "cleanup-decisions.json"

    @staticmethod
    def _clean(value, *, field: str, limit: int, required: bool = False) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if required and not text:
            raise BokError(f"invalid_{field}", f"{field} is required")
        if len(text) > limit:
            raise BokError(f"invalid_{field}", f"{field} exceeds the {limit} character limit")
        return text

    @staticmethod
    def _parse_time(value: str):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _redact(value: str) -> str:
        text = str(value or "")
        text = re.sub(r"(?i)\bsk-[a-z0-9_-]{8,}\b", "[secret]", text)
        text = re.sub(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", "[email]", text)
        text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[phone]", text)
        text = re.sub(r"(?i)\b[A-Z]:\\[^\s]+", "[local-path]", text)
        text = re.sub(r"(?<![\w])/(?:Users|home|var|private|tmp)/[^\s]+", "[local-path]", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _looks_sensitive(value: str) -> bool:
        text = str(value or "")
        patterns = (
            r"(?i)\b(?:password|passwd|api[_ -]?key|access[_ -]?token|secret)\b",
            r"(?i)\bsk-[a-z0-9_-]{8,}\b",
            r"身份证|银行卡|支付密码|验证码|助记词|私钥",
            r"(?<!\d)\d{17}[0-9Xx](?!\d)",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _assert_safe(self) -> None:
        self.claims._assert_safe_paths()
        for directory in (self.observations_dir, self.outcomes_dir, self.impacts_dir):
            if directory.is_symlink():
                raise BokError("unsafe_personal_core", "Personal learning directories cannot be symbolic links", status=403)
        if self.decisions_path.is_symlink():
            raise BokError("unsafe_personal_core", "Cleanup decisions cannot be a symbolic link", status=403)

    def initialize(self) -> dict:
        if not self.configured:
            return {"configured": False, "ready": False, "reason": "personal_core_not_configured"}
        self._ensure_initialized()
        promoted = self._promote_existing_safe_claims()
        result = self.health()
        result["upgrade_promoted"] = promoted
        return result

    def _ensure_initialized(self) -> None:
        if not self.configured:
            self.claims._require_root()
        self._assert_safe()
        if self._initialized and all(path.is_dir() for path in (self.observations_dir, self.outcomes_dir, self.impacts_dir)):
            return
        for directory in (self.observations_dir, self.outcomes_dir, self.impacts_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        self._assert_safe()
        self._initialized = True

    @staticmethod
    def _record_path(directory: Path, record_id: str) -> Path:
        return directory / f"{record_id}.md"

    def _render(self, record: dict) -> str:
        record_type = record["type"]
        title = {
            "personal-observation": "Personal Observation",
            "personal-outcome": "Personal Outcome",
            "personal-impact": "Personal Impact",
        }[record_type]
        body_value = record.get("candidate_statement") or record.get("note") or record.get("task_summary") or "无正文"
        frontmatter = render_frontmatter({key: value for key, value in record.items() if key not in {"body"} and value not in (None, "")})
        return f"{frontmatter}# {title}\n\n## 内容\n\n> {body_value}\n"

    def _read(self, path: Path, *, expected_type: str, prefix: str) -> dict:
        if path.is_symlink():
            raise BokError("unsafe_personal_record", "Personal learning records cannot be symbolic links", status=403)
        try:
            frontmatter, _body = parse_frontmatter(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError) as error:
            raise BokError("personal_record_read_failed", "Could not read Personal Core record", status=500) from error
        record_id = str(frontmatter.get("id", ""))
        if str(frontmatter.get("type", "")) != expected_type or not re.fullmatch(rf"{prefix}-[0-9a-f]{{32}}", record_id):
            raise BokError("invalid_personal_record", "Personal Core record type or identifier is invalid", status=500)
        if path.stem != record_id:
            raise BokError("invalid_personal_record", "Personal Core record filename does not match its identifier", status=500)
        record = dict(frontmatter)
        record["id"] = record_id
        record["type"] = expected_type
        for field in ("claim_ids", "source_refs"):
            if field in record:
                record[field] = list_value(record[field])
        for field in ("created_at", "updated_at"):
            if self._parse_time(str(record.get(field, ""))) is None:
                raise BokError("invalid_personal_record", f"Personal Core record has an invalid {field}", status=500)
        return record

    def _all(self, directory: Path, *, expected_type: str, prefix: str) -> list[dict]:
        self._assert_safe()
        paths = sorted(directory.glob(f"{prefix}-*.md"), key=lambda item: item.name)
        signature = []
        for path in paths:
            try:
                stat = path.lstat()
                signature.append((path.name, stat.st_mtime_ns, stat.st_size, path.is_symlink()))
            except OSError:
                signature.append((path.name, 0, 0, True))
        cache_key = str(directory)
        signature_value = tuple(signature)
        cached = self._cache.get(cache_key)
        if cached and cached[0] == signature_value:
            return list(cached[1])
        records = []
        for path in paths:
            try:
                records.append(self._read(path, expected_type=expected_type, prefix=prefix))
            except BokError:
                continue
        records.sort(key=lambda item: (str(item.get("updated_at", "")), item["id"]), reverse=True)
        self._cache[cache_key] = (signature_value, records)
        return list(records)

    def observations(self, *, status: str = "all", limit: int = 100) -> dict:
        records = self._all(self.observations_dir, expected_type="personal-observation", prefix="obs")
        if status != "all":
            if status not in OBSERVATION_STATUSES:
                raise BokError("invalid_observation_status", "Observation status is not supported")
            records = [item for item in records if item.get("status") == status]
        counts: Dict[str, int] = {}
        for item in records:
            key = str(item.get("status", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return {"items": records[: max(1, min(safe_int(limit, 100), 500))], "counts": counts}

    def _write_record(self, directory: Path, record: dict) -> dict:
        self._assert_safe()
        path = self._record_path(directory, record["id"])
        if path.is_symlink():
            raise BokError("unsafe_personal_record", "Personal learning records cannot be symbolic links", status=403)
        atomic_write_text(path, self._render(record))
        self._cache.pop(str(directory), None)
        return dict(record)

    @staticmethod
    def _evidence_key(statement: str, claim_type: str, scope_kind: str, scope_value: str, concept_key: str = "") -> str:
        normalized = f"concept:{concept_key}" if concept_key else re.sub(r"[^\w\u3400-\u9fff]+", "", str(statement).casefold())
        return sha256_text("\n".join((normalized, claim_type, scope_kind, scope_value.casefold())))

    @staticmethod
    def _semantic_text(value: str) -> str:
        return re.sub(r"[^\w\u3400-\u9fff]+", "", str(value or "").casefold())

    @classmethod
    def _signal_quality_reason(cls, candidate: str, source: str, *, scope_kind: str, inference_basis: str) -> str:
        """Reject quotes and task chatter before they enter Personal Core."""
        candidate_text = cls._semantic_text(candidate)
        source_text = cls._semantic_text(source)
        if not re.match(r"^(?:用户|该用户|theuser|user)", candidate_text, flags=re.IGNORECASE):
            return "interpretation_must_be_third_person"
        if len(candidate_text) < 8:
            return "interpretation_too_short"
        basis_text = cls._semantic_text(inference_basis)
        if len(basis_text) < 6:
            return "inference_basis_required"
        if source_text:
            ratio = SequenceMatcher(None, candidate_text, source_text).ratio()
            if candidate_text == source_text or (len(source_text) >= 12 and source_text in candidate_text) or ratio >= 0.72:
                return "verbatim_or_near_verbatim"
        temporary_markers = r"(?:这次|本次|本轮|当前|现在|今天|明天|暂时|先别|先不|先把|这个项目|本项目)"
        if scope_kind == "global" and (re.search(temporary_markers, candidate) or re.search(temporary_markers, source)):
            return "temporary_instruction_cannot_be_global"
        return ""

    def record_observation(
        self,
        *,
        candidate_statement: str,
        claim_type: str,
        source_ref: str,
        signal_kind: str = "observed",
        polarity: str = "support",
        scope_kind: str = "global",
        scope_value: str = "",
        confidence: float = 0.7,
        sensitivity: str = "none",
        conversation_id: str = "",
        turn_id: str = "",
        agent: str = "",
        project: str = "",
        claim_id: str = "",
        occurred_at: str = "",
        source_excerpt: str = "",
        inference_basis: str = "",
        concept_key: str = "",
    ) -> dict:
        self._ensure_initialized()
        source_ref = self._clean(source_ref, field="source_ref", limit=240, required=True)
        claim_type = self._clean(claim_type, field="claim_type", limit=40, required=True).casefold()
        signal_kind = self._clean(signal_kind, field="signal_kind", limit=20, required=True).casefold()
        polarity = self._clean(polarity, field="polarity", limit=20, required=True).casefold()
        scope_kind = self._clean(scope_kind, field="scope_kind", limit=30, required=True).casefold()
        scope_value = self._clean(scope_value, field="scope_value", limit=240)
        candidate_statement = self._clean(self._redact(candidate_statement), field="candidate_statement", limit=1000)
        source_excerpt = self._clean(self._redact(source_excerpt), field="source_excerpt", limit=240)
        inference_basis = self._clean(self._redact(inference_basis), field="inference_basis", limit=240)
        concept_key = self._clean(concept_key, field="concept_key", limit=96).casefold()
        sensitivity = self._clean(sensitivity, field="sensitivity", limit=20, required=True).casefold()
        if claim_type not in CLAIM_TYPES or signal_kind not in SIGNAL_KINDS or polarity not in POLARITIES or scope_kind not in SCOPE_KINDS:
            raise BokError("invalid_personal_observation", "Observation type, signal, polarity or scope is unsupported")
        if scope_kind != "global" and not scope_value:
            raise BokError("invalid_scope_value", "scope_value is required for non-global observations")
        if scope_kind == "global":
            scope_value = ""
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError) as error:
            raise BokError("invalid_confidence", "Observation confidence must be a number") from error
        sensitive = sensitivity in {"private", "sensitive", "high"} or self._looks_sensitive(candidate_statement)
        if not candidate_statement and not sensitive:
            raise BokError("invalid_candidate_statement", "Observation candidate_statement is required")
        if not inference_basis and not sensitive:
            raise BokError("invalid_inference_basis", "A non-sensitive observation requires a short inference basis")
        if not sensitive and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,95}", concept_key):
            raise BokError("invalid_concept_key", "A non-sensitive observation requires a stable lowercase concept_key")
        if claim_id:
            self.claims.get(claim_id)
        occurred_at = occurred_at or utc_now()
        if self._parse_time(occurred_at) is None:
            raise BokError("invalid_occurred_at", "occurred_at must be an ISO-8601 timestamp")
        digest_input = "\n".join((source_ref, candidate_statement, claim_type, scope_kind, scope_value, polarity, claim_id, concept_key))
        record_id = "obs-" + sha256_text(digest_input)[:32]
        path = self._record_path(self.observations_dir, record_id)
        if path.is_file():
            value = self._read(path, expected_type="personal-observation", prefix="obs")
            value["deduplicated"] = True
            return value
        now = utc_now()
        record = {
            "id": record_id,
            "type": "personal-observation",
            "status": "excluded_sensitive" if sensitive else "pending",
            "signal_kind": signal_kind,
            "polarity": polarity,
            "candidate_statement": "" if sensitive else candidate_statement,
            "claim_type": claim_type,
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "confidence": round(confidence, 4),
            "sensitivity": "sensitive" if sensitive else "none",
            "source_ref": source_ref,
            "source_excerpt": "" if sensitive else source_excerpt,
            "inference_basis": "" if sensitive else inference_basis,
            "concept_key": "" if sensitive else concept_key,
            "source_hash": sha256_text(candidate_statement or source_ref),
            "evidence_key": "" if sensitive else self._evidence_key(candidate_statement, claim_type, scope_kind, scope_value, concept_key),
            "conversation_id": self._clean(conversation_id, field="conversation_id", limit=200),
            "turn_id": self._clean(turn_id, field="turn_id", limit=200),
            "agent": self._clean(agent, field="agent", limit=80),
            "project": self._clean(project, field="project", limit=240),
            "claim_id": claim_id,
            "projected_claim_id": "",
            "occurred_at": occurred_at,
            "created_at": now,
            "updated_at": now,
            "interpretation_version": 2,
        }
        return self._write_record(self.observations_dir, record)

    def _extract_turn_signals(self, content: str, *, project: str) -> list[dict]:
        """Only emit a content-free privacy receipt without a model interpretation.

        Personal semantics must come from a model/agent supplied structured signal.
        Deterministic keyword rules previously copied commands into Personal Core and
        are deliberately not used as a substitute for understanding.
        """
        if self._looks_sensitive(content):
            return [{
                "candidate_statement": "",
                "claim_type": "temporary_state",
                "signal_kind": "observed",
                "sensitivity": "sensitive",
                "source_excerpt": "",
            }]
        return []

    def observe_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        content: str,
        agent: str = "",
        project: str = "",
        occurred_at: str = "",
        signals=None,
    ) -> dict:
        if not self.configured:
            return {"status": "disabled", "reason": "personal_core_not_configured", "observations": []}
        values = signals if isinstance(signals, list) else self._extract_turn_signals(content, project=project)
        observations = []
        rejected = []
        for index, signal in enumerate(values[:8]):
            if not isinstance(signal, dict):
                rejected.append({"index": index, "reason": "signal_must_be_an_object"})
                continue
            candidate_statement = signal.get("candidate_statement", signal.get("statement", ""))
            inference_basis = signal.get("inference_basis", "")
            scope_kind = self._clean(signal.get("scope_kind", "global"), field="scope_kind", limit=30, required=True).casefold()
            sensitive = str(signal.get("sensitivity", "none")).casefold() in {"private", "sensitive", "high"} or self._looks_sensitive(content)
            quality_reason = "" if sensitive else self._signal_quality_reason(
                candidate_statement,
                content,
                scope_kind=scope_kind,
                inference_basis=inference_basis,
            )
            if quality_reason:
                rejected.append({"index": index, "reason": quality_reason})
                continue
            observations.append(
                self.record_observation(
                    candidate_statement=candidate_statement,
                    claim_type=signal.get("claim_type", "temporary_state"),
                    source_ref=f"conversation:{conversation_id}:{turn_id}:signal-{index + 1}",
                    signal_kind=signal.get("signal_kind", "observed"),
                    polarity=signal.get("polarity", "support"),
                    scope_kind=scope_kind,
                    scope_value=signal.get("scope_value", ""),
                    confidence=signal.get("confidence", 0.7),
                    sensitivity=signal.get("sensitivity", "none"),
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    agent=agent,
                    project=project,
                    claim_id=signal.get("claim_id", ""),
                    occurred_at=occurred_at or utc_now(),
                    source_excerpt="",
                    inference_basis=inference_basis,
                    concept_key=signal.get("concept_key", ""),
                )
            )
        status = "observed" if observations else "no_signal"
        if rejected:
            status = "observed_with_warnings" if observations else "needs_attention"
        return {
            "status": status,
            "observations": [{"id": item["id"], "status": item["status"]} for item in observations],
            "rejected_signals": rejected,
        }

    def _mark_observations(self, records: Iterable[dict], *, status: str, claim_id: str = "") -> None:
        for record in records:
            if record.get("status") == status and record.get("projected_claim_id", "") == claim_id:
                continue
            record["status"] = status
            record["projected_claim_id"] = claim_id
            record["updated_at"] = utc_now()
            self._write_record(self.observations_dir, record)

    @staticmethod
    def _evidence_span(records: Iterable[dict]) -> dict:
        items = list(records)
        return {
            "conversations": len({str(item.get("conversation_id", "")) for item in items if item.get("conversation_id")}),
            "days": len({str(item.get("occurred_at", ""))[:10] for item in items if item.get("occurred_at")}),
            "contexts": len({
                (str(item.get("project", "")), str(item.get("agent", "")), str(item.get("scope_value", "")))
                for item in items
            }),
        }

    @staticmethod
    def _quiet_learning_ready(records: Iterable[dict], *, direct: bool) -> bool:
        items = list(records)
        if not items:
            return False
        claim_type = str(items[0].get("claim_type", ""))
        if claim_type not in QUIET_LEARNABLE_CLAIM_TYPES:
            return False
        confidences = [float(item.get("confidence", 0)) for item in items]
        if direct:
            return max(confidences) >= 0.9
        span = PersonalLearningStore._evidence_span(items)
        stricter = claim_type in {"decision_pattern", "behavior_hypothesis"}
        minimum_support = 4 if stricter else 3
        minimum_confidence = 0.86 if stricter else 0.8
        return (
            len(items) >= minimum_support
            and span["conversations"] >= 3
            and (span["days"] >= 2 or span["contexts"] >= 2)
            and sum(confidences) / len(confidences) >= minimum_confidence
        )

    @staticmethod
    def _claim_conversation_count(claim: dict) -> int:
        conversations = set()
        for source_ref in claim.get("source_refs", []):
            value = str(source_ref or "")
            if not value.startswith("conversation:"):
                continue
            parts = value.split(":", 2)
            if len(parts) >= 2 and parts[1]:
                conversations.add(parts[1])
        return len(conversations)

    def _promote_existing_safe_claims(self) -> int:
        """Migrate old low-risk review cards to the quiet-learning contract.

        Earlier builds put every explicit preference into the review queue. A
        product upgrade must not leave those cards behind forever. Promotion is
        deliberately narrower than ordinary processing: direct safe claims need
        high confidence; hypotheses still need repeated, cross-conversation
        evidence. Protected, sensitive, rejected or contradicted claims are
        never migrated.
        """
        promoted = 0
        for claim in self.claims._all():
            if claim.get("claim_type") not in QUIET_LEARNABLE_CLAIM_TYPES:
                continue
            if claim.get("epistemic_status") not in {"explicit", "hypothesis", "observed"}:
                continue
            if claim.get("sensitivity") == "sensitive" or int(claim.get("contradiction_count", 0)):
                continue
            status = str(claim.get("epistemic_status", ""))
            confidence = float(claim.get("confidence", 0))
            ready = status == "explicit" and confidence >= 0.9
            if status in {"hypothesis", "observed"}:
                stricter = claim.get("claim_type") in {"decision_pattern", "behavior_hypothesis"}
                minimum_support = 4 if stricter else 3
                minimum_confidence = 0.86 if stricter else 0.8
                first_day = str(claim.get("first_seen", ""))[:10]
                last_day = str(claim.get("last_seen", ""))[:10]
                spans_time = bool(first_day and last_day and first_day != last_day)
                ready = (
                    int(claim.get("support_count", 0)) >= minimum_support
                    and self._claim_conversation_count(claim) >= 3
                    and spans_time
                    and confidence >= minimum_confidence
                )
            if not ready:
                continue
            self.claims.adopt_learned(claim["id"], reason="safe_upgrade_migration")
            promoted += 1
        return promoted

    def process(self, *, limit: int = 100) -> dict:
        if not self.configured:
            return {"processed": 0, "projected": 0, "learned": 0, "accumulating": 0, "remaining": 0}
        with self.lock:
            all_records = self._all(self.observations_dir, expected_type="personal-observation", prefix="obs")
            candidates = [item for item in all_records if item.get("status") in {"pending", "accumulating"}]
            selected_keys = list(dict.fromkeys(str(item.get("evidence_key", "")) for item in candidates if item.get("evidence_key")))[: max(1, min(limit, 500))]
            projected = 0
            learned = 0
            accumulating = 0
            processed_ids = set()
            for evidence_key in selected_keys:
                group = [item for item in all_records if item.get("evidence_key") == evidence_key and item.get("status") in {"pending", "accumulating"}]
                if not group:
                    continue
                direct = [item for item in group if item.get("signal_kind") == "explicit"]
                contradictory = [item for item in group if item.get("polarity") == "contradict" and item.get("claim_id")]
                if contradictory:
                    for claim_id in dict.fromkeys(str(item["claim_id"]) for item in contradictory):
                        refs = [str(item["source_ref"]) for item in contradictory if item.get("claim_id") == claim_id]
                        self.claims.add_evidence(claim_id, contradiction_refs=refs)
                        self._mark_observations([item for item in contradictory if item.get("claim_id") == claim_id], status="projected", claim_id=claim_id)
                        processed_ids.update(item["id"] for item in contradictory if item.get("claim_id") == claim_id)
                        projected += 1
                    # A conflict suspends the existing understanding. Do not use
                    # support from the same batch to immediately re-adopt it.
                    continue
                supporting = [item for item in group if item.get("polarity") == "support"]
                if not supporting:
                    continue
                source_refs = list(dict.fromkeys(str(item["source_ref"]) for item in supporting))[:50]
                representative = direct[0] if direct else supporting[0]
                result = None
                if representative["claim_type"] == "temporary_state":
                    self._mark_observations(supporting, status="ignored")
                    processed_ids.update(item["id"] for item in supporting)
                    continue
                if direct:
                    quiet_ready = self._quiet_learning_ready(supporting, direct=True)
                    requires_review = representative["claim_type"] in REVIEW_REQUIRED_CLAIM_TYPES
                    if quiet_ready or requires_review:
                        result = self.claims.propose_explicit(
                            statement=representative["candidate_statement"],
                            claim_type=representative["claim_type"],
                            scope_kind=representative.get("scope_kind", "global"),
                            scope_value=representative.get("scope_value", ""),
                            confidence=max(float(item.get("confidence", 0)) for item in supporting),
                            sensitivity="private",
                            access_scope=["personal-core"],
                            source_refs=source_refs,
                        )
                    else:
                        self._mark_observations([item for item in supporting if item.get("status") == "pending"], status="accumulating")
                        processed_ids.update(item["id"] for item in supporting)
                        accumulating += len(supporting)
                else:
                    span = self._evidence_span(supporting)
                    stable_enough_for_review = len(supporting) >= 3 and span["conversations"] >= 3 and (span["days"] >= 2 or span["contexts"] >= 2)
                    quiet_ready = self._quiet_learning_ready(supporting, direct=False)
                    requires_review = representative["claim_type"] in REVIEW_REQUIRED_CLAIM_TYPES
                    if quiet_ready or (requires_review and stable_enough_for_review):
                        result = self.claims.propose_hypothesis(
                            statement=representative["candidate_statement"],
                            claim_type=representative["claim_type"],
                            scope_kind=representative.get("scope_kind", "global"),
                            scope_value=representative.get("scope_value", ""),
                            confidence=sum(float(item.get("confidence", 0)) for item in supporting) / len(supporting),
                            sensitivity="private",
                            access_scope=["personal-core"],
                            source_refs=source_refs,
                        )
                    else:
                        self._mark_observations([item for item in supporting if item.get("status") == "pending"], status="accumulating")
                        processed_ids.update(item["id"] for item in supporting)
                        accumulating += len(supporting)
                if result:
                    claim_id = result["id"]
                    self.claims.add_evidence(claim_id, source_refs=source_refs)
                    if self._quiet_learning_ready(supporting, direct=bool(direct)) and not result.get("rejected_guard") and not result.get("historical_guard"):
                        self.claims.adopt_learned(
                            claim_id,
                            reason="explicit_low_risk_signal" if direct else "repeated_cross_context_evidence",
                        )
                        learned += 1
                    self._mark_observations(supporting, status="projected", claim_id=claim_id)
                    processed_ids.update(item["id"] for item in supporting)
                    projected += 1
            learned += self._promote_existing_safe_claims()
            remaining = sum(1 for item in self._all(self.observations_dir, expected_type="personal-observation", prefix="obs") if item.get("status") in {"pending", "accumulating"})
            return {"processed": len(processed_ids), "projected": projected, "learned": learned, "accumulating": accumulating, "remaining": remaining}

    def _claim_ids(self, values, *, agent: str = "", project: str = "", require_visible: bool = False) -> list[str]:
        if not isinstance(values, (list, tuple)):
            raise BokError("invalid_claim_ids", "claim_ids must be an array")
        result = []
        for value in values[:50]:
            claim_id = str(value or "")
            record = self.claims._read(claim_id)
            if require_visible and (
                self.claims._effective_reason(record) != "active"
                or not self.claims._authorized(record, agent=agent, project=project)
            ):
                raise PermissionDeniedError("Agent cannot reference a Personal Claim outside its effective visibility scope")
            if claim_id not in result:
                result.append(claim_id)
        if not result:
            raise BokError("invalid_claim_ids", "At least one claim_id is required")
        return result

    def record_impact(self, *, answer_ref: str, task: str, agent: str, project: str = "", claim_ids=None) -> dict:
        self._ensure_initialized()
        answer_ref = self._clean(answer_ref, field="answer_ref", limit=240, required=True)
        agent = self._clean(agent, field="agent", limit=80, required=True)
        project = self._clean(project, field="project", limit=240)
        claim_ids = self._claim_ids(claim_ids, agent=agent, project=project, require_visible=True)
        digest = sha256_text("\n".join((answer_ref, agent, *claim_ids)))
        record_id = "impact-" + digest[:32]
        path = self._record_path(self.impacts_dir, record_id)
        if path.is_file():
            value = self._read(path, expected_type="personal-impact", prefix="impact")
            value["deduplicated"] = True
            return value
        now = utc_now()
        record = {
            "id": record_id,
            "type": "personal-impact",
            "answer_ref": answer_ref,
            "task_summary": self._clean(self._redact(task), field="task", limit=240, required=True),
            "task_hash": sha256_text(str(task or "")),
            "agent": agent,
            "project": project,
            "claim_ids": claim_ids,
            "created_at": now,
            "updated_at": now,
        }
        return self._write_record(self.impacts_dir, record)

    def record_outcome(
        self,
        *,
        answer_ref: str,
        outcome: str,
        claim_ids,
        source_ref: str,
        agent: str,
        project: str = "",
        rating: int = 0,
        rework: bool = False,
        note: str = "",
    ) -> dict:
        self._ensure_initialized()
        answer_ref = self._clean(answer_ref, field="answer_ref", limit=240, required=True)
        outcome = self._clean(outcome, field="outcome", limit=20, required=True).casefold()
        source_ref = self._clean(source_ref, field="source_ref", limit=240, required=True)
        agent = self._clean(agent, field="agent", limit=80, required=True)
        project = self._clean(project, field="project", limit=240)
        if outcome not in OUTCOMES:
            raise BokError("invalid_outcome", "outcome must be positive, negative or neutral")
        rating = safe_int(rating, 0)
        if rating and not 1 <= rating <= 5:
            raise BokError("invalid_rating", "rating must be 1-5 or omitted")
        claim_ids = self._claim_ids(claim_ids, agent=agent, project=project, require_visible=True)
        note = self._clean(self._redact(note), field="note", limit=500)
        digest = sha256_text("\n".join((answer_ref, source_ref, outcome, *claim_ids)))
        record_id = "outcome-" + digest[:32]
        path = self._record_path(self.outcomes_dir, record_id)
        if path.is_file():
            value = self._read(path, expected_type="personal-outcome", prefix="outcome")
            value["deduplicated"] = True
            return value
        now = utc_now()
        record = {
            "id": record_id,
            "type": "personal-outcome",
            "answer_ref": answer_ref,
            "outcome": outcome,
            "rating": rating,
            "rework": bool(rework),
            "note": note,
            "source_ref": source_ref,
            "agent": agent,
            "project": project,
            "claim_ids": claim_ids,
            "created_at": now,
            "updated_at": now,
        }
        saved = self._write_record(self.outcomes_dir, record)
        for claim_id in claim_ids:
            claim_outcome = "negative" if rework else outcome
            self.claims.record_outcome(claim_id, outcome_id=record_id, outcome=claim_outcome)
        return saved

    def _outcome_counts(self) -> dict:
        result: Dict[str, Dict[str, int]] = {}
        for record in self._all(self.outcomes_dir, expected_type="personal-outcome", prefix="outcome"):
            key = "negative" if record.get("outcome") == "negative" or record.get("rework") is True else str(record.get("outcome", "neutral"))
            for claim_id in list_value(record.get("claim_ids")):
                counts = result.setdefault(claim_id, {"positive": 0, "negative": 0, "neutral": 0})
                counts[key if key in counts else "neutral"] += 1
        return result

    def cleanup_candidates(self, *, include_dismissed: bool = False) -> dict:
        claims = self.claims.list(status="all", limit=10000)["items"]
        outcomes = self._outcome_counts()
        decisions = read_json(self.decisions_path, {})
        decisions = decisions if isinstance(decisions, dict) else {}
        candidates = []
        fingerprints: Dict[str, list[dict]] = {}
        for claim in claims:
            fingerprint = self._evidence_key(claim["statement"], claim["claim_type"], claim["scope_kind"], claim["scope_value"])
            fingerprints.setdefault(fingerprint, []).append(claim)
        duplicate_ids = set()
        for values in fingerprints.values():
            active = [item for item in values if item.get("epistemic_status") not in {"rejected", "superseded", "expired"}]
            if len(active) > 1:
                active.sort(key=lambda item: (item.get("updated", ""), item["id"]), reverse=True)
                duplicate_ids.update(item["id"] for item in active[1:])
        for claim in claims:
            reasons = []
            suggested = "review"
            status = claim["epistemic_status"]
            counts = outcomes.get(claim["id"], {"positive": 0, "negative": 0, "neutral": 0})
            if claim["id"] in duplicate_ids:
                reasons.append("duplicate")
                suggested = "merge_or_expire"
            if any(str(ref).startswith("conversation:") for ref in claim.get("source_refs", [])) and not re.match(
                r"^(?:用户|该用户|The user\b|User\b)", str(claim.get("statement", "")), flags=re.IGNORECASE
            ):
                reasons.append("verbatim_or_uninterpreted")
                suggested = "replace_or_forget"
            if status in {"rejected", "superseded", "expired"}:
                reasons.append(status)
                suggested = "keep_as_history"
            if int(claim.get("contradiction_count", 0)) > 0:
                reasons.append("contradictory_evidence")
                suggested = "review_conflict"
            if counts["negative"] > counts["positive"]:
                reasons.append("negative_outcomes")
                suggested = "review_or_expire"
            updated = self._parse_time(str(claim.get("updated", "")))
            if updated and (datetime.now(timezone.utc) - updated).days >= 180 and not claim.get("last_used"):
                reasons.append("stale_180_days")
                suggested = "review"
            if not reasons:
                continue
            decision = decisions.get(claim["id"])
            if decision and not include_dismissed:
                continue
            candidates.append(
                {
                    "claim_id": claim["id"],
                    "statement": claim["statement"],
                    "claim_type": claim["claim_type"],
                    "status": status,
                    "reasons": reasons,
                    "outcomes": counts,
                    "suggested_action": suggested,
                    "protected": claim.get("importance") == "important",
                    "dismissed": bool(decision),
                }
            )
        return {"items": candidates, "count": len(candidates)}

    def cleanup_action(self, claim_id: str, *, action: str, confirm_important: bool = False) -> dict:
        action = self._clean(action, field="cleanup_action", limit=20, required=True).casefold()
        claim = self.claims.get(claim_id)
        if action in {"dismiss", "keep"}:
            decisions = read_json(self.decisions_path, {})
            decisions = decisions if isinstance(decisions, dict) else {}
            decisions[claim_id] = {"action": action, "at": utc_now(), "claim_version": claim["version"]}
            atomic_write_json(self.decisions_path, decisions)
            return {"claim_id": claim_id, "action": action, "changed": True}
        if action == "expire":
            if claim.get("importance") == "important" and not confirm_important:
                raise PermissionDeniedError("Expiring an important personal claim requires explicit confirmation")
            result = self.claims.expire(claim_id, reason="cleanup", confirm_important=confirm_important)
            return {"claim_id": claim_id, "action": action, "claim": result, "changed": True}
        raise BokError("invalid_cleanup_action", "cleanup action must be dismiss, keep or expire")

    def forget_claim(self, claim_id: str, *, confirm_forget: bool = False) -> dict:
        """Erase a wrong Personal Claim and every local learning record derived from it."""
        if not confirm_forget:
            raise PermissionDeniedError("Forgetting a Personal Claim requires explicit confirmation")
        self._ensure_initialized()
        with self.lock:
            claim = self.claims._read(claim_id)
            source_refs = set(str(item) for item in claim.get("source_refs", []))
            related_paths = []
            source_turns = []
            removed = {"observations": 0, "outcomes": 0, "impacts": 0}

            observations = self._all(self.observations_dir, expected_type="personal-observation", prefix="obs")
            for item in observations:
                related = (
                    item.get("claim_id") == claim_id
                    or item.get("projected_claim_id") == claim_id
                    or str(item.get("source_ref", "")) in source_refs
                )
                if not related:
                    continue
                related_paths.append(f"Observations/{item['id']}.md")
                removed["observations"] += 1
                if item.get("conversation_id") and item.get("turn_id"):
                    source_turns.append({
                        "conversation_id": str(item["conversation_id"]),
                        "turn_id": str(item["turn_id"]),
                    })

            for directory, expected_type, prefix, label in (
                (self.outcomes_dir, "personal-outcome", "outcome", "outcomes"),
                (self.impacts_dir, "personal-impact", "impact", "impacts"),
            ):
                for item in self._all(directory, expected_type=expected_type, prefix=prefix):
                    if claim_id not in list_value(item.get("claim_ids")):
                        continue
                    related_paths.append(f"{directory.name}/{item['id']}.md")
                    removed[label] += 1

            result = self.claims.forget_claim_artifacts(claim_id, related_paths=related_paths)
            decisions = read_json(self.decisions_path, {})
            if isinstance(decisions, dict) and claim_id in decisions:
                decisions.pop(claim_id, None)
                atomic_write_json(self.decisions_path, decisions)
            self._cache.clear()
            result["removed_learning_records"] = removed
            result["source_turns"] = list({(item["conversation_id"], item["turn_id"]): item for item in source_turns}.values())
            return result

    def dashboard(self, *, limit: int = 100) -> dict:
        claim_result = self.claims.list(status="all", limit=max(100, min(safe_int(limit, 100), 500)))
        claims = claim_result["items"]
        review_required = [item for item in claims if item["epistemic_status"] in {"explicit", "observed", "hypothesis", "contradicted"}]
        understanding = [item for item in claims if item.get("effective")]
        groups: Dict[str, list] = {}
        for item in understanding:
            groups.setdefault(item["claim_type"], []).append(item)
        dimensions = (
            ("communication", "沟通方式", {"communication_preference"}),
            ("work", "工作习惯", {"work_preference"}),
            ("decisions", "选择与决策", {"decision_pattern", "authority_rule", "negative_preference"}),
            ("projects", "项目与经历", {"project_experience", "long_term_goal"}),
            ("knowledge", "知识与能力", {"knowledge_claim", "capability_claim"}),
            ("identity", "自我与行为", {"identity", "public_identity", "behavior_hypothesis"}),
        )
        profile = []
        for key, label, claim_types in dimensions:
            matches = [item for item in understanding if item["claim_type"] in claim_types]
            if not matches:
                continue
            matches.sort(key=lambda item: (float(item.get("confidence", 0)), int(item.get("support_count", 0)), item.get("updated", "")), reverse=True)
            profile.append({
                "key": key,
                "label": label,
                "count": len(matches),
                "statements": [item["statement"] for item in matches[:3]],
                "claim_ids": [item["id"] for item in matches[:3]],
                "updated_at": max(str(item.get("updated", "")) for item in matches),
            })
        observations = self._all(self.observations_dir, expected_type="personal-observation", prefix="obs")
        outcomes = self._all(self.outcomes_dir, expected_type="personal-outcome", prefix="outcome")
        impacts = self._all(self.impacts_dir, expected_type="personal-impact", prefix="impact")
        timeline = []
        for item in claims[:50]:
            timeline.append({"at": item["updated"], "kind": "claim", "id": item["id"], "label": item["statement"], "status": item["epistemic_status"]})
        for item in outcomes[:50]:
            timeline.append({"at": item["updated_at"], "kind": "outcome", "id": item["id"], "label": item.get("outcome", ""), "status": "recorded"})
        for item in observations[:50]:
            label = item.get("candidate_statement") or "敏感内容已排除"
            timeline.append({"at": item["updated_at"], "kind": "observation", "id": item["id"], "label": label, "status": item.get("status", "")})
        timeline.sort(key=lambda item: (item["at"], item["id"]), reverse=True)
        return {
            "configured": True,
            "ready": self.health()["ready"],
            "claims": {
                "understanding": understanding,
                "confirmed": understanding,
                "review_required": review_required,
                "pending": review_required,
                "groups": groups,
                "profile": profile,
                "total": sum(claim_result["counts"].values()),
            },
            "observations": {"counts": self._counts(observations, "status"), "recent": observations[:20]},
            "outcomes": {"counts": self._counts(outcomes, "outcome"), "recent": outcomes[:20]},
            "impacts": {"count": len(impacts), "recent": impacts[:20]},
            "cleanup": self.cleanup_candidates(),
            "timeline": timeline[:100],
        }

    @staticmethod
    def _counts(records: Iterable[dict], field: str) -> dict:
        result: Dict[str, int] = {}
        for item in records:
            key = str(item.get(field, "unknown"))
            result[key] = result.get(key, 0) + 1
        return result

    def health(self) -> dict:
        if not self.configured:
            return {"configured": False, "ready": False, "reason": "personal_core_not_configured", "observations": {}, "outcomes": 0, "impacts": 0, "corrupt_records": 0}
        try:
            self._assert_safe()
        except BokError as error:
            return {"configured": True, "ready": False, "reason": error.code, "observations": {}, "outcomes": 0, "impacts": 0, "corrupt_records": 0}
        required = (self.observations_dir, self.outcomes_dir, self.impacts_dir)
        if not all(path.is_dir() for path in required):
            return {"configured": True, "ready": False, "reason": "personal_learning_not_initialized", "observations": {}, "outcomes": 0, "impacts": 0, "corrupt_records": 0}
        observations = self._all(self.observations_dir, expected_type="personal-observation", prefix="obs")
        outcomes = self._all(self.outcomes_dir, expected_type="personal-outcome", prefix="outcome")
        impacts = self._all(self.impacts_dir, expected_type="personal-impact", prefix="impact")
        expected = sum(len(list(path.glob("*.md"))) for path in required)
        corrupt = max(0, expected - len(observations) - len(outcomes) - len(impacts))
        return {
            "configured": True,
            "ready": corrupt == 0,
            "observations": self._counts(observations, "status"),
            "outcomes": len(outcomes),
            "impacts": len(impacts),
            "cleanup_candidates": self.cleanup_candidates()["count"],
            "corrupt_records": corrupt,
        }
