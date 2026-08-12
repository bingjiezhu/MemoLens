#!/usr/bin/env python3
"""Minimal stdio MCP server for MemoLens, implemented with Python stdlib."""

from __future__ import annotations

import json
import sys
from typing import Any

from memolens_core import MemoLensError, MemoLensGateway, json_ready


SERVER_INFO = {"name": "memolens-local", "version": "0.2.0"}
PROTOCOL_VERSION = "2025-06-18"


def _schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _output_schema(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


COMMON_OUTPUT = _output_schema(
    {
        "object": {"type": "string"},
        "status": {"type": "string"},
        "source": {"type": "string"},
    },
    ["object", "status"],
)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "memolens_status",
        "title": "MemoLens status",
        "description": (
            "Report the configured photo index through read-only SQLite without contacting "
            "localhost. Also reports whether the user explicitly enabled local API trust."
        ),
        "inputSchema": _schema(),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "MemoLens status",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_search",
        "title": "Search local photos",
        "description": (
            "Search the local photo index. Safe-default mode uses read-only lexical SQLite "
            "search and returns traversal-checked paths without contacting localhost."
        ),
        "inputSchema": _schema(
            {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural-language description of desired photos.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 36,
                    "default": 12,
                },
            },
            ["query"],
        ),
        "outputSchema": _output_schema(
            {
                "object": {"const": "memolens.search"},
                "status": {"type": "string"},
                "source": {"type": "string"},
                "query": {"type": "string"},
                "result_count": {"type": "integer"},
                "results": {"type": "array", "items": {"type": "object"}},
            },
            ["object", "status", "source", "query", "result_count", "results"],
        ),
        "annotations": {
            "title": "Search local photos",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_memories",
        "title": "Explore photo memories",
        "description": (
            "Read event and theme clusters from the local MemoLens Atlas for album and "
            "story planning. Requires the user's explicit unauthenticated local-API opt-in."
        ),
        "inputSchema": _schema(
            {
                "query": {
                    "type": "string",
                    "description": "Optional text used to narrow the memory view.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 24,
                    "default": 8,
                },
            }
        ),
        "outputSchema": _output_schema(
            {
                "object": {"const": "memolens.memories"},
                "status": {"type": "string"},
                "source": {"const": "local_api"},
                "memory_count": {"type": "integer"},
                "memories": {"type": "array", "items": {"type": "object"}},
            },
            ["object", "status", "source", "memory_count", "memories"],
        ),
        "annotations": {
            "title": "Explore photo memories",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_cleanup",
        "title": "Review cleanup candidates",
        "description": (
            "Return a read-only report of duplicate, similar, low-quality, and incomplete-"
            "metadata candidates. Requires explicit local-API trust and never changes a photo."
        ),
        "inputSchema": _schema(),
        "outputSchema": _output_schema(
            {
                "object": {"const": "memolens.cleanup_report"},
                "status": {"type": "string"},
                "source": {"const": "local_api"},
                "read_only": {"const": True},
                "counts": {"type": "object"},
                "stacks": {"type": "array", "items": {"type": "object"}},
            },
            ["object", "status", "source", "read_only", "counts", "stacks"],
        ),
        "annotations": {
            "title": "Review cleanup candidates",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]


def _arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MemoLensError("Tool arguments must be an object.", code="invalid_argument")
    return value


def call_tool(name: str, arguments: Any, gateway: MemoLensGateway) -> dict[str, Any]:
    args = _arguments(arguments)
    allowed: dict[str, set[str]] = {
        "memolens_status": set(),
        "memolens_search": {"query", "limit"},
        "memolens_memories": {"query", "limit"},
        "memolens_cleanup": set(),
    }
    if name not in allowed:
        raise MemoLensError(f"Unknown tool: {name}", code="unknown_tool")
    unexpected = sorted(set(args) - allowed[name])
    if unexpected:
        raise MemoLensError(
            f"Unexpected argument(s): {', '.join(unexpected)}", code="invalid_argument"
        )
    if name == "memolens_status":
        return gateway.status()
    if name == "memolens_search":
        query = args.get("query")
        if not isinstance(query, str):
            raise MemoLensError("query must be a string.", code="invalid_argument")
        return gateway.search(query, limit=args.get("limit", 12))
    if name == "memolens_memories":
        query = args.get("query")
        if query is not None and not isinstance(query, str):
            raise MemoLensError("query must be a string.", code="invalid_argument")
        return gateway.memories(query=query, limit=args.get("limit", 8))
    return gateway.cleanup()


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_message(message: Any, gateway: MemoLensGateway) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    if "id" not in message:  # Notifications never receive a response.
        return None
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        return _success(
            request_id,
            {
                "protocolVersion": (
                    requested if requested == PROTOCOL_VERSION else PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "All MemoLens tools are local and read-only. The safe default never contacts "
                    "localhost and supports SQLite status/search. Do not enable local API trust "
                    "for the user. Inspect returned absolute_path files with Codex vision."
                ),
            },
        )
    if method == "ping":
        return _success(request_id, {})
    if method == "tools/list":
        return _success(request_id, {"tools": TOOLS})
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "Invalid tool call parameters")
        try:
            structured = json_ready(
                call_tool(params["name"], params.get("arguments"), gateway)
            )
        except MemoLensError as exc:
            detail = {
                "object": "memolens.error",
                "status": "error",
                "error": {"code": exc.code, "message": str(exc)},
            }
            return _success(
                request_id,
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(detail, ensure_ascii=False),
                        }
                    ],
                },
            )
        return _success(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(structured, ensure_ascii=False),
                    }
                ],
                "structuredContent": structured,
            },
        )
    return _error(request_id, -32601, "Method not found")


def main() -> int:
    gateway = MemoLensGateway()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        else:
            try:
                response = handle_message(message, gateway)
            except Exception:
                # Never print traces or logs to stdout: it is reserved for MCP frames.
                response = _error(
                    message.get("id") if isinstance(message, dict) else None,
                    -32603,
                    "Internal error",
                )
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
