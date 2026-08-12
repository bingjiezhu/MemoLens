#!/usr/bin/env python3
"""Command-line interface for the local-only MemoLens Codex plugin."""

from __future__ import annotations

import argparse
import json
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
            "Read-only local photo search through SQLite. The unauthenticated local API "
            "is disabled unless MEMOLENS_PLUGIN_TRUST_LOCAL_API=1. Outputs JSON."
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
        help="Local photo-library root used only to resolve safe result paths",
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

    memories = subparsers.add_parser(
        "memories", help="List Atlas memories (requires explicit local-API trust)"
    )
    memories.add_argument("--query", default=None, help="Optional memory filter")
    memories.add_argument("--limit", type=int, default=8, help="Memories, 1-24")

    subparsers.add_parser(
        "cleanup",
        help="Report cleanup candidates (requires API trust; never changes files)",
    )
    return parser


def _gateway(args: argparse.Namespace) -> MemoLensGateway:
    return MemoLensGateway(
        base_url=args.base_url,
        db_path=args.db_path,
        library_dir=args.library_dir,
        timeout=args.timeout,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    gateway = _gateway(args)
    if args.command == "status":
        return gateway.status()
    if args.command == "search":
        return gateway.search(args.query, limit=args.limit)
    if args.command == "memories":
        return gateway.memories(query=args.query, limit=args.limit)
    if args.command == "cleanup":
        return gateway.cleanup()
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
