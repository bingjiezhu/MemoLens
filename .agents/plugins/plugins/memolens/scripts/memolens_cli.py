#!/usr/bin/env python3
"""Command-line interface for the local-only MemoLens Codex plugin."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from memolens_agent import AgentPairingWorkflow
from memolens_agent_client import PAIRING_ACTIONS
from memolens_creative_blueprint import (
    BLUEPRINT_TRANSPORT_JSON_LIMITS,
    decode_blueprint_candidate,
)
from memolens_core import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    MemoLensError,
    MemoLensGateway,
    json_ready,
)
from memolens_library_bootstrap import (
    library_bootstrap_status,
    start_library_bootstrap,
)
from memolens_strict_json import StrictJsonError, decode_strict_json


_AGENT_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class MemoLensArgumentParser(argparse.ArgumentParser):
    """Turn command-line contract errors into the same JSON error surface."""

    def error(self, message: str) -> None:
        raise MemoLensError(
            "Command arguments are invalid or unsupported.", code="invalid_argument"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = MemoLensArgumentParser(
        prog="memolens",
        description=(
            "Read confirmed Creator Memory, Media Inbox, live Media Wiki, cross-Agent "
            "project resume, Creative Blueprint candidate preflight, local media search, and "
            "unsaved Timeline 1.0 drafting. A native-approved short-lived pairing can also "
            "submit reversible Blueprint proposals. The "
            "unauthenticated local API is disabled unless "
            "MEMOLENS_PLUGIN_TRUST_LOCAL_API=1 and never grants write access; paired writes "
            "use a separate proof protocol. Outputs JSON."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            f"Loopback MemoLens URL (default: MEMOLENS_BASE_URL or {DEFAULT_BASE_URL}); "
            "read-only API use still requires explicit trust, while Agent pairing is an "
            "independent native-approved protocol"
        ),
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="Local MemoLens SQLite path for read-only offline fallback",
    )
    parser.add_argument(
        "--library",
        dest="library_dir",
        default=None,
        help="Legacy photo-library root used only to resolve safe photo-search paths",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Local HTTP timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "status",
        help="Report safe-default read-only SQLite and optional local-API status",
    )

    library_bootstrap_start = subparsers.add_parser(
        "library-bootstrap-start",
        help=(
            "Queue a path-free request for MemoLens native Library selection; "
            "does not contact the backend or create a Library database or project"
        ),
    )
    library_bootstrap_start.add_argument(
        "--request-idempotency-key",
        required=True,
        help="Stable 1-200 character retry key; the raw key is never persisted",
    )

    library_bootstrap_status_parser = subparsers.add_parser(
        "library-bootstrap-status",
        help="Read the path-free state of one queued native Library request",
    )
    library_bootstrap_status_parser.add_argument(
        "request_id", help="Opaque request ID returned by library-bootstrap-start"
    )

    subparsers.add_parser(
        "creator-context",
        help="Read the latest confirmed Creator Memory profile revision",
    )

    search = subparsers.add_parser(
        "search", help="Search indexed photos using natural language"
    )
    search.add_argument("query", help="Natural-language photo request")
    search.add_argument("--limit", type=int, default=12, help="Results, 1-36")

    mixed_search = subparsers.add_parser(
        "mixed-search",
        help="Search photos and current video segments in one ranked result set",
    )
    mixed_search.add_argument("query", help="Natural-language media request")
    mixed_search.add_argument("--limit", type=int, default=12, help="Results, 1-36")

    video_search = subparsers.add_parser(
        "video-search",
        help="Search current video segments through read-only SQLite",
    )
    video_search.add_argument("query", help="Natural-language video request")
    video_search.add_argument("--limit", type=int, default=12, help="Results, 1-36")

    media_list = subparsers.add_parser(
        "media-list", help="List indexed image, video, and audio assets read-only"
    )
    media_list.add_argument(
        "--kind",
        dest="kinds",
        action="append",
        choices=("image", "video", "audio"),
        help="Repeat to select media kinds; defaults to all kinds",
    )
    media_list.add_argument("--limit", type=int, default=24, help="Results, 1-100")
    media_list.add_argument("--cursor", default=None, help="Opaque cursor from a prior page")

    media_get = subparsers.add_parser(
        "media-get", help="Read one indexed media asset and its current video segments"
    )
    media_get.add_argument("asset_id", help="Stable MemoLens asset ID")

    subparsers.add_parser(
        "wiki-status",
        help="Describe live Agent Media Wiki coverage and known gaps",
    )

    wiki_list = subparsers.add_parser(
        "wiki-list",
        help="Browse compact live Wiki asset-page summaries",
    )
    wiki_list.add_argument(
        "--kind",
        dest="kinds",
        action="append",
        choices=("image", "video", "audio"),
        help="Repeat to select media kinds; defaults to all kinds",
    )
    wiki_list.add_argument("--limit", type=int, default=24, help="Pages, 1-100")
    wiki_list.add_argument("--cursor", default=None, help="Opaque live keyset cursor")

    wiki_search = subparsers.add_parser(
        "wiki-search",
        help="Find live Wiki pages and evidence references for a story idea",
    )
    wiki_search.add_argument("query", help="Natural-language media request")
    wiki_search.add_argument("--limit", type=int, default=12, help="Results, 1-36")

    wiki_open = subparsers.add_parser(
        "wiki-open",
        help="Open one memolens:// Library, Asset, or current Span page",
    )
    wiki_open.add_argument("page_id", help="Stable memolens:// Wiki page URI")

    wiki_evidence = subparsers.add_parser(
        "wiki-evidence",
        help="Resolve one memolens:// asset or current-span evidence URI",
    )
    wiki_evidence.add_argument(
        "evidence_id", help="Stable memolens:// Wiki evidence URI"
    )

    inbox_list = subparsers.add_parser(
        "inbox-list",
        help="List current non-destructive media review state read-only",
    )
    inbox_list.add_argument(
        "--state",
        choices=("inbox", "kept", "archived", "all"),
        default="inbox",
    )
    inbox_list.add_argument(
        "--kind",
        dest="kinds",
        action="append",
        choices=("image", "video", "audio"),
    )
    inbox_list.add_argument("--limit", type=int, default=24, help="Results, 1-100")
    inbox_list.add_argument("--cursor", default=None, help="Opaque cursor from a prior page")

    memories = subparsers.add_parser(
        "memories", help="List Atlas memories (requires explicit local-API trust)"
    )
    memories.add_argument("--query", default=None, help="Optional memory filter")
    memories.add_argument("--limit", type=int, default=8, help="Memories, 1-24")

    subparsers.add_parser(
        "cleanup",
        help="Report cleanup candidates (requires API trust; never changes files)",
    )

    timeline_draft = subparsers.add_parser(
        "timeline-draft",
        help="Build an unsaved Timeline 1.0 draft from JSON without filesystem writes",
    )
    timeline_draft.add_argument(
        "--input", required=True, help="JSON request file, or - to read stdin"
    )

    timeline_revise = subparsers.add_parser(
        "timeline-revise-draft",
        help="Apply typed operations to an unsaved timeline draft in memory",
    )
    timeline_revise.add_argument(
        "--input", required=True, help="JSON request file, or - to read stdin"
    )

    timeline_validate = subparsers.add_parser(
        "timeline-validate", help="Validate Timeline 1.0 JSON without saving or rendering"
    )
    timeline_validate.add_argument(
        "--input", required=True, help="Timeline JSON file, or - to read stdin"
    )

    timeline_list = subparsers.add_parser(
        "timeline-list", help="List latest persisted timeline revisions read-only"
    )
    timeline_list.add_argument("--project-id", default=None)
    timeline_list.add_argument("--limit", type=int, default=24, help="Results, 1-100")
    timeline_list.add_argument("--cursor", default=None, help="Opaque cursor from a prior page")

    timeline_get = subparsers.add_parser(
        "timeline-get", help="Read an immutable persisted timeline revision"
    )
    timeline_get.add_argument("timeline_id")
    timeline_get.add_argument("--revision", type=int, default=None)

    project_list = subparsers.add_parser(
        "project-list",
        help="List resumable creative projects through read-only SQLite",
    )
    project_list.add_argument(
        "--status",
        choices=("current", "draft", "active", "archived", "all"),
        default="current",
        help="Project state filter; current includes draft and active",
    )
    project_list.add_argument("--limit", type=int, default=24, help="Results, 1-100")
    project_list.add_argument(
        "--cursor", default=None, help="Opaque cursor from a prior page"
    )

    project_open = subparsers.add_parser(
        "project-open",
        help="Open a bounded cross-Agent project resume capsule",
    )
    project_open.add_argument("project_id", help="Stable MemoLens project ID")

    project_history = subparsers.add_parser(
        "project-history",
        help="Read bounded persisted timeline revision summaries for a project",
    )
    project_history.add_argument("project_id", help="Stable MemoLens project ID")
    project_history.add_argument(
        "--limit", type=int, default=50, help="Revision summaries, 1-100"
    )

    blueprint_shadow = subparsers.add_parser(
        "blueprint-shadow",
        help="Project the latest legacy brief into a non-authoritative Blueprint candidate",
    )
    blueprint_shadow.add_argument("project_id", help="Stable MemoLens project ID")

    blueprint_get = subparsers.add_parser(
        "blueprint-get",
        help="Read the canonical persisted Blueprint head or one exact revision",
    )
    blueprint_get.add_argument("project_id", help="Stable MemoLens project ID")
    blueprint_get.add_argument(
        "--revision", type=int, default=None, help="Exact immutable revision"
    )

    blueprint_history = subparsers.add_parser(
        "blueprint-history",
        help="Read the bounded Blueprint-only operation ledger",
    )
    blueprint_history.add_argument("project_id", help="Stable MemoLens project ID")
    blueprint_history.add_argument(
        "--limit", type=int, default=50, help="Operation summaries, 1-100"
    )

    blueprint_validate = subparsers.add_parser(
        "blueprint-validate",
        help="Strictly validate a Creative Blueprint candidate without persisting it",
    )
    blueprint_validate.add_argument(
        "--input", required=True, help="Candidate JSON file, or - to read stdin"
    )

    agent_pair = subparsers.add_parser(
        "agent-pair",
        help="Request a short-lived native-approved capability for one existing Blueprint project",
    )
    agent_pair.add_argument("project_id", help="Existing Creative Blueprint project ID")
    agent_pair.add_argument(
        "--client-label",
        default="Generic Agent CLI",
        help="Unverified display label shown in the native pairing review",
    )
    agent_pair.add_argument(
        "--ttl-seconds", type=int, default=900, help="Requested lifetime, 60-1800 seconds"
    )
    agent_pair.add_argument(
        "--max-operations", type=int, default=20, help="Requested write count, 1-32"
    )
    agent_pair.add_argument(
        "--action",
        dest="actions",
        action="append",
        choices=PAIRING_ACTIONS,
        help="Repeat to narrow scope; defaults to proposal commit and restore",
    )

    agent_pair_status = subparsers.add_parser(
        "agent-pair-status",
        help="Refresh the native review or active capability status without exposing its proof",
    )
    agent_pair_status.add_argument("project_id", help="Paired Creative Blueprint project ID")

    agent_capability_status = subparsers.add_parser(
        "agent-capability-status",
        help="Report whether the stored project capability is ready, expired, denied, or revoked",
    )
    agent_capability_status.add_argument(
        "project_id", help="Paired Creative Blueprint project ID"
    )

    blueprint_commit = subparsers.add_parser(
        "blueprint-commit",
        help="Submit one exact-CAS unverified Blueprint proposal through an active pairing",
    )
    blueprint_commit.add_argument("project_id", help="Paired Creative Blueprint project ID")
    blueprint_commit.add_argument(
        "--input", required=True, help="B1 commit command JSON file, or - for stdin"
    )
    blueprint_commit.add_argument(
        "--idempotency-key", required=True, help="Stable retry key for this exact command"
    )

    blueprint_restore = subparsers.add_parser(
        "blueprint-restore",
        help="Restore semantic content as a new unverified revision through an active pairing",
    )
    blueprint_restore.add_argument("project_id", help="Paired Creative Blueprint project ID")
    blueprint_restore.add_argument(
        "--input", required=True, help="B1 restore command JSON file, or - for stdin"
    )
    blueprint_restore.add_argument(
        "--idempotency-key", required=True, help="Stable retry key for this exact command"
    )
    return parser


def _gateway(args: argparse.Namespace) -> MemoLensGateway:
    return MemoLensGateway(
        base_url=args.base_url,
        db_path=args.db_path,
        library_dir=args.library_dir,
        timeout=args.timeout,
    )


def _load_json_input(path: str) -> Any:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MemoLensError(
            "Input must be readable UTF-8 JSON.", code="invalid_argument"
        ) from exc


def _load_blueprint_input(path: str) -> dict[str, Any]:
    maximum = BLUEPRINT_TRANSPORT_JSON_LIMITS.max_bytes
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(maximum + 1)
        else:
            with Path(path).open("rb") as handle:
                raw = handle.read(maximum + 1)
    except OSError as exc:
        raise MemoLensError(
            "Blueprint input could not be read.", code="invalid_argument"
        ) from exc
    try:
        return decode_blueprint_candidate(raw)
    except StrictJsonError as exc:
        raise MemoLensError(
            "Blueprint input is not strict bounded JSON.", code="invalid_argument"
        ) from exc


def _load_blueprint_command_input(path: str, *, command: str) -> dict[str, Any]:
    maximum = BLUEPRINT_TRANSPORT_JSON_LIMITS.max_bytes
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(maximum + 1)
        else:
            with Path(path).open("rb") as handle:
                raw = handle.read(maximum + 1)
    except OSError as exc:
        raise MemoLensError(
            "Blueprint command input could not be read.", code="invalid_argument"
        ) from exc
    try:
        payload = decode_strict_json(
            raw,
            require_object=True,
            limits=BLUEPRINT_TRANSPORT_JSON_LIMITS,
        )
    except StrictJsonError as exc:
        raise MemoLensError(
            "Blueprint command input is not strict bounded JSON.",
            code="invalid_argument",
        ) from exc
    required = (
        {"expected_head", "initial_legacy_brief", "source_candidate_sha256", "semantic"}
        if command == "commit"
        else {"expected_head", "restore_from"}
    )
    if type(payload) is not dict or set(payload) != required:
        raise MemoLensError(
            "Blueprint command input has an unsupported shape.",
            code="invalid_argument",
        )
    return payload


def _agent_workflow(args: argparse.Namespace) -> AgentPairingWorkflow:
    return AgentPairingWorkflow(base_url=args.base_url, timeout=args.timeout)


def _run_agent_command(args: argparse.Namespace) -> dict[str, Any]:
    workflow = _agent_workflow(args)
    if args.command == "agent-pair":
        read = _gateway(args).blueprint_get(args.project_id)
        selection = read.get("selection") if isinstance(read, dict) else None
        current_head = selection.get("current_head") if isinstance(selection, dict) else None
        if not isinstance(current_head, dict):
            raise MemoLensError(
                "The project has no exact Creative Blueprint head to pair.",
                code="blueprint_head_not_found",
            )
        label = str(args.client_label or "").strip()
        if not 1 <= len(label) <= 120:
            raise MemoLensError(
                "Client label must contain 1-120 characters.", code="invalid_argument"
            )
        if not 60 <= args.ttl_seconds <= 1800:
            raise MemoLensError(
                "Pairing lifetime must be between 60 and 1800 seconds.",
                code="invalid_argument",
            )
        if not 1 <= args.max_operations <= 32:
            raise MemoLensError(
                "Pairing operation count must be between 1 and 32.",
                code="invalid_argument",
            )
        return workflow.create(
            project_id=args.project_id,
            current_head=current_head,
            claimed_client_label=label,
            ttl_seconds=args.ttl_seconds,
            max_operations=args.max_operations,
            actions=args.actions,
        )
    if args.command in {"agent-pair-status", "agent-capability-status"}:
        return workflow.status(args.project_id)
    if args.command in {"blueprint-commit", "blueprint-restore"}:
        command = "commit" if args.command == "blueprint-commit" else "restore"
        if _AGENT_IDEMPOTENCY_KEY.fullmatch(args.idempotency_key) is None:
            raise MemoLensError(
                "Idempotency key must be 1-200 bounded ASCII characters.",
                code="invalid_argument",
            )
        return workflow.write(
            command=command,
            project_id=args.project_id,
            body=_load_blueprint_command_input(args.input, command=command),
            idempotency_key=args.idempotency_key,
        )
    raise MemoLensError("Unknown Agent command.", code="invalid_argument")


def _run_blueprint_command(
    args: argparse.Namespace, gateway: MemoLensGateway
) -> dict[str, Any]:
    if args.command == "blueprint-shadow":
        return gateway.blueprint_shadow(args.project_id)
    if args.command == "blueprint-get":
        return gateway.blueprint_get(args.project_id, revision=args.revision)
    if args.command == "blueprint-history":
        return gateway.blueprint_history(args.project_id, limit=args.limit)
    return gateway.blueprint_validate(_load_blueprint_input(args.input))


def _run_library_bootstrap_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.db_path is not None or args.library_dir is not None:
        raise MemoLensError(
            "Library bootstrap does not accept database or Library locators.",
            code="invalid_argument",
        )
    if args.command == "library-bootstrap-start":
        return start_library_bootstrap(args.request_idempotency_key)
    return library_bootstrap_status(args.request_id)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command in {"library-bootstrap-start", "library-bootstrap-status"}:
        return _run_library_bootstrap_command(args)
    if args.command in {
        "agent-pair",
        "agent-pair-status",
        "agent-capability-status",
        "blueprint-commit",
        "blueprint-restore",
    }:
        return _run_agent_command(args)
    gateway = _gateway(args)
    if args.command.startswith("blueprint-"):
        return _run_blueprint_command(args, gateway)
    if args.command == "status":
        return gateway.status()
    if args.command == "creator-context":
        return gateway.creator_context()
    if args.command == "search":
        return gateway.search(args.query, limit=args.limit)
    if args.command == "mixed-search":
        return gateway.mixed_search(args.query, limit=args.limit)
    if args.command == "video-search":
        return gateway.video_search(args.query, limit=args.limit)
    if args.command == "media-list":
        return gateway.media_list(
            kinds=args.kinds, limit=args.limit, cursor=args.cursor
        )
    if args.command == "media-get":
        return gateway.media_get(args.asset_id)
    if args.command == "wiki-status":
        return gateway.wiki_status()
    if args.command == "wiki-list":
        return gateway.wiki_list(
            kinds=args.kinds, limit=args.limit, cursor=args.cursor
        )
    if args.command == "wiki-search":
        return gateway.wiki_search(args.query, limit=args.limit)
    if args.command == "wiki-open":
        return gateway.wiki_open(args.page_id)
    if args.command == "wiki-evidence":
        return gateway.wiki_evidence(args.evidence_id)
    if args.command == "inbox-list":
        return gateway.inbox_list(
            state=args.state,
            kinds=args.kinds,
            limit=args.limit,
            cursor=args.cursor,
        )
    if args.command == "memories":
        return gateway.memories(query=args.query, limit=args.limit)
    if args.command == "cleanup":
        return gateway.cleanup()
    if args.command == "timeline-draft":
        payload = _load_json_input(args.input)
        if not isinstance(payload, dict):
            raise MemoLensError("Draft input must be an object.", code="invalid_argument")
        allowed = {"project_id", "items", "created_at", "format", "brief_revision"}
        if set(payload) - allowed:
            raise MemoLensError("Draft input contains unknown fields.", code="invalid_argument")
        return gateway.timeline_draft(
            project_id=payload.get("project_id"),
            items=payload.get("items"),
            created_at=payload.get("created_at"),
            format_options=payload.get("format"),
            brief_revision=payload.get("brief_revision", 1),
        )
    if args.command == "timeline-revise-draft":
        payload = _load_json_input(args.input)
        if not isinstance(payload, dict) or set(payload) != {
            "timeline",
            "operations",
            "created_at",
        }:
            raise MemoLensError(
                "Revision input requires only timeline, operations, and created_at.",
                code="invalid_argument",
            )
        return gateway.timeline_revise_draft(**payload)
    if args.command == "timeline-validate":
        return gateway.timeline_validate(_load_json_input(args.input))
    if args.command == "timeline-list":
        return gateway.timeline_list(
            project_id=args.project_id,
            limit=args.limit,
            cursor=args.cursor,
        )
    if args.command == "timeline-get":
        return gateway.timeline_get(args.timeline_id, revision=args.revision)
    if args.command == "project-list":
        return gateway.project_list(
            status=args.status,
            limit=args.limit,
            cursor=args.cursor,
        )
    if args.command == "project-open":
        return gateway.project_open(args.project_id)
    if args.command == "project-history":
        return gateway.project_history(args.project_id, limit=args.limit)
    raise MemoLensError("Unknown command.", code="invalid_argument")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = run(args)
    except MemoLensError as exc:
        command = getattr(locals().get("args", None), "command", "")
        project_write_attempted = command in {"blueprint-commit", "blueprint-restore"}
        app_state_write_attempted = command == "library-bootstrap-start"
        result = {
            "object": "memolens.error",
            "status": "error",
            "error": {"code": exc.code, "message": str(exc)},
            "safety": {
                "read_only": not project_write_attempted and not app_state_write_attempted,
                "project_write_attempted": project_write_attempted,
                "app_state_intent_write_attempted": app_state_write_attempted,
                "photos_modified": False,
                "remote_network_allowed": False,
                "arbitrary_path_write_allowed": False,
            },
        }
        print(json.dumps(json_ready(result), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(json_ready(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
