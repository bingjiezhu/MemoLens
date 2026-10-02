#!/usr/bin/env python3
"""Report whether this Python can safely run MemoLens's writable WAL backend."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.sqlite_runtime import (  # noqa: E402
    sqlite_runtime_capability,
    sqlite_runtime_error_message,
)


EX_CONFIG = 78


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check the SQLite WAL runtime used by this Python interpreter."
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--json",
        action="store_true",
        help="emit one bounded JSON capability",
    )
    output.add_argument(
        "--quiet",
        action="store_true",
        help="emit nothing and communicate readiness through the exit code",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    capability = sqlite_runtime_capability()
    is_safe = capability["wal_reset_safe"] is True

    if not args.quiet:
        if args.json:
            print(json.dumps(capability, ensure_ascii=False, sort_keys=True))
        elif is_safe:
            print(
                "MemoLens SQLite runtime ready: "
                f"Python {capability['python_version']}, "
                f"SQLite {capability['sqlite_version']}."
            )
        if not is_safe:
            print(sqlite_runtime_error_message(capability), file=sys.stderr)

    return 0 if is_safe else EX_CONFIG


if __name__ == "__main__":
    if not sys.flags.isolated:
        print(
            "MemoLens SQLite admission must run with isolated Python startup (-I).",
            file=sys.stderr,
        )
        raise SystemExit(EX_CONFIG)
    raise SystemExit(main())
