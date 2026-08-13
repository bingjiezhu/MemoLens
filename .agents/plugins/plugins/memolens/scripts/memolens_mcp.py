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


def _exact_output_schema(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


SAFETY_OUTPUT = _exact_output_schema(
    {
        "read_only": {"const": True},
        "photos_opened_by_plugin": {"const": False},
        "photos_modified": {"const": False},
        "media_modified": {"const": False},
        "timeline_persisted": {"const": False},
        "rendered": {"const": False},
        "exported": {"const": False},
        "remote_network_allowed": {"const": False},
    },
    [
        "read_only",
        "photos_opened_by_plugin",
        "photos_modified",
        "media_modified",
        "timeline_persisted",
        "rendered",
        "exported",
        "remote_network_allowed",
    ],
)


PROFILE_OUTPUT = {
    "type": "object",
    "properties": {
        "platform": {"type": "string"},
        "audience": {"type": "string"},
        "default_duration_ms": {"type": "integer"},
        "duration_ms": {"type": "integer"},
        "aspect_ratio": {"type": "string"},
        "tone": {"type": "string"},
        "pace": {"type": "string"},
        "narrative_arc": {"type": "string"},
        "must_include": {"type": "array", "items": {"type": "string"}},
        "must_exclude": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


CREATOR_CONTEXT_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creator_context"},
        "schema_version": {"const": "1"},
        "status": {"enum": ["completed", "capability_unavailable"]},
        "source": {"const": "sqlite_read_only"},
        "mode": {"const": "safe_default_read_only"},
        "capability_available": {"type": "boolean"},
        "profile_id": _nullable({"type": "string"}),
        "profile_revision": {"type": "integer", "minimum": 0},
        "profile_content_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
        ),
        "profile_source": _nullable(
            {"enum": ["user_edit", "confirmed_suggestion", "reset"]}
        ),
        "profile": PROFILE_OUTPUT,
        "evidence_summary": _exact_output_schema(
            {
                "count": {"type": "integer", "minimum": 0},
                "by_kind": {
                    "type": "object",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                },
                "project_reference_count": {"type": "integer", "minimum": 0},
                "asset_reference_count": {"type": "integer", "minimum": 0},
                "raw_references_included": {"const": False},
            },
            [
                "count",
                "by_kind",
                "project_reference_count",
                "asset_reference_count",
                "raw_references_included",
            ],
        ),
        "learning": _exact_output_schema(
            {
                "policy": {"const": "confirmed_only"},
                "confirmed_only": {"const": True},
                "hidden_inference": {"const": False},
                "accepted_sources": {
                    "type": "array",
                    "items": {
                        "enum": ["user_edit", "confirmed_suggestion", "reset"]
                    },
                },
            },
            ["policy", "confirmed_only", "hidden_inference", "accepted_sources"],
        ),
        "write_boundary": {"type": "string"},
        "safety": SAFETY_OUTPUT,
    },
    [
        "object",
        "schema_version",
        "status",
        "source",
        "mode",
        "capability_available",
        "profile_id",
        "profile_revision",
        "profile_content_sha256",
        "profile_source",
        "profile",
        "evidence_summary",
        "learning",
        "write_boundary",
        "safety",
    ],
)


INBOX_ASSET_OUTPUT = _exact_output_schema(
    {
        "asset_id": {"type": "string"},
        "media_kind": {"enum": ["image", "video", "audio"]},
        "filename": {"type": "string"},
        "captured_at": _nullable({"type": "string"}),
        "dimensions": _exact_output_schema(
            {
                "width": _nullable({"type": "integer", "minimum": 1}),
                "height": _nullable({"type": "integer", "minimum": 1}),
            },
            ["width", "height"],
        ),
        "timing": _exact_output_schema(
            {"duration_ms": _nullable({"type": "integer", "minimum": 1})},
            ["duration_ms"],
        ),
        "review": _exact_output_schema(
            {
                "revision": {"type": "integer", "minimum": 0},
                "inbox_state": {"enum": ["inbox", "kept", "archived"]},
                "favorite": {"type": "boolean"},
                "project_ready": {"type": "boolean"},
                "has_note": {"type": "boolean"},
            },
            [
                "revision",
                "inbox_state",
                "favorite",
                "project_ready",
                "has_note",
            ],
        ),
        "provenance": _exact_output_schema(
            {
                "asset_source_id": {"type": "string"},
                "asset_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-fA-F]{64}$",
                },
                "source_availability": {"type": "string"},
            },
            ["asset_source_id", "asset_sha256", "source_availability"],
        ),
    },
    [
        "asset_id",
        "media_kind",
        "filename",
        "captured_at",
        "dimensions",
        "timing",
        "review",
        "provenance",
    ],
)


INBOX_LIST_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.inbox_list"},
        "schema_version": {"const": "1"},
        "status": {"enum": ["completed", "capability_unavailable"]},
        "source": {"const": "sqlite_read_only"},
        "mode": {"const": "safe_default_read_only"},
        "capability_available": {"type": "boolean"},
        "state": {"enum": ["inbox", "kept", "archived", "all"]},
        "kinds": {
            "type": "array",
            "items": {"enum": ["image", "video", "audio"]},
        },
        "result_count": {"type": "integer", "minimum": 0},
        "assets": {"type": "array", "items": INBOX_ASSET_OUTPUT},
        "next_cursor": _nullable({"type": "string"}),
        "review_boundary": {"type": "string"},
        "safety": SAFETY_OUTPUT,
    },
    [
        "object",
        "schema_version",
        "status",
        "source",
        "mode",
        "capability_available",
        "state",
        "kinds",
        "result_count",
        "assets",
        "next_cursor",
        "review_boundary",
        "safety",
    ],
)


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
        "title": "Check MemoLens readiness",
        "description": (
            "Check which MemoLens indexes and read-only features are ready. Safe-default "
            "mode reads local SQLite without contacting the desktop service."
        ),
        "inputSchema": _schema(),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Check MemoLens readiness",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_search",
        "title": "Find photo memories",
        "description": (
            "Find photos that match a remembered scene or idea. Safe-default mode searches "
            "read-only local SQLite and returns only traversal-checked paths."
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
            "title": "Find photo memories",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_creator_context",
        "title": "Read confirmed creator context",
        "description": (
            "Read the latest user-confirmed Creator Memory profile and evidence summary "
            "directly from local SQLite. Never returns raw prompts or hidden inferences."
        ),
        "inputSchema": _schema(),
        "outputSchema": CREATOR_CONTEXT_OUTPUT,
        "annotations": {
            "title": "Read confirmed creator context",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_mixed_search",
        "title": "Find photos and video moments",
        "description": (
            "Find one source-grounded shortlist of photo memories and current video moments "
            "for a story idea, using deterministic reciprocal-rank fusion."
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
            "title": "Find photos and video moments",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_memories",
        "title": "Explore memory themes",
        "description": (
            "Explore read-only event and theme clusters from the local MemoLens Atlas for "
            "story planning. Requires the user's explicit loopback read opt-in."
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
            "title": "Explore memory themes",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_cleanup",
        "title": "Review cleanup suggestions",
        "description": (
            "Review likely duplicates, similar photos, low-quality items, and missing metadata. "
            "Requires explicit loopback read opt-in and never changes a photo."
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
            "title": "Review cleanup suggestions",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_video_search",
        "title": "Find moments inside videos",
        "description": (
            "Find precise moments inside indexed videos. Uses only the current successful "
            "analysis and returns stable source, segment, revision, and timing provenance."
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
            "title": "Find moments inside videos",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_media_list",
        "title": "Browse indexed media",
        "description": (
            "Browse indexed image, video, and audio details through read-only SQLite. Does "
            "not scan folders or reveal unnecessary absolute paths."
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
            "title": "Browse indexed media",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_media_get",
        "title": "Open media details",
        "description": (
            "Open source-grounded details for one indexed asset. Videos include only "
            "segments from the current successful analysis."
        ),
        "inputSchema": _schema(
            {"asset_id": {"type": "string", "minLength": 1, "maxLength": 200}},
            ["asset_id"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Open media details",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_inbox_list",
        "title": "Review the media inbox",
        "description": (
            "List current local photo, video, and audio review state with stable source, "
            "hash, and timing provenance. Suggestions must be confirmed in the MemoLens app."
        ),
        "inputSchema": _schema(
            {
                "state": {
                    "enum": ["inbox", "kept", "archived", "all"],
                    "default": "inbox",
                },
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
        "outputSchema": INBOX_LIST_OUTPUT,
        "annotations": {
            "title": "Review the media inbox",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_draft",
        "title": "Shape an unsaved story timeline",
        "description": (
            "Arrange selected photos, video moments, and audio into a deterministic hard-cut "
            "Timeline 1.0 draft in memory. Nothing is saved, rendered, or exported."
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
            "title": "Shape an unsaved story timeline",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_revise_draft",
        "title": "Refine the unsaved story timeline",
        "description": (
            "Apply strict typed changes to a Timeline 1.0 value and return a new in-memory "
            "draft. Nothing is saved, rendered, or exported."
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
            "title": "Refine the unsaved story timeline",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_validate",
        "title": "Check the timeline draft",
        "description": (
            "Check whether a Timeline 1.0 draft is structurally ready for desktop review. "
            "Does not access files or a network, verify sources, or save anything."
        ),
        "inputSchema": _schema(
            {"timeline": {"type": "object"}}, ["timeline"]
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Check the timeline draft",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_list",
        "title": "Browse existing timeline history",
        "description": (
            "Browse the latest immutable timeline revisions already stored by MemoLens "
            "through read-only SQLite. Does not create or save a revision."
        ),
        "inputSchema": _schema(
            {
                "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 24},
                "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
            }
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Browse existing timeline history",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_timeline_get",
        "title": "Open an existing timeline revision",
        "description": (
            "Open one immutable timeline revision already stored by MemoLens through "
            "read-only SQLite. Does not alter or save the timeline."
        ),
        "inputSchema": _schema(
            {
                "timeline_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "revision": {"type": "integer", "minimum": 1},
            },
            ["timeline_id"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Open an existing timeline revision",
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
        "memolens_creator_context": set(),
        "memolens_mixed_search": {"query", "limit"},
        "memolens_memories": {"query", "limit"},
        "memolens_cleanup": set(),
        "memolens_video_search": {"query", "limit"},
        "memolens_media_list": {"kinds", "limit", "cursor"},
        "memolens_media_get": {"asset_id"},
        "memolens_inbox_list": {"state", "kinds", "limit", "cursor"},
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
    if name == "memolens_creator_context":
        return gateway.creator_context()
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
    if name == "memolens_inbox_list":
        return gateway.inbox_list(
            state=args.get("state", "inbox"),
            kinds=args.get("kinds"),
            limit=args.get("limit", 24),
            cursor=args.get("cursor"),
        )
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
                    "localhost and supports confirmed Creator Memory, Media Inbox, SQLite media "
                    "search/read, and in-memory timeline drafting and validation. Do not enable "
                    "local API trust for the user. Inbox decisions require app confirmation. "
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
