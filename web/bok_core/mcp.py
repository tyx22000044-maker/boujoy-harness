from __future__ import annotations

import json
import sys
from typing import Callable, Dict

from .errors import BokError
from .person_claim import CLAIM_TYPES, SCOPE_KINDS
from .service import BokService


TOOLS = [
    {
        "name": "bok_search",
        "description": "Search the user's local Bok Markdown memory and return cited paragraph-level results.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}, "token_budget": {"type": "integer", "minimum": 128}, "scope": {"type": "string", "enum": ["default", "all"]}},
            "required": ["query"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Search Bok memory", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_context",
        "description": "Build minimal cited context for a task from the local Bok Vault.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "limit": {"type": "integer"}, "token_budget": {"type": "integer"}, "scope": {"type": "string", "enum": ["default", "all"]}},
            "required": ["task"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Build Bok context", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_project_resume",
        "description": "Resume the focused or specified project with status, decisions, next actions and cited context.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "token_budget": {"type": "integer"}}},
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Resume Bok project", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_person_context",
        "description": "Build a minimal context block from effective Personal Core understanding: low-risk evidence-backed learned claims plus user-confirmed protected claims visible to the declared local agent and project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                "token_budget": {"type": "integer", "minimum": 256},
            },
            "required": ["task", "agent"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Build Bok personal context", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_capture_memory",
        "description": "Queue material for quiet local memory analysis. Returns immediately; important changes remain pending review.",
        "inputSchema": {
            "type": "object",
            "properties": {"material": {"type": "string"}, "source_ref": {"type": "string"}},
            "required": ["material"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Capture Bok memory", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_observe_conversation",
        "description": "Atomically record one conversation turn. personal_signals must be durable third-person interpretations, never copied user wording. Safe low-risk patterns can become quietly learned; identity, authority, sensitive, conflicting and low-confidence judgments still require review. Returns a content-free compact acknowledgement; detailed receipts remain available through the local API.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "role": {"type": "string", "enum": ["user", "assistant", "tool", "system"]},
                "content": {"type": "string"},
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "memory_mode": {"type": "string", "enum": ["default", "session_only", "do_not_remember"]},
                "external_content": {"type": "boolean"},
                "personal_signals": {
                    "type": "array",
                    "maxItems": 8,
                    "description": "Only clear, evidence-backed interpretations. Omit for ordinary turns; never quote or paraphrase the user's command as a profile fact.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "candidate_statement": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 1000,
                                "description": "Third-person durable interpretation beginning with 用户/User/The user; must be semantically abstracted from the source wording.",
                            },
                            "claim_type": {"type": "string", "enum": sorted(CLAIM_TYPES)},
                            "signal_kind": {"type": "string", "enum": ["explicit", "observed"]},
                            "polarity": {"type": "string", "enum": ["support", "contradict"]},
                            "scope_kind": {"type": "string", "enum": sorted(SCOPE_KINDS)},
                            "scope_value": {"type": "string", "maxLength": 240},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "sensitivity": {"type": "string", "enum": ["none", "private", "sensitive"]},
                            "claim_id": {"type": "string"},
                            "concept_key": {
                                "type": "string",
                                "pattern": "^[a-z0-9][a-z0-9._-]{2,95}$",
                                "description": "Stable content-free semantic slot reused for the same behavior or preference across turns, for example memory.semantic-abstraction.",
                            },
                            "inference_basis": {
                                "type": "string",
                                "maxLength": 240,
                                "description": "Short reasoning category such as explicit correction or repeated choice; do not include a source quote.",
                            },
                        },
                        "required": ["candidate_statement", "claim_type", "signal_kind", "polarity", "scope_kind", "confidence", "inference_basis", "concept_key"],
                    },
                },
            },
            "required": ["conversation_id", "turn_id", "role", "content", "agent"],
        },
        "outputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ok": {"type": "boolean"},
                "status": {"type": "string"},
                "personal": {"type": "string"},
                "rejected_signals": {"type": "integer", "minimum": 0},
                "privacy_filtered": {"type": "boolean"},
            },
            "required": ["ok", "status", "personal"],
        },
        "annotations": {"title": "Observe a Bok conversation turn", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_record_person_impact",
        "description": "Record which effective Personal Claims actually influenced an answer without storing the answer body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "answer_ref": {"type": "string"},
                "task": {"type": "string"},
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "claim_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            },
            "required": ["answer_ref", "task", "agent", "claim_ids"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Record Bok personal impact", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_record_person_outcome",
        "description": "Record positive, negative or neutral feedback for the Personal Claims used by an answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "answer_ref": {"type": "string"},
                "outcome": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                "agent": {"type": "string"},
                "project": {"type": "string"},
                "claim_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "source_ref": {"type": "string"},
                "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                "rework": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "required": ["answer_ref", "outcome", "agent", "claim_ids", "source_ref"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Record Bok personal outcome", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "bok_quick_note",
        "description": "Save a frictionless local Markdown quick note to the Bok inbox.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "source": {"type": "string"}}, "required": ["text"]},
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Create Bok quick note", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "bok_memory_inbox",
        "description": "List pending or completed Bok memory proposals without exposing raw captured material.",
        "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "limit": {"type": "integer"}}},
        "outputSchema": {"type": "object"},
        "annotations": {"title": "Review Bok memory inbox", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


class MCPServer:
    SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
    def __init__(self, service: BokService):
        self.service = service
        self.handlers: Dict[str, Callable[[dict], dict]] = {
            "bok_search": lambda value: service.search(value.get("query", ""), limit=value.get("limit"), token_budget=value.get("token_budget"), scope=value.get("scope", "default")),
            "bok_context": lambda value: service.context(value.get("task", ""), limit=value.get("limit"), token_budget=value.get("token_budget"), scope=value.get("scope", "default")),
            "bok_project_resume": lambda value: service.project_resume(value.get("path", ""), token_budget=value.get("token_budget")),
            "bok_person_context": lambda value: service.person_context(
                task=value.get("task", ""),
                agent=value.get("agent", ""),
                project=value.get("project", ""),
                limit=value.get("limit", 6),
                token_budget=value.get("token_budget", 1500),
            ),
            "bok_capture_memory": lambda value: service.capture_memory(value.get("material", ""), source={"type": "mcp", "ref": value.get("source_ref", "")}),
            "bok_observe_conversation": lambda value: service.observe_conversation(
                conversation_id=value.get("conversation_id", ""),
                turn_id=value.get("turn_id", ""),
                role=value.get("role", "user"),
                content=value.get("content", ""),
                memory_mode=value.get("memory_mode", "default"),
                external_content=value.get("external_content") is True,
                client="mcp",
                agent=value.get("agent", ""),
                project=value.get("project", ""),
                personal_signals=value.get("personal_signals") if isinstance(value.get("personal_signals"), list) else None,
            ),
            "bok_record_person_impact": lambda value: service.record_person_impact(
                answer_ref=value.get("answer_ref", ""),
                task=value.get("task", ""),
                agent=value.get("agent", ""),
                project=value.get("project", ""),
                claim_ids=value.get("claim_ids"),
            ),
            "bok_record_person_outcome": lambda value: service.record_person_outcome(
                answer_ref=value.get("answer_ref", ""),
                outcome=value.get("outcome", ""),
                agent=value.get("agent", ""),
                project=value.get("project", ""),
                claim_ids=value.get("claim_ids"),
                source_ref=value.get("source_ref", ""),
                rating=value.get("rating", 0),
                rework=value.get("rework") is True,
                note=value.get("note", ""),
            ),
            "bok_quick_note": lambda value: service.create_quick_note(value.get("text", ""), source=value.get("source", "mcp")),
            "bok_memory_inbox": lambda value: service.inbox(status=value.get("status", "pending"), limit=int(value.get("limit", 100))),
        }

    @staticmethod
    def _write(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _tool_result(name: str, result: dict) -> dict:
        """Keep automatic write acknowledgements out of the model context.

        The durable conversation receipt is still available through the local
        API/status endpoints. MCP callers only need to know whether the turn was
        accepted and whether the structured personal signal path needs attention.
        """
        if name != "bok_observe_conversation":
            return result
        learning = result.get("personal_learning") if isinstance(result.get("personal_learning"), dict) else {}
        compact = {
            "ok": True,
            "status": str(result.get("status", "received")),
            "personal": str(learning.get("status", "no_signal")),
        }
        rejected = learning.get("rejected_signals")
        if isinstance(rejected, list) and rejected:
            compact["rejected_signals"] = len(rejected)
        elif isinstance(rejected, int) and rejected > 0:
            compact["rejected_signals"] = rejected
        if result.get("privacy_filtered") is True:
            compact["privacy_filtered"] = True
        return compact

    def _response(self, request: dict) -> dict:
        request_id = request.get("id")
        method = request.get("method", "")
        if method == "server/discover":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"resultType": "complete", "supportedVersions": self.SUPPORTED_VERSIONS, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "bok-local-memory", "version": self.service.SERVICE_VERSION}, "instructions": "Use read-only search/context/resume first. Capture is non-blocking; important memories remain pending review and are never silently modified."}}
        if method == "initialize":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            requested = str(params.get("protocolVersion", ""))
            selected = requested if requested in self.SUPPORTED_VERSIONS and requested != "2026-07-28" else "2025-11-25"
            return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": selected, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "bok-local-memory", "version": self.service.SERVICE_VERSION}, "instructions": "Use read-only tools first; memory capture is queued and important changes require review."}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS, "ttlMs": 3600000, "cacheScope": "private"}}
        if method == "tools/call":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            name = str(params.get("name", ""))
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            handler = self.handlers.get(name)
            if handler is None:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Unknown Bok tool"}}
            try:
                result = handler(arguments)
                public_result = self._tool_result(name, result)
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(public_result, ensure_ascii=False, separators=(",", ":"))}], "structuredContent": public_result, "isError": False}}
            except BokError as error:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": error.message}], "structuredContent": error.as_dict(), "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}

    def run(self) -> None:
        self.service.initialize()
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request is not an object")
                if request.get("method", "").startswith("notifications/") or "id" not in request:
                    continue
                self._write(self._response(request))
            except (ValueError, TypeError):
                self._write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
