from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .api import serve
from .config import BokConfig
from .errors import BokError
from .mcp import MCPServer
from .service import BokService


DEFAULT_VAULT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bok", description="Bok local-first Markdown memory core")
    result.add_argument("--vault", type=Path, default=DEFAULT_VAULT, help="Vault root (defaults to the parent of Bok/)")
    subparsers = result.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize local state and index")
    server = subparsers.add_parser("serve", help="Run the authenticated loopback Memory API")
    server.add_argument("--host", default=None)
    server.add_argument("--port", type=int, default=None)
    subparsers.add_parser("health")
    subparsers.add_parser("token", help="Print the local API token for client configuration")

    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=None)
    search.add_argument("--token-budget", type=int, default=None)
    search.add_argument("--no-semantic", action="store_true")
    search.add_argument("--scope", choices=("default", "all"), default="default")
    context = subparsers.add_parser("context")
    context.add_argument("task")
    context.add_argument("--limit", type=int, default=None)
    context.add_argument("--token-budget", type=int, default=None)
    context.add_argument("--no-semantic", action="store_true")
    context.add_argument("--scope", choices=("default", "all"), default="default")

    capture = subparsers.add_parser("capture")
    capture.add_argument("material")
    capture.add_argument("--source", default="cli")
    process = subparsers.add_parser("process")
    process.add_argument("--limit", type=int, default=3)
    inbox = subparsers.add_parser("inbox")
    inbox.add_argument("--status", default="pending")
    inbox.add_argument("--limit", type=int, default=100)
    commit = subparsers.add_parser("commit")
    commit.add_argument("proposal_id")
    commit.add_argument("--confirm-important", action="store_true")
    reject = subparsers.add_parser("reject")
    reject.add_argument("proposal_id")
    reject.add_argument("--reason", default="")
    rollback = subparsers.add_parser("rollback-memory")
    rollback.add_argument("proposal_id")
    rollback.add_argument("--confirm-important", action="store_true")

    note = subparsers.add_parser("quick-note")
    note.add_argument("text")
    note.add_argument("--source", default="cli")
    resume = subparsers.add_parser("resume")
    resume.add_argument("--path", default="")
    resume.add_argument("--token-budget", type=int, default=None)

    person = subparsers.add_parser("person", help="Manage the physically separate Personal Core")
    person_commands = person.add_subparsers(dest="person_command", required=True)
    person_setup = person_commands.add_parser("setup")
    person_setup.add_argument("path")
    person_setup.add_argument("--confirm", action="store_true")
    person_commands.add_parser("health")
    person_list = person_commands.add_parser("list")
    person_list.add_argument("--status", default="all")
    person_list.add_argument("--type", dest="claim_type", default="")
    person_list.add_argument("--limit", type=int, default=100)
    person_propose = person_commands.add_parser("propose")
    person_propose.add_argument("statement")
    person_propose.add_argument("--type", dest="claim_type", required=True)
    person_propose.add_argument("--scope-kind", default="global")
    person_propose.add_argument("--scope-value", default="")
    person_propose.add_argument("--source", action="append", required=True)
    person_confirm = person_commands.add_parser("confirm")
    person_confirm.add_argument("claim_id")
    person_confirm.add_argument("--source", default="")
    person_authorize = person_commands.add_parser("authorize")
    person_authorize.add_argument("claim_id")
    person_authorize.add_argument("--access", action="append", required=True)
    person_authorize.add_argument("--source", default="")
    person_correct = person_commands.add_parser("correct")
    person_correct.add_argument("claim_id")
    person_correct.add_argument("statement")
    person_correct.add_argument("--source", required=True)
    person_correct.add_argument("--scope-kind", default="")
    person_correct.add_argument("--scope-value", default="")
    person_reject = person_commands.add_parser("reject")
    person_reject.add_argument("claim_id")
    person_reject.add_argument("--reason", required=True)
    person_reject.add_argument("--source", default="")
    person_forget = person_commands.add_parser("forget")
    person_forget.add_argument("claim_id")
    person_forget.add_argument("--confirm-forget", action="store_true")
    person_supersede = person_commands.add_parser("supersede")
    person_supersede.add_argument("claim_id")
    person_supersede.add_argument("statement")
    person_supersede.add_argument("--source", required=True)
    person_supersede.add_argument("--scope-kind", default="")
    person_supersede.add_argument("--scope-value", default="")
    person_explain = person_commands.add_parser("explain")
    person_explain.add_argument("claim_id")
    person_versions = person_commands.add_parser("versions")
    person_versions.add_argument("claim_id")
    person_versions.add_argument("--limit", type=int, default=100)
    person_rollback = person_commands.add_parser("rollback")
    person_rollback.add_argument("version_id")
    person_rollback.add_argument("--confirm-important", action="store_true")
    person_context = person_commands.add_parser("context")
    person_context.add_argument("task")
    person_context.add_argument("--agent", required=True)
    person_context.add_argument("--project", default="")
    person_context.add_argument("--limit", type=int, default=6)
    person_context.add_argument("--token-budget", type=int, default=1500)
    person_observations = person_commands.add_parser("observations")
    person_observations.add_argument("--status", default="all")
    person_observations.add_argument("--limit", type=int, default=100)
    person_process = person_commands.add_parser("process")
    person_process.add_argument("--limit", type=int, default=100)
    person_dashboard = person_commands.add_parser("dashboard")
    person_dashboard.add_argument("--limit", type=int, default=100)
    person_impact = person_commands.add_parser("impact")
    person_impact.add_argument("answer_ref")
    person_impact.add_argument("task")
    person_impact.add_argument("--agent", required=True)
    person_impact.add_argument("--project", default="")
    person_impact.add_argument("--claim", action="append", required=True)
    person_outcome = person_commands.add_parser("outcome")
    person_outcome.add_argument("answer_ref")
    person_outcome.add_argument("outcome", choices=("positive", "negative", "neutral"))
    person_outcome.add_argument("--agent", required=True)
    person_outcome.add_argument("--project", default="")
    person_outcome.add_argument("--claim", action="append", required=True)
    person_outcome.add_argument("--source", required=True)
    person_outcome.add_argument("--rating", type=int, default=0)
    person_outcome.add_argument("--rework", action="store_true")
    person_outcome.add_argument("--note", default="")
    person_cleanup = person_commands.add_parser("cleanup")
    person_cleanup.add_argument("--include-dismissed", action="store_true")
    person_cleanup_action = person_commands.add_parser("cleanup-action")
    person_cleanup_action.add_argument("claim_id")
    person_cleanup_action.add_argument("action", choices=("dismiss", "keep", "expire"))
    person_cleanup_action.add_argument("--confirm-important", action="store_true")
    person_commands.add_parser("backup")
    person_backups = person_commands.add_parser("backups")
    person_backups.add_argument("--limit", type=int, default=100)
    person_verify_backup = person_commands.add_parser("verify-backup")
    person_verify_backup.add_argument("backup_id")
    person_restore_backup = person_commands.add_parser("restore-backup")
    person_restore_backup.add_argument("backup_id")
    person_restore_backup.add_argument("--confirm-personal-core", required=True)
    person_restore_backup.add_argument("--mode", choices=("exact", "merge"), default="exact")

    agents = subparsers.add_parser("agent", help="Manage scoped local Agent credentials")
    agent_commands = agents.add_subparsers(dest="agent_command", required=True)
    agent_issue = agent_commands.add_parser("issue")
    agent_issue.add_argument("agent_id")
    agent_issue.add_argument("--scope", action="append")
    agent_commands.add_parser("list")
    agent_revoke = agent_commands.add_parser("revoke")
    agent_revoke.add_argument("agent_id")

    subparsers.add_parser("backup")
    verify = subparsers.add_parser("verify-backup")
    verify.add_argument("backup_id")
    restore = subparsers.add_parser("restore-backup")
    restore.add_argument("backup_id")
    restore.add_argument("--confirm-vault", required=True)
    restore.add_argument("--mode", choices=("exact", "merge"), default="exact")

    credential = subparsers.add_parser("credential-set")
    credential.add_argument("name")
    subparsers.add_parser("doctor")
    subparsers.add_parser("mcp", help="Run the local MCP stdio server")
    return result


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _run_person(service: BokService, arguments) -> dict:
    command = arguments.person_command
    if command == "setup":
        return service.setup_personal_core(arguments.path, confirm=arguments.confirm)
    if command == "health":
        return service.person_health()
    if command == "list":
        return service.person_claims(status=arguments.status, claim_type=arguments.claim_type, limit=arguments.limit)
    if command == "propose":
        return service.propose_person_claim(
            statement=arguments.statement,
            claim_type=arguments.claim_type,
            scope_kind=arguments.scope_kind,
            scope_value=arguments.scope_value,
            source_refs=arguments.source,
        )
    if command == "confirm":
        return service.confirm_person_claim(arguments.claim_id, source_ref=arguments.source)
    if command == "authorize":
        return service.authorize_person_claim(arguments.claim_id, access_scope=arguments.access, source_ref=arguments.source)
    if command == "correct":
        return service.correct_person_claim(
            arguments.claim_id,
            statement=arguments.statement,
            source_ref=arguments.source,
            scope_kind=arguments.scope_kind,
            scope_value=arguments.scope_value,
        )
    if command == "reject":
        return service.reject_person_claim(arguments.claim_id, reason=arguments.reason, source_ref=arguments.source)
    if command == "forget":
        return service.forget_person_claim(arguments.claim_id, confirm_forget=arguments.confirm_forget)
    if command == "supersede":
        return service.supersede_person_claim(
            arguments.claim_id,
            statement=arguments.statement,
            source_ref=arguments.source,
            scope_kind=arguments.scope_kind,
            scope_value=arguments.scope_value,
        )
    if command == "explain":
        return service.explain_person_claim(arguments.claim_id)
    if command == "versions":
        return service.person_claim_versions(arguments.claim_id, limit=arguments.limit)
    if command == "rollback":
        return service.rollback_person_claim(arguments.version_id, confirm_important=arguments.confirm_important)
    if command == "context":
        return service.person_context(
            task=arguments.task,
            agent=arguments.agent,
            project=arguments.project,
            limit=arguments.limit,
            token_budget=arguments.token_budget,
        )
    if command == "observations":
        return service.person_observations(status=arguments.status, limit=arguments.limit)
    if command == "process":
        return service.process_person_learning(limit=arguments.limit)
    if command == "dashboard":
        return service.person_dashboard(limit=arguments.limit)
    if command == "impact":
        return service.record_person_impact(
            answer_ref=arguments.answer_ref,
            task=arguments.task,
            agent=arguments.agent,
            project=arguments.project,
            claim_ids=arguments.claim,
        )
    if command == "outcome":
        return service.record_person_outcome(
            answer_ref=arguments.answer_ref,
            outcome=arguments.outcome,
            agent=arguments.agent,
            project=arguments.project,
            claim_ids=arguments.claim,
            source_ref=arguments.source,
            rating=arguments.rating,
            rework=arguments.rework,
            note=arguments.note,
        )
    if command == "cleanup":
        return service.person_cleanup_candidates(include_dismissed=arguments.include_dismissed)
    if command == "cleanup-action":
        return service.person_cleanup_action(
            arguments.claim_id,
            action=arguments.action,
            confirm_important=arguments.confirm_important,
        )
    if command == "backup":
        return service.person_backup_create()
    if command == "backups":
        return service.person_backup_list(limit=arguments.limit)
    if command == "verify-backup":
        return service.person_backup_verify(arguments.backup_id)
    if command == "restore-backup":
        return service.person_backup_restore(
            arguments.backup_id,
            confirm_personal_core=arguments.confirm_personal_core,
            mode=arguments.mode,
        )
    raise BokError("unknown_person_command", "Unknown Personal Core command")


def doctor(service: BokService) -> dict:
    health = service.health()
    checks = [
        {"name": "vault", "status": "pass", "detail": str(service.config.vault_root)},
        {"name": "loopback", "status": "pass", "detail": f"{service.config.host}:{service.config.port}"},
        {"name": "markdown_index", "status": "pass" if health["index"]["documents"] else "warn", "detail": health["index"]},
        {"name": "local_only", "status": "pass" if service.config.local_only else "warn", "detail": service.config.local_only},
        {"name": "memory_provider", "status": "pass" if health["provider"]["available"] else "warn", "detail": health["provider"]},
        {
            "name": "personal_core",
            "status": "pass" if health["personal_core"]["ready"] and not health["personal_core"].get("broken_links") and not health["personal_core"].get("corrupt_claims") else "warn",
            "detail": health["personal_core"],
        },
        {
            "name": "personal_backup_directory",
            "status": "pass" if health["personal_core"].get("ready") else "warn",
            "detail": health["personal_core"].get("name", "not configured"),
        },
        {"name": "backup_directory", "status": "pass", "detail": str(service.storage.backups)},
    ]
    return {"status": "pass" if all(item["status"] == "pass" for item in checks) else "attention", "checks": checks}


def run(argv=None) -> int:
    arguments = parser().parse_args(argv)
    overrides = {}
    if arguments.command == "serve":
        overrides = {"host": arguments.host, "port": arguments.port}
    try:
        config = BokConfig.load(arguments.vault, overrides)
        if arguments.command == "serve":
            serve(config)
            return 0
        service = BokService(config)
        service.initialize()
        command = arguments.command
        if command == "init":
            value = service.initialize()
        elif command == "health":
            value = service.health()
        elif command == "token":
            value = {"token": service.auth_token(), "path": str(config.state_dir / "auth-token")}
        elif command == "search":
            value = service.search(arguments.query, limit=arguments.limit, token_budget=arguments.token_budget, semantic=not arguments.no_semantic, scope=arguments.scope)
        elif command == "context":
            value = service.context(arguments.task, limit=arguments.limit, token_budget=arguments.token_budget, semantic=not arguments.no_semantic, scope=arguments.scope)
        elif command == "capture":
            value = service.capture_memory(arguments.material, source={"type": "cli", "ref": arguments.source})
        elif command == "process":
            value = service.process_captures(limit=arguments.limit)
        elif command == "inbox":
            value = service.inbox(status=arguments.status, limit=arguments.limit)
        elif command == "commit":
            value = service.commit_memory(arguments.proposal_id, confirm_important=arguments.confirm_important)
        elif command == "reject":
            value = service.reject_memory(arguments.proposal_id, reason=arguments.reason)
        elif command == "rollback-memory":
            value = service.rollback_memory(arguments.proposal_id, confirm_important=arguments.confirm_important)
        elif command == "quick-note":
            value = service.create_quick_note(arguments.text, source=arguments.source)
        elif command == "resume":
            value = service.project_resume(arguments.path, token_budget=arguments.token_budget)
        elif command == "person":
            value = _run_person(service, arguments)
        elif command == "agent":
            if arguments.agent_command == "issue":
                value = service.issue_agent_credential(arguments.agent_id, scopes=arguments.scope)
            elif arguments.agent_command == "list":
                value = service.list_agent_credentials()
            elif arguments.agent_command == "revoke":
                value = service.revoke_agent_credential(arguments.agent_id)
            else:
                raise BokError("unknown_agent_command", "Unknown Agent credential command")
        elif command == "backup":
            value = service.backup_create()
        elif command == "verify-backup":
            value = service.backup_verify(arguments.backup_id)
        elif command == "restore-backup":
            value = service.backup_restore(arguments.backup_id, confirm_vault=arguments.confirm_vault, mode=arguments.mode)
        elif command == "credential-set":
            secret = getpass.getpass("Provider secret: ")
            value = service.set_credential(arguments.name, secret)
        elif command == "doctor":
            value = doctor(service)
        elif command == "mcp":
            MCPServer(service).run()
            return 0
        else:
            raise BokError("unknown_command", "Unknown command")
        _print(value)
        return 0
    except BokError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
