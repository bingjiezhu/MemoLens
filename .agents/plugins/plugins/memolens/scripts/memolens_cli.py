#!/usr/bin/env python3
"""Command-line interface for the local-only MemoLens Codex plugin."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from memolens_core import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    MemoLensError,
    MemoLensGateway,
    json_ready,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memolens",
        description=(
            "Read-only local media search plus unsaved Timeline 1.0 drafting. The "
            "unauthenticated local API is disabled unless "
            "MEMOLENS_PLUGIN_TRUST_LOCAL_API=1 and never grants write access. Outputs JSON."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            f"Loopback MemoLens URL (default: MEMOLENS_BASE_URL or {DEFAULT_BASE_URL}); "
            "ignored unless local API trust is explicitly enabled"
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    gateway = _gateway(args)
    if args.command == "status":
        return gateway.status()
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
    raise MemoLensError("Unknown command.", code="invalid_argument")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except MemoLensError as exc:
        result = {
            "object": "memolens.error",
            "status": "error",
            "error": {"code": exc.code, "message": str(exc)},
            "safety": {
                "read_only": True,
                "photos_modified": False,
                "remote_network_allowed": False,
            },
        }
        print(json.dumps(json_ready(result), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(json_ready(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
