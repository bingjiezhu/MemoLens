#!/usr/bin/env python3
"""Minimal stdio MCP server for MemoLens, implemented with Python stdlib."""

from __future__ import annotations

import json
import sys
from typing import Any

from memolens_contracts import PLUGIN_VERSION
from memolens_core import MemoLensError, MemoLensGateway, json_ready


SERVER_INFO = {"name": "memolens-local", "version": PLUGIN_VERSION}
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
        "name": "memolens_mixed_search",
        "title": "Search local photos and video segments",
        "description": (
            "Run one query across the photo index and current successful video segments, "
            "then return a single deterministic reciprocal-rank-fused result set."
        ),
        "inputSchema": _schema(
            {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Natural-language description of desired media.",
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
                "object": {"const": "memolens.mixed_search"},
                "status": {"type": "string"},
                "source": {"type": "string"},
                "query": {"type": "string"},
                "result_count": {"type": "integer"},
                "results": {"type": "array", "items": {"type": "object"}},
            },
            ["object", "status", "source", "query", "result_count", "results"],
        ),
        "annotations": {
            "title": "Search local photos and video segments",
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
    {
        "name": "memolens_video_search",
        "title": "Search current video segments",
        "description": (
            "Search only the current successful video-analysis head through read-only "
            "SQLite. Returns stable asset, source, segment, and analysis-run references."
        ),
        "inputSchema": _schema(
            {
                "query": {"type": "string", "minLength": 1},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 36,
                    "default": 12,
                },
            },
            ["query"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Search current video segments",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_media_list",
        "title": "List local media",
        "description": (
            "List indexed image, video, and audio metadata through read-only SQLite. "
            "Does not scan folders or return unnecessary absolute paths."
        ),
        "inputSchema": _schema(
            {
                "kinds": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"enum": ["image", "video", "audio"]},
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 24,
                },
                "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
            }
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "List local media",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_media_get",
        "title": "Read local media details",
        "description": (
            "Read one indexed asset and, for video, its current successful segments."
        ),
        "inputSchema": _schema(
            {"asset_id": {"type": "string", "minLength": 1, "maxLength": 200}},
            ["asset_id"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Read local media details",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_draft",
        "title": "Draft an unsaved timeline",
        "description": (
            "Build a deterministic Timeline 1.0 draft in memory. It is not persisted, "
            "rendered, or exported; each source must include its ID and SHA provenance."
        ),
        "inputSchema": _schema(
            {
                "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 500,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"enum": ["video", "image", "audio"]},
                            "asset_id": {"type": "string"},
                            "asset_source_id": {"type": "string"},
                            "asset_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
                            "segment_id": {"type": "string"},
                            "analysis_run_id": {"type": "string"},
                            "analysis_revision": {"type": "integer", "minimum": 1},
                            "source_in_ms": {"type": "integer", "minimum": 0},
                            "source_out_ms": {"type": "integer", "minimum": 1},
                            "timeline_start_ms": {"type": "integer", "minimum": 0},
                            "timeline_duration_ms": {"type": "integer", "minimum": 1},
                            "fit": {"enum": ["contain", "cover", "stretch"]},
                            "crop": {"type": "object"},
                            "volume_db": {"type": "number", "minimum": -60, "maximum": 12},
                            "audio_enabled": {"type": "boolean"},
                            "fade_in_ms": {"type": "integer", "minimum": 0},
                            "fade_out_ms": {"type": "integer", "minimum": 0},
                            "reason": {"type": "string", "maxLength": 500},
                            "match_id": {"type": "string", "maxLength": 200},
                        },
                        "required": [
                            "kind",
                            "asset_id",
                            "asset_source_id",
                            "asset_sha256",
                            "timeline_duration_ms",
                        ],
                        "additionalProperties": False,
                    },
                },
                "created_at": {"type": "string", "minLength": 1},
                "format": {"type": "object"},
                "brief_revision": {"type": "integer", "minimum": 1},
            },
            ["project_id", "items", "created_at"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Draft an unsaved timeline",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_revise_draft",
        "title": "Revise an unsaved timeline draft",
        "description": (
            "Apply strict typed operations to a Timeline 1.0 value in memory only."
        ),
        "inputSchema": _schema(
            {
                "timeline": {"type": "object"},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"type": "object"},
                },
                "created_at": {"type": "string", "minLength": 1},
            },
            ["timeline", "operations", "created_at"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Revise an unsaved timeline draft",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_validate",
        "title": "Validate a timeline",
        "description": (
            "Pure structural Timeline 1.0 validation with no SQLite, file, or network access."
        ),
        "inputSchema": _schema(
            {"timeline": {"type": "object"}}, ["timeline"]
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Validate a timeline",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_list",
        "title": "List persisted timelines",
        "description": "List the latest immutable timeline revisions through read-only SQLite.",
        "inputSchema": _schema(
            {
                "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 24},
                "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
            }
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "List persisted timelines",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_get",
        "title": "Read a persisted timeline",
        "description": "Read one immutable timeline revision through read-only SQLite.",
        "inputSchema": _schema(
            {
                "timeline_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "revision": {"type": "integer", "minimum": 1},
            },
            ["timeline_id"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Read a persisted timeline",
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
        "memolens_mixed_search": {"query", "limit"},
        "memolens_memories": {"query", "limit"},
        "memolens_cleanup": set(),
        "memolens_video_search": {"query", "limit"},
        "memolens_media_list": {"kinds", "limit", "cursor"},
        "memolens_media_get": {"asset_id"},
        "memolens_timeline_draft": {
            "project_id",
            "items",
            "created_at",
            "format",
            "brief_revision",
        },
        "memolens_timeline_revise_draft": {
            "timeline",
            "operations",
            "created_at",
        },
        "memolens_timeline_validate": {"timeline"},
        "memolens_timeline_list": {"project_id", "limit", "cursor"},
        "memolens_timeline_get": {"timeline_id", "revision"},
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
    if name == "memolens_mixed_search":
        query = args.get("query")
        if not isinstance(query, str):
            raise MemoLensError("query must be a string.", code="invalid_argument")
        return gateway.mixed_search(query, limit=args.get("limit", 12))
    if name == "memolens_memories":
        query = args.get("query")
        if query is not None and not isinstance(query, str):
            raise MemoLensError("query must be a string.", code="invalid_argument")
        return gateway.memories(query=query, limit=args.get("limit", 8))
    if name == "memolens_cleanup":
        return gateway.cleanup()
    if name == "memolens_video_search":
        query = args.get("query")
        if not isinstance(query, str):
            raise MemoLensError("query must be a string.", code="invalid_argument")
        return gateway.video_search(query, limit=args.get("limit", 12))
    if name == "memolens_media_list":
        return gateway.media_list(
            kinds=args.get("kinds"),
            limit=args.get("limit", 24),
            cursor=args.get("cursor"),
        )
    if name == "memolens_media_get":
        return gateway.media_get(args.get("asset_id"))
    if name == "memolens_timeline_draft":
        return gateway.timeline_draft(
            project_id=args.get("project_id"),
            items=args.get("items"),
            created_at=args.get("created_at"),
            format_options=args.get("format"),
            brief_revision=args.get("brief_revision", 1),
        )
    if name == "memolens_timeline_revise_draft":
        return gateway.timeline_revise_draft(
            timeline=args.get("timeline"),
            operations=args.get("operations"),
            created_at=args.get("created_at"),
        )
    if name == "memolens_timeline_validate":
        return gateway.timeline_validate(args.get("timeline"))
    if name == "memolens_timeline_list":
        return gateway.timeline_list(
            project_id=args.get("project_id"),
            limit=args.get("limit", 24),
            cursor=args.get("cursor"),
        )
    return gateway.timeline_get(
        args.get("timeline_id"), revision=args.get("revision")
    )


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
                    "localhost and supports SQLite media search/read plus in-memory timeline "
                    "drafting and validation. Do not enable local API trust for the user. "
                    "No save, render, export, delete, or arbitrary-path tool is exposed."
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
