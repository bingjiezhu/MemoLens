#!/usr/bin/env python3
"""Minimal stdio MCP server for MemoLens, implemented with Python stdlib."""

from __future__ import annotations

import json
import sys
from typing import Any

from memolens_contracts import PLUGIN_VERSION
from memolens_creative_blueprint import BLUEPRINT_CANDIDATE_SCHEMA
from memolens_core import MemoLensError, MemoLensGateway, json_ready
from memolens_library_bootstrap import (
    library_bootstrap_status,
    start_library_bootstrap,
)
from memolens_editor_server import (
    EditorServerError,
    create_canonical_editor_handoff,
    create_editor_handoff,
)
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json


SERVER_INFO = {"name": "memolens-local", "version": PLUGIN_VERSION}
PROTOCOL_VERSION = "2025-06-18"
MCP_JSON_LIMITS = StrictJsonLimits(
    max_bytes=1_048_576,
    max_depth=32,
    max_nodes=50_000,
    max_object_items=2_000,
    max_array_items=10_000,
    max_string_chars=262_144,
)
_BLUEPRINT_CANDIDATE_INPUT_SCHEMA = {
    "type": "object",
    "description": (
        "Untrusted candidate JSON. The tool applies the versioned Blueprint "
        "contract at runtime and returns structured schema diagnostics."
    ),
}
_BLUEPRINT_CANDIDATE_OUTPUT_SCHEMA = (
    BLUEPRINT_CANDIDATE_SCHEMA
    if isinstance(BLUEPRINT_CANDIDATE_SCHEMA, dict)
    else {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
)


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


_REPLACEMENT_ID_INPUT = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    "description": "Path-free stable identifier; private locators are rejected at runtime.",
}
_REPLACEMENT_SHA_INPUT = {
    "type": "string",
    "pattern": "^[0-9a-fA-F]{64}$",
}
_REPLACEMENT_TEXT_INPUT = {
    "label": {
        "type": "string",
        "minLength": 1,
        "maxLength": 120,
        "description": "Path-free display label.",
    },
    "reason": {
        "type": "string",
        "minLength": 1,
        "maxLength": 500,
        "description": "Path-free selection reason.",
    },
    "match_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "description": "Path-free source match identifier.",
    },
}
_REPLACEMENT_BASE_INPUT = {
    "asset_id": _REPLACEMENT_ID_INPUT,
    "asset_source_id": _REPLACEMENT_ID_INPUT,
    "asset_sha256": _REPLACEMENT_SHA_INPUT,
    **_REPLACEMENT_TEXT_INPUT,
}
_REPLACEMENT_RANGE_INPUT = {
    "source_in_ms": {
        "type": "integer",
        "minimum": 0,
        "maximum": 2_147_483_646,
    },
    "source_out_ms": {
        "type": "integer",
        "minimum": 1,
        "maximum": 2_147_483_647,
    },
}
_REPLACEMENT_CANDIDATE_INPUT = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "video"},
                **_REPLACEMENT_BASE_INPUT,
                "segment_id": _REPLACEMENT_ID_INPUT,
                "analysis_run_id": _REPLACEMENT_ID_INPUT,
                "analysis_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1_000_000,
                },
                **_REPLACEMENT_RANGE_INPUT,
            },
            "required": [
                "kind",
                "asset_id",
                "asset_source_id",
                "asset_sha256",
                "segment_id",
                "analysis_run_id",
                "analysis_revision",
                "source_in_ms",
                "source_out_ms",
            ],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "image"},
                **_REPLACEMENT_BASE_INPUT,
            },
            "required": ["kind", "asset_id", "asset_source_id", "asset_sha256"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "audio"},
                **_REPLACEMENT_BASE_INPUT,
                **_REPLACEMENT_RANGE_INPUT,
            },
            "required": [
                "kind",
                "asset_id",
                "asset_source_id",
                "asset_sha256",
                "source_in_ms",
                "source_out_ms",
            ],
            "additionalProperties": False,
        },
    ]
}


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


_IMAGE_SHA256_OUTPUT = {
    "type": "string",
    "pattern": "^[0-9a-f]{64}$",
}
_IMAGE_TOKEN_OUTPUT = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
}
_IMAGE_REASON_OUTPUT = {
    "type": "string",
    "minLength": 1,
    "maxLength": 120,
}
_IMAGE_ANALYSIS_BINDING_OUTPUT = _exact_output_schema(
    {
        "analysis_run_id": {
            "type": "string",
            "pattern": "^arun_[0-9a-f]{32}$",
        },
        "revision": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1_000_000,
        },
        "content_sha256": _IMAGE_SHA256_OUTPUT,
    },
    ["analysis_run_id", "revision", "content_sha256"],
)
_IMAGE_STAGE_PROVENANCE_OUTPUT = _exact_output_schema(
    {
        "producer_id": _IMAGE_TOKEN_OUTPUT,
        "producer_version": _IMAGE_TOKEN_OUTPUT,
        "model_id": _nullable(_IMAGE_TOKEN_OUTPUT),
        "model_version": _nullable(_IMAGE_TOKEN_OUTPUT),
        "rule_id": _nullable(_IMAGE_TOKEN_OUTPUT),
        "rule_version": _nullable(_IMAGE_TOKEN_OUTPUT),
    },
    [
        "producer_id",
        "producer_version",
        "model_id",
        "model_version",
        "rule_id",
        "rule_version",
    ],
)


def _image_stage_output_schema(output: dict[str, Any]) -> dict[str, Any]:
    return _exact_output_schema(
        {
            "status": {
                "enum": [
                    "succeeded",
                    "partial",
                    "unsupported",
                    "disabled",
                    "failed",
                    "unknown",
                ]
            },
            "provenance": _IMAGE_STAGE_PROVENANCE_OUTPUT,
            "output": _nullable(output),
            "reason_code": _nullable(_IMAGE_REASON_OUTPUT),
        },
        ["status", "provenance", "output", "reason_code"],
    )


_IMAGE_METADATA_OUTPUT = _exact_output_schema(
    {
        "mime_type": _nullable({"type": "string", "maxLength": 106}),
        "file_size": _nullable(
            {"type": "integer", "minimum": 1, "maximum": 8_589_934_592}
        ),
        "width": _nullable(
            {"type": "integer", "minimum": 1, "maximum": 100_000}
        ),
        "height": _nullable(
            {"type": "integer", "minimum": 1, "maximum": 100_000}
        ),
        "taken_at": _nullable({"type": "string", "maxLength": 128}),
        "latitude": _nullable(
            {"type": "number", "minimum": -90, "maximum": 90}
        ),
        "longitude": _nullable(
            {"type": "number", "minimum": -180, "maximum": 180}
        ),
        "altitude": _nullable(
            {"type": "number", "minimum": -20_000, "maximum": 1_000_000}
        ),
    },
    [
        "mime_type",
        "file_size",
        "width",
        "height",
        "taken_at",
        "latitude",
        "longitude",
        "altitude",
    ],
)
_IMAGE_GEOCODE_OUTPUT = _exact_output_schema(
    {
        "place_name": _nullable({"type": "string", "maxLength": 512}),
        "country": _nullable({"type": "string", "maxLength": 256}),
    },
    ["place_name", "country"],
)
_IMAGE_VISION_OUTPUT = _exact_output_schema(
    {
        "description": _nullable({"type": "string", "maxLength": 32_768}),
        "tags": {
            "type": "array",
            "maxItems": 128,
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "location_hint": _nullable({"type": "string", "maxLength": 2_048}),
    },
    ["description", "tags", "location_hint"],
)
_IMAGE_VECTOR_OUTPUT = _exact_output_schema(
    {
        "purpose": {"enum": ["image_search", "text_search"]},
        "signal": {"enum": ["visual", "text_derived"]},
        "model_id": _IMAGE_TOKEN_OUTPUT,
        "dimensions": {"type": "integer", "minimum": 1, "maximum": 65_536},
    },
    ["purpose", "signal", "model_id", "dimensions"],
)
_IMAGE_EMBEDDING_OUTPUT = _exact_output_schema(
    {
        "combined_text_sha256": _nullable(_IMAGE_SHA256_OUTPUT),
        "vectors": {
            "type": "array",
            "maxItems": 4,
            "items": _IMAGE_VECTOR_OUTPUT,
        },
    },
    ["combined_text_sha256", "vectors"],
)
_IMAGE_QUALITY_OUTPUT = _exact_output_schema(
    {
        "aesthetic_score": _nullable(
            {"type": "number", "minimum": 0, "maximum": 1}
        ),
        "technical_quality_score": _nullable(
            {"type": "number", "minimum": 0, "maximum": 1}
        ),
    },
    ["aesthetic_score", "technical_quality_score"],
)
_IMAGE_STAGES_OUTPUT = _exact_output_schema(
    {
        "metadata": _image_stage_output_schema(_IMAGE_METADATA_OUTPUT),
        "geocode": _image_stage_output_schema(_IMAGE_GEOCODE_OUTPUT),
        "vision": _image_stage_output_schema(_IMAGE_VISION_OUTPUT),
        "embedding": _image_stage_output_schema(_IMAGE_EMBEDDING_OUTPUT),
        "quality": _image_stage_output_schema(_IMAGE_QUALITY_OUTPUT),
    },
    ["metadata", "geocode", "vision", "embedding", "quality"],
)


def _canonical_image_observation_variant(
    *,
    status: str,
    provenance_status: str,
    analysis_binding: dict[str, Any],
    source_binding_sha256: dict[str, Any],
    projection: dict[str, Any],
    stages: dict[str, Any],
    reason_code: dict[str, Any],
) -> dict[str, Any]:
    return _exact_output_schema(
        {
            "object": {"const": "memolens.canonical_image_observation"},
            "schema_version": {"const": "1"},
            "status": {"const": status},
            "authority": {"const": "canonical_image_analysis"},
            "provenance_status": {"const": provenance_status},
            "asset_id": {"type": "string"},
            "analysis_binding": analysis_binding,
            "source_binding_sha256": source_binding_sha256,
            "projection": projection,
            "stages": stages,
            "reason_code": reason_code,
        },
        [
            "object",
            "schema_version",
            "status",
            "authority",
            "provenance_status",
            "asset_id",
            "analysis_binding",
            "source_binding_sha256",
            "projection",
            "stages",
            "reason_code",
        ],
    )


_IMAGE_CURRENT_PROJECTION_OUTPUT = _exact_output_schema(
    {
        "status": {"const": "current"},
        "generation_id": _IMAGE_TOKEN_OUTPUT,
        "processing_generation_id": _IMAGE_TOKEN_OUTPUT,
        "receipt_sha256": _IMAGE_SHA256_OUTPUT,
        "row_sha256": _IMAGE_SHA256_OUTPUT,
        "reason_code": {"type": "null"},
    },
    [
        "status",
        "generation_id",
        "processing_generation_id",
        "receipt_sha256",
        "row_sha256",
        "reason_code",
    ],
)
_IMAGE_PENDING_PROJECTION_OUTPUT = _exact_output_schema(
    {
        "status": {"const": "pending"},
        "generation_id": _nullable(_IMAGE_TOKEN_OUTPUT),
        "processing_generation_id": _nullable(_IMAGE_TOKEN_OUTPUT),
        "receipt_sha256": _nullable(_IMAGE_SHA256_OUTPUT),
        "row_sha256": {"type": "null"},
        "reason_code": _IMAGE_REASON_OUTPUT,
    },
    [
        "status",
        "generation_id",
        "processing_generation_id",
        "receipt_sha256",
        "row_sha256",
        "reason_code",
    ],
)
_IMAGE_UNAVAILABLE_PROJECTION_OUTPUT = _exact_output_schema(
    {
        "status": {"const": "unavailable"},
        "generation_id": {"type": "null"},
        "processing_generation_id": {"type": "null"},
        "receipt_sha256": {"type": "null"},
        "row_sha256": {"type": "null"},
        "reason_code": _IMAGE_REASON_OUTPUT,
    },
    [
        "status",
        "generation_id",
        "processing_generation_id",
        "receipt_sha256",
        "row_sha256",
        "reason_code",
    ],
)
CANONICAL_IMAGE_OBSERVATION_OUTPUT = {
    "oneOf": [
        _canonical_image_observation_variant(
            status="current",
            provenance_status="verified_current",
            analysis_binding=_IMAGE_ANALYSIS_BINDING_OUTPUT,
            source_binding_sha256=_IMAGE_SHA256_OUTPUT,
            projection=_IMAGE_CURRENT_PROJECTION_OUTPUT,
            stages=_IMAGE_STAGES_OUTPUT,
            reason_code={"type": "null"},
        ),
        _canonical_image_observation_variant(
            status="pending",
            provenance_status="verified_pending",
            analysis_binding=_IMAGE_ANALYSIS_BINDING_OUTPUT,
            source_binding_sha256=_IMAGE_SHA256_OUTPUT,
            projection=_IMAGE_PENDING_PROJECTION_OUTPUT,
            stages={"type": "null"},
            reason_code=_IMAGE_REASON_OUTPUT,
        ),
        _canonical_image_observation_variant(
            status="unavailable",
            provenance_status="unavailable",
            analysis_binding=_nullable(_IMAGE_ANALYSIS_BINDING_OUTPUT),
            source_binding_sha256=_nullable(_IMAGE_SHA256_OUTPUT),
            projection=_IMAGE_UNAVAILABLE_PROJECTION_OUTPUT,
            stages={"type": "null"},
            reason_code=_IMAGE_REASON_OUTPUT,
        ),
    ]
}


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
        "canonical_image_observation": _nullable(
            CANONICAL_IMAGE_OBSERVATION_OUTPUT
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
        "canonical_image_observation",
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


LIBRARY_BOOTSTRAP_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.library_bootstrap_status"},
        "schema_version": {"const": "1"},
        "request_id": {
            "type": "string",
            "pattern": "^lb_[0-9a-f]{64}$",
        },
        "expires_at_ms": {"type": "integer", "minimum": 0},
        "status": {
            "enum": [
                "queued_open_memolens",
                "awaiting_native_confirmation",
                "library_authority_staged",
                "library_authority_committed",
                "native_cancelled",
                "expired",
            ]
        },
        "next_action": {
            "enum": [
                "open_memolens",
                "finish_native_confirmation",
                "wait_for_library_scan",
                "start_with_new_idempotency_key",
            ]
        },
        "core_library_created": {"type": "boolean"},
        "database_created": {"type": "boolean"},
        "project_created": {"type": "boolean"},
        "scan_started": {"const": False},
        "editor_ready": {"const": False},
        "timeline_edit_granted": {"const": False},
        "backend_contacted_by_plugin": {"const": False},
    },
    [
        "object",
        "schema_version",
        "request_id",
        "expires_at_ms",
        "status",
        "next_action",
        "core_library_created",
        "database_created",
        "project_created",
        "scan_started",
        "editor_ready",
        "timeline_edit_granted",
        "backend_contacted_by_plugin",
    ],
)
LIBRARY_BOOTSTRAP_OUTPUT["allOf"] = [
    {
        "if": {
            "properties": {
                "status": {"const": "library_authority_committed"},
            },
            "required": ["status"],
        },
        "then": {
            "properties": {
                "next_action": {"const": "wait_for_library_scan"},
                "core_library_created": {"const": True},
                "database_created": {"const": True},
                "project_created": {"const": True},
            }
        },
        "else": {
            "properties": {
                "core_library_created": {"const": False},
                "database_created": {"const": False},
                "project_created": {"const": False},
            }
        },
    }
]


WIKI_PROJECTION_OUTPUT = _exact_output_schema(
    {
        "mode": {"const": "live_read_model_v0"},
        "generation": {"type": "null"},
        "generation_pinned": {"const": False},
        "snapshot_policy": {
            "const": "private_sqlite_snapshot_per_store_read"
        },
        "cross_request_consistency": {"const": "not_pinned"},
    },
    [
        "mode",
        "generation",
        "generation_pinned",
        "snapshot_policy",
        "cross_request_consistency",
    ],
)

WIKI_GAP_OUTPUT = _exact_output_schema(
    {"code": {"type": "string"}, "message": {"type": "string"}},
    ["code", "message"],
)

WIKI_COMMON_PROPERTIES = {
    "schema_version": {"const": "1"},
    "status": {"type": "string"},
    "source": {"const": "sqlite_read_only"},
    "mode": {"const": "safe_default_read_only"},
    "projection": WIKI_PROJECTION_OUTPUT,
    "gaps": {"type": "array", "items": WIKI_GAP_OUTPUT},
    "safety": SAFETY_OUTPUT,
}

WIKI_REVIEW_OUTPUT = _exact_output_schema(
    {
        "revision": {"type": "integer", "minimum": 0},
        "inbox_state": {"enum": ["inbox", "kept", "archived"]},
        "favorite": {"type": "boolean"},
        "project_ready": {"type": "boolean"},
    },
    ["revision", "inbox_state", "favorite", "project_ready"],
)

WIKI_ASSET_FACTS_OUTPUT = _exact_output_schema(
    {
        "asset_id": {"type": "string"},
        "asset_source_id": _nullable({"type": "string"}),
        "asset_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
        ),
        "media_kind": {"enum": ["image", "video", "audio"]},
        "mime_type": _nullable({"type": "string"}),
        "file_size": _nullable({"type": "integer", "minimum": 0}),
        "duration_ms": _nullable({"type": "integer", "minimum": 0}),
        "dimensions": _exact_output_schema(
            {
                "width": _nullable({"type": "integer", "minimum": 0}),
                "height": _nullable({"type": "integer", "minimum": 0}),
                "rotation_degrees": _nullable({"type": "integer"}),
            },
            ["width", "height", "rotation_degrees"],
        ),
        "captured_at": _nullable({"type": "string"}),
        "probe_status": _nullable({"type": "string"}),
        "source_availability": _nullable({"type": "string"}),
        "review": WIKI_REVIEW_OUTPUT,
    },
    [
        "asset_id",
        "asset_source_id",
        "asset_sha256",
        "media_kind",
        "mime_type",
        "file_size",
        "duration_ms",
        "dimensions",
        "captured_at",
        "probe_status",
        "source_availability",
        "review",
    ],
)

WIKI_SPAN_FACTS_OUTPUT = _exact_output_schema(
    {
        "asset_id": {"type": "string"},
        "asset_source_id": _nullable({"type": "string"}),
        "asset_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
        ),
        "source_availability": _nullable({"type": "string"}),
        "library_root_status": _nullable({"type": "string"}),
        "segment_id": {"type": "string"},
        "time_range": _exact_output_schema(
            {
                "start_ms": {"type": "integer", "minimum": 0},
                "end_ms": {"type": "integer", "minimum": 1},
                "interval_semantics": {"const": "half_open"},
            },
            ["start_ms", "end_ms", "interval_semantics"],
        ),
        "analysis_run_id": {"type": "string"},
        "analysis_revision": {"type": "integer", "minimum": 0},
        "current_successful_head": {"const": True},
    },
    [
        "asset_id",
        "asset_source_id",
        "asset_sha256",
        "source_availability",
        "library_root_status",
        "segment_id",
        "time_range",
        "analysis_run_id",
        "analysis_revision",
        "current_successful_head",
    ],
)

WIKI_COVERAGE_OUTPUT = _exact_output_schema(
    {
        "indexed_asset_count": {"type": "integer", "minimum": 0},
        "asset_kind_counts": {
            "type": "object",
            "additionalProperties": {"type": "integer", "minimum": 0},
        },
        "legacy_described_image_count": {"type": "integer", "minimum": 0},
        "current_video_segment_count": {"type": "integer", "minimum": 0},
        "default_discoverable_video_segment_count": {
            "type": "integer",
            "minimum": 0,
        },
        "indexed_video_asset_count": {"type": "integer", "minimum": 0},
        "current_video_asset_count": {"type": "integer", "minimum": 0},
        "default_discoverable_video_asset_count": {
            "type": "integer",
            "minimum": 0,
        },
        "video_assets_with_readable_current_spans": {
            "type": "integer",
            "minimum": 0,
        },
        "video_assets_without_readable_current_spans": {
            "type": "integer",
            "minimum": 0,
        },
        "video_assets_with_verified_successful_head": {
            "type": "integer",
            "minimum": 0,
        },
        "video_assets_without_verified_successful_head": {
            "type": "integer",
            "minimum": 0,
        },
        "video_assets_with_head_but_no_readable_spans": {
            "type": "integer",
            "minimum": 0,
        },
        "video_analysis_coverage_basis": {
            "enum": ["explicit_head_plus_successful_run", "unavailable"]
        },
        "physical_library_file_count": {"type": "null"},
        "unseen_library_files": {"const": "unknown"},
    },
    [
        "indexed_asset_count",
        "asset_kind_counts",
        "legacy_described_image_count",
        "current_video_segment_count",
        "default_discoverable_video_segment_count",
        "indexed_video_asset_count",
        "current_video_asset_count",
        "default_discoverable_video_asset_count",
        "video_assets_with_readable_current_spans",
        "video_assets_without_readable_current_spans",
        "video_assets_with_verified_successful_head",
        "video_assets_without_verified_successful_head",
        "video_assets_with_head_but_no_readable_spans",
        "video_analysis_coverage_basis",
        "physical_library_file_count",
        "unseen_library_files",
    ],
)

WIKI_CAPABILITIES_OUTPUT = _exact_output_schema(
    {
        key: {"type": "boolean"}
        for key in (
            "list_asset_pages",
            "search_pages",
            "open_asset_pages",
            "open_current_span_pages",
            "read_asset_evidence",
            "read_current_span_evidence",
            "write_wiki",
            "refine_media",
        )
    },
    [
        "list_asset_pages",
        "search_pages",
        "open_asset_pages",
        "open_current_span_pages",
        "read_asset_evidence",
        "read_current_span_evidence",
        "write_wiki",
        "refine_media",
    ],
)

WIKI_PAGE_SUMMARY_OUTPUT = _exact_output_schema(
    {
        "page_id": {"type": "string", "pattern": "^memolens://asset/"},
        "page_type": {"const": "asset"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "authority_summary": {
            "type": "array",
            "items": {"enum": ["deterministic", "model_hypothesis"]},
        },
        "status": {"const": "current_live_projection"},
        "freshness": {"const": "live"},
        "media_kind": {"enum": ["image", "video", "audio"]},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "pattern": "^memolens://evidence/asset/"},
        },
        "untrusted_data_fields": {"type": "array", "items": {"type": "string"}},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    [
        "page_id",
        "page_type",
        "title",
        "summary",
        "authority_summary",
        "status",
        "freshness",
        "media_kind",
        "evidence_refs",
        "untrusted_data_fields",
        "content_sha256",
    ],
)

WIKI_SEARCH_RESULT_OUTPUT = _exact_output_schema(
    {
        "page_id": {"type": "string", "pattern": "^memolens://(asset|span)/"},
        "evidence_id": {
            "type": "string",
            "pattern": "^memolens://evidence/(asset|span)/",
        },
        "result_type": {"enum": ["image", "video_segment"]},
        "media_kind": {"enum": ["image", "video"]},
        "asset_id": {"type": "string"},
        "segment_id": _nullable({"type": "string"}),
        "title": {"type": "string"},
        "summary": _nullable({"type": "string"}),
        "start_ms": _nullable({"type": "integer", "minimum": 0}),
        "end_ms": _nullable({"type": "integer", "minimum": 1}),
        "analysis_run_id": _nullable({"type": "string"}),
        "analysis_revision": _nullable({"type": "integer", "minimum": 0}),
        "matched_terms": {"type": "array", "items": {"type": "string"}},
        "rank_score": _nullable({"type": "number"}),
        "source_availability": _nullable({"type": "string"}),
        "authority_summary": {
            "type": "array",
            "items": {"enum": ["deterministic", "model_hypothesis"]},
        },
        "untrusted_data_fields": {"type": "array", "items": {"type": "string"}},
    },
    [
        "page_id",
        "evidence_id",
        "result_type",
        "media_kind",
        "asset_id",
        "segment_id",
        "title",
        "summary",
        "start_ms",
        "end_ms",
        "analysis_run_id",
        "analysis_revision",
        "matched_terms",
        "rank_score",
        "source_availability",
        "authority_summary",
        "untrusted_data_fields",
    ],
)

WIKI_PAGE_BODY_OUTPUT = _exact_output_schema(
    {
        "page_id": {"type": "string", "pattern": "^memolens://"},
        "page_type": {"enum": ["library", "asset", "temporal_span"]},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "authority_summary": {
            "type": "array",
            "items": {"enum": ["deterministic", "model_hypothesis"]},
        },
        "status": {"const": "current_live_projection"},
        "freshness": {"const": "live"},
        "content": {"type": "object"},
        "links": _exact_output_schema(
            {
                "items": {"type": "array", "items": {"type": "object"}},
                "returned_count": {"type": "integer", "minimum": 0},
                "known_count": _nullable({"type": "integer", "minimum": 0}),
                "truncated": {"type": "boolean"},
                "discovery_operation": {"const": "wiki-list"},
            },
            ["items", "returned_count", "known_count", "truncated"],
        ),
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "untrusted_data_fields": {"type": "array", "items": {"type": "string"}},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    [
        "page_id",
        "page_type",
        "title",
        "summary",
        "authority_summary",
        "status",
        "freshness",
        "content",
        "links",
        "evidence_refs",
        "untrusted_data_fields",
        "content_sha256",
    ],
)

WIKI_EVIDENCE_RECORD_OUTPUT = _exact_output_schema(
    {
        "evidence_id": {"type": "string", "pattern": "^memolens://evidence/"},
        "page_id": {"type": "string", "pattern": "^memolens://(asset|span)/"},
        "evidence_kind": {"enum": ["asset_identity", "current_video_span"]},
        "authority": {"const": "deterministic"},
        "facts": {"oneOf": [WIKI_ASSET_FACTS_OUTPUT, WIKI_SPAN_FACTS_OUTPUT]},
        "untrusted_data": _exact_output_schema(
            {"filename": _nullable({"type": "string"})}, ["filename"]
        ),
        "observations": _exact_output_schema(
            {
                "authority": {"const": "model_hypothesis"},
                "summary": _nullable({"type": "string"}),
                "visible_text": _nullable({"type": "string"}),
                "semantic": _nullable({"type": "object"}),
                "visual_status": _nullable({"type": "string"}),
                "transcript_status": _nullable({"type": "string"}),
                "confidence": _nullable({"type": "number"}),
            },
            [
                "authority",
                "summary",
                "visible_text",
                "semantic",
                "visual_status",
                "transcript_status",
                "confidence",
            ],
        ),
        "untrusted_data_fields": {"type": "array", "items": {"type": "string"}},
    },
    [
        "evidence_id",
        "page_id",
        "evidence_kind",
        "authority",
        "facts",
        "untrusted_data_fields",
    ],
)

WIKI_STATUS_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.wiki_status"},
        **WIKI_COMMON_PROPERTIES,
        "capability_available": {"type": "boolean"},
        "root_page_id": {"const": "memolens://library/current"},
        "coverage": WIKI_COVERAGE_OUTPUT,
        "capabilities": WIKI_CAPABILITIES_OUTPUT,
    },
    [
        "object",
        *WIKI_COMMON_PROPERTIES,
        "capability_available",
        "root_page_id",
        "coverage",
        "capabilities",
    ],
)

WIKI_LIST_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.wiki_page_list"},
        **WIKI_COMMON_PROPERTIES,
        "capability_available": {"type": "boolean"},
        "kinds": {
            "type": "array",
            "items": {"enum": ["image", "video", "audio"]},
        },
        "result_count": {"type": "integer", "minimum": 0},
        "pages": {"type": "array", "items": WIKI_PAGE_SUMMARY_OUTPUT},
        "next_cursor": _nullable({"type": "string"}),
    },
    [
        "object",
        *WIKI_COMMON_PROPERTIES,
        "capability_available",
        "kinds",
        "result_count",
        "pages",
        "next_cursor",
    ],
)

WIKI_SEARCH_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.wiki_search"},
        **WIKI_COMMON_PROPERTIES,
        "query": {"type": "string"},
        "ranking": {"type": "string"},
        "retrieval_claim": {"const": "existing_mixed_search_projection"},
        "result_count": {"type": "integer", "minimum": 0},
        "results": {"type": "array", "items": WIKI_SEARCH_RESULT_OUTPUT},
        "searched_result_types": {
            "type": "array",
            "items": {"type": "string"},
        },
        "branch_errors": {
            "type": "array",
            "items": _exact_output_schema(
                {
                    "result_type": {"enum": ["image", "video_segment"]},
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
                ["result_type", "code", "message"],
            ),
        },
    },
    [
        "object",
        *WIKI_COMMON_PROPERTIES,
        "query",
        "ranking",
        "retrieval_claim",
        "result_count",
        "results",
        "searched_result_types",
        "branch_errors",
    ],
)

WIKI_PAGE_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.wiki_page"},
        **WIKI_COMMON_PROPERTIES,
        "page": WIKI_PAGE_BODY_OUTPUT,
    },
    ["object", *WIKI_COMMON_PROPERTIES, "page"],
)

WIKI_EVIDENCE_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.wiki_evidence"},
        **WIKI_COMMON_PROPERTIES,
        "evidence": WIKI_EVIDENCE_RECORD_OUTPUT,
    },
    ["object", *WIKI_COMMON_PROPERTIES, "evidence"],
)


PROJECT_GAP_OUTPUT = _exact_output_schema(
    {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    ["code", "message"],
)

PROJECT_BASIC_OUTPUT = _exact_output_schema(
    {
        "project_id": {"type": "string"},
        "title": _nullable({"type": "string"}),
        "title_trust": {"const": "untrusted_data"},
        "untrusted_data_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
        "status": {"type": "string"},
        "created_at": _nullable({"type": "string"}),
        "updated_at": _nullable({"type": "string"}),
    },
    [
        "project_id",
        "title",
        "title_trust",
        "untrusted_data_fields",
        "status",
        "created_at",
        "updated_at",
    ],
)

PROJECT_LIST_ITEM_OUTPUT = _exact_output_schema(
    {
        **PROJECT_BASIC_OUTPUT["properties"],
        "completeness": {"enum": ["presence_only", "incomplete"]},
        "brief_revision": _nullable({"type": "integer", "minimum": 1}),
        "brief_revision_count": {"type": "integer", "minimum": 0},
        "timeline_branch_count": {"type": "integer", "minimum": 0},
        "timeline_revision_count": {"type": "integer", "minimum": 0},
        "gaps": {"type": "array", "items": PROJECT_GAP_OUTPUT},
    },
    [
        *PROJECT_BASIC_OUTPUT["required"],
        "completeness",
        "brief_revision",
        "brief_revision_count",
        "timeline_branch_count",
        "timeline_revision_count",
        "gaps",
    ],
)

PROJECT_INTEGRITY_OUTPUT = _exact_output_schema(
    {
        "status": {
            "enum": [
                "verified",
                "invalid_stored_digest",
                "digest_mismatch",
                "invalid_json",
                "identity_mismatch",
            ]
        },
        "stored_content_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
        ),
        "computed_content_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
        ),
        "digest_matches": {"type": "boolean"},
        "json_object": {"type": "boolean"},
        "identity_matches": {"type": "boolean"},
        "verified": {"type": "boolean"},
    },
    [
        "status",
        "stored_content_sha256",
        "computed_content_sha256",
        "digest_matches",
        "json_object",
        "identity_matches",
        "verified",
    ],
)

CREATOR_PROFILE_REF_OUTPUT = _exact_output_schema(
    {
        "profile_id": {"type": "string"},
        "revision": {"type": "integer", "minimum": 1},
        "content_sha256": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{64}$",
        },
    },
    ["profile_id", "revision", "content_sha256"],
)

PROJECT_BRIEF_FIELDS_OUTPUT = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "goal": {"type": "string"},
        "duration_ms": {"type": "integer", "minimum": 1},
        "aspect_ratio": {"type": "string"},
        "audience": {"type": "string"},
        "platform": {"type": "string"},
        "tone": {"type": "string"},
        "pace": {"type": "string"},
        "must_include": {"type": "array", "items": {"type": "string"}},
        "must_exclude": {"type": "array", "items": {"type": "string"}},
        "narrative_arc": {"type": "string"},
        "missing_assets": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "creator_profile_ref": CREATOR_PROFILE_REF_OUTPUT,
        "applied_profile_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "additionalProperties": False,
}

PROJECT_BRIEF_OUTPUT = _exact_output_schema(
    {
        "kind": {"const": "legacy_brief_projection"},
        "is_creative_blueprint": {"const": False},
        "revision": {"type": "integer", "minimum": 1},
        "content_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
        ),
        "created_at": _nullable({"type": "string"}),
        "integrity": PROJECT_INTEGRITY_OUTPUT,
        "data_trust": {"const": "untrusted_data"},
        "untrusted_data_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
        "fields": PROJECT_BRIEF_FIELDS_OUTPUT,
    },
    [
        "kind",
        "is_creative_blueprint",
        "revision",
        "content_sha256",
        "created_at",
        "integrity",
        "data_trust",
        "untrusted_data_fields",
        "fields",
    ],
)

PROJECT_FORMAT_OUTPUT = {
    "type": "object",
    "properties": {
        "width": {"type": "integer", "minimum": 1},
        "height": {"type": "integer", "minimum": 1},
        "fps": {"type": "number", "minimum": 0},
        "sample_rate": {"type": "integer", "minimum": 1},
        "duration_ms": {"type": "integer", "minimum": 0},
        "background_color": {"type": "string"},
    },
    "additionalProperties": False,
}

PROJECT_BRIEF_BINDING_OUTPUT = _exact_output_schema(
    {
        "revision": _nullable({"type": "integer", "minimum": 1}),
        "exists": _nullable({"type": "boolean"}),
        "latest_brief_revision": _nullable(
            {"type": "integer", "minimum": 1}
        ),
        "is_latest": _nullable({"type": "boolean"}),
    },
    ["revision", "exists", "latest_brief_revision", "is_latest"],
)

TIMELINE_RESUME_SUMMARY_PROPERTIES = {
    "kind": {
        "enum": ["latest_observed_timeline_head", "timeline_revision_summary"]
    },
    "authoritative_project_head": {"const": False},
    "selection_policy": {
        "enum": [
            "per_branch_max_revision_then_created_at_desc_revision_desc_id_asc",
            "history_created_at_desc_revision_desc_id_asc",
        ]
    },
    "timeline_id": {"type": "string"},
    "project_id": {"type": "string"},
    "revision": {"type": "integer", "minimum": 1},
    "parent_revision": _nullable({"type": "integer", "minimum": 1}),
    "brief_revision": _nullable({"type": "integer", "minimum": 1}),
    "schema_version": _nullable({"type": "string"}),
    "content_sha256": _nullable(
        {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}
    ),
    "validation_status": {"type": "string"},
    "created_at": _nullable({"type": "string"}),
    "integrity": PROJECT_INTEGRITY_OUTPUT,
    "format": _nullable(PROJECT_FORMAT_OUTPUT),
    "track_count": _nullable({"type": "integer", "minimum": 0}),
    "clip_count": _nullable({"type": "integer", "minimum": 0}),
    "brief_binding": PROJECT_BRIEF_BINDING_OUTPUT,
}

TIMELINE_RESUME_SUMMARY_REQUIRED = list(TIMELINE_RESUME_SUMMARY_PROPERTIES)

PROJECT_TIMELINE_HEAD_OUTPUT = _exact_output_schema(
    TIMELINE_RESUME_SUMMARY_PROPERTIES,
    TIMELINE_RESUME_SUMMARY_REQUIRED,
)

PROJECT_EVIDENCE_OUTPUT = _exact_output_schema(
    {
        "evidence_id": {
            "type": "string",
            "pattern": "^memolens://evidence/(asset|span)/[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
        },
        "kind": {"enum": ["asset", "span"]},
        "identifier": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {"enum": ["timeline_clip", "legacy_candidate_ref"]},
            "uniqueItems": True,
        },
    },
    ["evidence_id", "kind", "identifier", "sources"],
)

PROJECT_SELECTION_OUTPUT = _exact_output_schema(
    {
        "policy": {
            "enum": [
                "per_branch_max_revision_then_created_at_desc_revision_desc_id_asc",
                "explicit_blueprint_head_exact_revision_digest_operation_join",
            ]
        },
        "branch_head_count": {"type": "integer", "minimum": 0},
        "authoritative_project_head": {"type": "boolean"},
    },
    ["policy", "branch_head_count", "authoritative_project_head"],
)

PROJECT_OPERATION_SUBJECT_OUTPUT = _exact_output_schema(
    {
        "kind": {"enum": ["clip", "transition", "asset"]},
        "identifier": {"type": "string"},
    },
    ["kind", "identifier"],
)

PROJECT_OPERATION_OUTPUT = _exact_output_schema(
    {
        "type": {"type": "string"},
        "subject": _nullable(PROJECT_OPERATION_SUBJECT_OUTPUT),
    },
    ["type", "subject"],
)

PROJECT_OPERATION_SUMMARY_OUTPUT = _exact_output_schema(
    {
        "source": {
            "enum": ["verified_timeline_content_provenance", "unavailable"]
        },
        "observed_count": {"type": "integer", "minimum": 0},
        "returned_count": {"type": "integer", "minimum": 0},
        "operations": {"type": "array", "items": PROJECT_OPERATION_OUTPUT},
    },
    ["source", "observed_count", "returned_count", "operations"],
)

PROJECT_HISTORY_REVISION_OUTPUT = _exact_output_schema(
    {
        **TIMELINE_RESUME_SUMMARY_PROPERTIES,
        "operation_summary": PROJECT_OPERATION_SUMMARY_OUTPUT,
        "gaps": {"type": "array", "items": PROJECT_GAP_OUTPUT},
    },
    [*TIMELINE_RESUME_SUMMARY_REQUIRED, "operation_summary", "gaps"],
)

PROJECT_COMMON_PROPERTIES = {
    "schema_version": {"const": "1"},
    "status": {"enum": ["completed", "incomplete", "capability_unavailable"]},
    "source": {"const": "sqlite_read_only"},
    "mode": {"const": "safe_default_read_only"},
    "capability_available": {"type": "boolean"},
    "gaps": {"type": "array", "items": PROJECT_GAP_OUTPUT},
    "next_actions": {
        "type": "array",
        "items": {
            "enum": [
                "blueprint-get",
                "blueprint-history",
                "project-open",
                "project-history",
                "timeline-get",
                "wiki-evidence",
            ]
        },
    },
    "safety": SAFETY_OUTPUT,
}

PROJECT_COMMON_REQUIRED = list(PROJECT_COMMON_PROPERTIES)

PROJECT_LIST_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.project_list"},
        **PROJECT_COMMON_PROPERTIES,
        "status_filter": {
            "enum": ["current", "draft", "active", "archived", "all"]
        },
        "completeness": {"enum": ["presence_only", "incomplete"]},
        "result_count": {"type": "integer", "minimum": 0},
        "projects": {"type": "array", "items": PROJECT_LIST_ITEM_OUTPUT},
        "next_cursor": _nullable({"type": "string"}),
    },
    [
        "object",
        *PROJECT_COMMON_REQUIRED,
        "status_filter",
        "completeness",
        "result_count",
        "projects",
        "next_cursor",
    ],
)

PROJECT_BLUEPRINT_AUTHORITY_OUTPUT = _exact_output_schema(
    {
        "state": {
            "enum": ["unverified", "partially_confirmed", "confirmed"]
        },
        "all_decision_units_confirmed": {"type": "boolean"},
        "any_decision_unit_confirmed": {"type": "boolean"},
        "decision_unit_count": {"const": 8},
        "verified_decision_unit_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8,
        },
        "claims": {
            "type": "array",
            "items": {
                "enum": [
                    "agent_proposal",
                    "native_user_decision_confirmation",
                ]
            },
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
        },
    },
    [
        "state",
        "all_decision_units_confirmed",
        "any_decision_unit_confirmed",
        "decision_unit_count",
        "verified_decision_unit_count",
        "claims",
    ],
)

BLUEPRINT_CREATION_AUTHORITY_OUTPUT = _exact_output_schema(
    {
        "state": {"const": "unverified"},
        "verified": {"const": False},
    },
    ["state", "verified"],
)

BLUEPRINT_ACTIVE_CONFIRMATION_OUTPUT = _exact_output_schema(
    {
        "event_id": {"type": "string"},
        "confirmed_at": {
            "type": "string",
            "minLength": 20,
            "maxLength": 64,
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$",
        },
        "observed_revision": {"type": "integer", "minimum": 1},
        "presentation_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
    },
    [
        "event_id",
        "confirmed_at",
        "observed_revision",
        "presentation_sha256",
    ],
)

BLUEPRINT_DECISION_UNIT_AUTHORITY_OUTPUT = _exact_output_schema(
    {
        "semantic_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "state": {"enum": ["unverified", "confirmed"]},
        "confirmed": {"type": "boolean"},
        "active_confirmation": _nullable(BLUEPRINT_ACTIVE_CONFIRMATION_OUTPUT),
    },
    ["semantic_sha256", "state", "confirmed", "active_confirmation"],
)

BLUEPRINT_DECISION_AUTHORITY_OUTPUT = _exact_output_schema(
    {
        "state": {
            "enum": ["unverified", "partially_confirmed", "confirmed"]
        },
        "confirmed_decision_unit_count": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8,
        },
        "decision_unit_count": {"const": 8},
        "as_of_revision": {"type": "integer", "minimum": 1},
        "as_of_content_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "decision_units": _exact_output_schema(
            {
                unit: BLUEPRINT_DECISION_UNIT_AUTHORITY_OUTPUT
                for unit in (
                    "intent_goal",
                    "intent_stance",
                    "script",
                    "creative_direction",
                    "output",
                    "material_constraints",
                    "references",
                    "techniques",
                )
            },
            [
                "intent_goal",
                "intent_stance",
                "script",
                "creative_direction",
                "output",
                "material_constraints",
                "references",
                "techniques",
            ],
        ),
    },
    [
        "state",
        "confirmed_decision_unit_count",
        "decision_unit_count",
        "as_of_revision",
        "as_of_content_sha256",
        "decision_units",
    ],
)

PROJECT_BLUEPRINT_OUTLINE_OUTPUT = _exact_output_schema(
    {
        key: {"type": "integer", "minimum": 0}
        for key in (
            "script_block_count",
            "must_include_count",
            "must_exclude_count",
            "material_hint_count",
            "reference_count",
            "technique_count",
            "open_decision_count",
        )
    },
    [
        "script_block_count",
        "must_include_count",
        "must_exclude_count",
        "material_hint_count",
        "reference_count",
        "technique_count",
        "open_decision_count",
    ],
)

PROJECT_BLUEPRINT_PARENT_OUTPUT = _exact_output_schema(
    {
        "revision": {"type": "integer", "minimum": 1},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
    ["revision", "content_sha256"],
)

PROJECT_BLUEPRINT_OPEN_DECISION_OUTPUT = _exact_output_schema(
    {
        "decision_id": {"type": "string"},
        "question": {"type": "string"},
        "scope": {"type": "string"},
        "required_before": {"type": "string"},
    },
    ["decision_id", "question", "scope", "required_before"],
)

PROJECT_BLUEPRINT_OPERATION_OUTPUT = _exact_output_schema(
    {
        "operation_id": {"type": "string"},
        "sequence": {"type": "integer", "minimum": 1},
        "command_type": {"enum": ["blueprint.commit_proposal", "blueprint.restore_revision"]},
        "result_kind": {"enum": ["revision_created", "revision_restored"]},
        "created_at": {
            "type": "string",
            "minLength": 20,
            "maxLength": 64,
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$",
        },
    },
    ["operation_id", "sequence", "command_type", "result_kind", "created_at"],
)

PROJECT_BLUEPRINT_HEAD_OUTPUT = _exact_output_schema(
    {
        "canonical_persisted_head": {"const": True},
        "selection_policy": {
            "const": "explicit_blueprint_head_exact_revision_digest_operation_join"
        },
        "revision": {"type": "integer", "minimum": 1},
        "parent": _nullable(PROJECT_BLUEPRINT_PARENT_OUTPUT),
        "status": {"const": "proposal"},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "semantic_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "created_at": {
            "type": "string",
            "minLength": 20,
            "maxLength": 64,
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$",
        },
        "creation_authority": BLUEPRINT_CREATION_AUTHORITY_OUTPUT,
        "authority_projection": BLUEPRINT_DECISION_AUTHORITY_OUTPUT,
        "authority": PROJECT_BLUEPRINT_AUTHORITY_OUTPUT,
        "outline": PROJECT_BLUEPRINT_OUTLINE_OUTPUT,
        "open_decisions": {
            "type": "array",
            "items": PROJECT_BLUEPRINT_OPEN_DECISION_OUTPUT,
        },
        "data_trust": {"const": "untrusted_data"},
        "untrusted_data_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
        "operation": PROJECT_BLUEPRINT_OPERATION_OUTPUT,
    },
    [
        "canonical_persisted_head",
        "selection_policy",
        "revision",
        "parent",
        "status",
        "content_sha256",
        "semantic_sha256",
        "created_at",
        "creation_authority",
        "authority_projection",
        "authority",
        "outline",
        "open_decisions",
        "data_trust",
        "untrusted_data_fields",
        "operation",
    ],
)

PROJECT_RESUME_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.project_resume"},
        **PROJECT_COMMON_PROPERTIES,
        "completeness": {"enum": ["complete", "incomplete"]},
        "project": _nullable(PROJECT_BASIC_OUTPUT),
        "brief": _nullable(PROJECT_BRIEF_OUTPUT),
        "creative_blueprint_head": _nullable(PROJECT_BLUEPRINT_HEAD_OUTPUT),
        "latest_observed_timeline_head": _nullable(
            PROJECT_TIMELINE_HEAD_OUTPUT
        ),
        "evidence_refs": {"type": "array", "items": PROJECT_EVIDENCE_OUTPUT},
        "selection": PROJECT_SELECTION_OUTPUT,
    },
    [
        "object",
        *PROJECT_COMMON_REQUIRED,
        "completeness",
        "project",
        "brief",
        "creative_blueprint_head",
        "latest_observed_timeline_head",
        "evidence_refs",
        "selection",
    ],
)

PROJECT_HISTORY_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.project_history"},
        **PROJECT_COMMON_PROPERTIES,
        "project_id": _nullable({"type": "string"}),
        "timeline_id": _nullable({"type": "string"}),
        "project": _nullable(PROJECT_BASIC_OUTPUT),
        "result_count": {"type": "integer", "minimum": 0},
        "revisions": {
            "type": "array",
            "items": PROJECT_HISTORY_REVISION_OUTPUT,
        },
        "truncated": {"type": "boolean"},
        "completeness": {"enum": ["complete", "incomplete"]},
    },
    [
        "object",
        *PROJECT_COMMON_REQUIRED,
        "project_id",
        "timeline_id",
        "project",
        "result_count",
        "revisions",
        "truncated",
        "completeness",
    ],
)

BLUEPRINT_CONTRACT_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint_candidate"},
        "schema_version": {"const": "1"},
        "schema_available": {"type": "boolean"},
        "schema_sha256": _nullable(
            {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        ),
        "schema_artifact": {
            "const": "creative-blueprint-candidate-v1.schema.json"
        },
        "candidate_persisted": {"const": False},
        "authority_verified": {"const": False},
    },
    [
        "object",
        "schema_version",
        "schema_available",
        "schema_sha256",
        "schema_artifact",
        "candidate_persisted",
        "authority_verified",
    ],
)

BLUEPRINT_SAFETY_OUTPUT = _exact_output_schema(
    {
        "read_only": {"const": True},
        "candidate_ephemeral": {"const": True},
        "private_ephemeral_snapshot_possible": {"const": True},
        "persistent_state_written": {"const": False},
        "blueprint_persisted": {"const": False},
        "project_modified": {"const": False},
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
        "candidate_ephemeral",
        "private_ephemeral_snapshot_possible",
        "persistent_state_written",
        "blueprint_persisted",
        "project_modified",
        "photos_opened_by_plugin",
        "photos_modified",
        "media_modified",
        "timeline_persisted",
        "rendered",
        "exported",
        "remote_network_allowed",
    ],
)

BLUEPRINT_AUTHORITY_OUTPUT = _exact_output_schema(
    {
        "declared_sources": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {
                "enum": [
                    "caller_declared_user_input",
                    "agent_proposal",
                    "legacy_unknown",
                    "unknown",
                ]
            },
        },
        "authority_verified": {"const": False},
        "user_confirmed": {"const": False},
        "decision_ledger_available": {"const": False},
        "candidate_can_self_assert_confirmation": {"const": False},
    },
    [
        "declared_sources",
        "authority_verified",
        "user_confirmed",
        "decision_ledger_available",
        "candidate_can_self_assert_confirmation",
    ],
)

BLUEPRINT_ERROR_OUTPUT = _exact_output_schema(
    {
        "code": {"type": "string", "minLength": 1, "maxLength": 100},
        "path": {"type": "string", "maxLength": 500},
        "message": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    ["code", "path", "message"],
)

BLUEPRINT_BASE_BINDING_OUTPUT = _exact_output_schema(
    {
        "status": {
            "enum": ["verified", "missing", "stale", "conflict", "not_applicable"]
        },
        "checked_in_single_snapshot": {"type": "boolean"},
        "issues": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
    },
    ["status", "checked_in_single_snapshot", "issues"],
)

BLUEPRINT_REFERENCE_ISSUE_OUTPUT = _exact_output_schema(
    {
        "path": {"type": "string", "maxLength": 500},
        "code": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    ["path", "code"],
)

BLUEPRINT_REFERENCE_RESOLUTION_OUTPUT = _exact_output_schema(
    {
        "status": {
            "enum": ["verified", "partial", "unresolved", "not_applicable"]
        },
        "checked_in_single_snapshot": {"type": "boolean"},
        "identity_and_current_availability_only": {"const": True},
        "total_count": {"type": "integer", "minimum": 0},
        "verified_count": {"type": "integer", "minimum": 0},
        "unresolved_count": {"type": "integer", "minimum": 0},
        "issues": {
            "type": "array",
            "maxItems": 64,
            "items": BLUEPRINT_REFERENCE_ISSUE_OUTPUT,
        },
        "issues_truncated": {"type": "boolean"},
    },
    [
        "status",
        "checked_in_single_snapshot",
        "identity_and_current_availability_only",
        "total_count",
        "verified_count",
        "unresolved_count",
        "issues",
        "issues_truncated",
    ],
)

BLUEPRINT_PLANNING_OUTPUT = _exact_output_schema(
    {
        "status": {
            "enum": ["ready_for_coverage", "ready_with_gaps", "blocked"]
        },
        "blockers": {
            "type": "array",
            "maxItems": 128,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "gaps": {
            "type": "array",
            "maxItems": 256,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
    },
    ["status", "blockers", "gaps"],
)

BLUEPRINT_OUTLINE_OUTPUT = _exact_output_schema(
    {
        key: {"type": "integer", "minimum": 0}
        for key in (
            "script_block_count",
            "must_include_count",
            "must_exclude_count",
            "material_hint_count",
            "reference_count",
            "technique_count",
            "assumption_count",
            "missing_evidence_count",
            "open_decision_count",
        )
    },
    [
        "script_block_count",
        "must_include_count",
        "must_exclude_count",
        "material_hint_count",
        "reference_count",
        "technique_count",
        "assumption_count",
        "missing_evidence_count",
        "open_decision_count",
    ],
)

BLUEPRINT_VALIDATION_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint_validation"},
        "schema_version": {"const": "1"},
        "status": {"enum": ["completed", "capability_unavailable"]},
        "source": {"const": "in_memory_validation"},
        "mode": {"const": "safe_default_read_only"},
        "contract": BLUEPRINT_CONTRACT_OUTPUT,
        "schema_valid": {"type": "boolean"},
        "candidate_digest": _nullable(
            {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        ),
        "material_constraint_projection_digest_v1": _nullable(
            {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        ),
        "errors": {
            "type": "array",
            "maxItems": 64,
            "items": BLUEPRINT_ERROR_OUTPUT,
        },
        "errors_truncated": {"type": "boolean"},
        "base_binding": BLUEPRINT_BASE_BINDING_OUTPUT,
        "reference_resolution": BLUEPRINT_REFERENCE_RESOLUTION_OUTPUT,
        "planning_readiness": BLUEPRINT_PLANNING_OUTPUT,
        "outline": BLUEPRINT_OUTLINE_OUTPUT,
        "authority": BLUEPRINT_AUTHORITY_OUTPUT,
        "data_trust": {"const": "untrusted_data"},
        "untrusted_data_fields": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "safety": BLUEPRINT_SAFETY_OUTPUT,
    },
    [
        "object",
        "schema_version",
        "status",
        "source",
        "mode",
        "contract",
        "schema_valid",
        "candidate_digest",
        "material_constraint_projection_digest_v1",
        "errors",
        "errors_truncated",
        "base_binding",
        "reference_resolution",
        "planning_readiness",
        "outline",
        "authority",
        "data_trust",
        "untrusted_data_fields",
        "safety",
    ],
)

BLUEPRINT_GAP_OUTPUT = _exact_output_schema(
    {
        "code": {"type": "string", "minLength": 1, "maxLength": 100},
        "message": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    ["code", "message"],
)

BLUEPRINT_SHADOW_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint_shadow"},
        "schema_version": {"const": "1"},
        "status": {
            "enum": ["completed", "incomplete", "capability_unavailable"]
        },
        "source": {"const": "sqlite_read_only"},
        "mode": {"const": "safe_default_read_only"},
        "projection_mode": {"const": "legacy_shadow_v1"},
        "contract": BLUEPRINT_CONTRACT_OUTPUT,
        "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "is_authoritative": {"const": False},
        "is_persisted": {"const": False},
        "shadow": _nullable(_BLUEPRINT_CANDIDATE_OUTPUT_SCHEMA),
        "shadow_digest": _nullable(
            {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        ),
        "validation": _nullable(BLUEPRINT_VALIDATION_OUTPUT),
        "authority": BLUEPRINT_AUTHORITY_OUTPUT,
        "data_trust": {"const": "untrusted_data"},
        "untrusted_data_fields": {
            "type": "array",
            "maxItems": 32,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "gaps": {
            "type": "array",
            "maxItems": 128,
            "items": BLUEPRINT_GAP_OUTPUT,
        },
        "safety": BLUEPRINT_SAFETY_OUTPUT,
    },
    [
        "object",
        "schema_version",
        "status",
        "source",
        "mode",
        "projection_mode",
        "contract",
        "project_id",
        "is_authoritative",
        "is_persisted",
        "shadow",
        "shadow_digest",
        "validation",
        "authority",
        "data_trust",
        "untrusted_data_fields",
        "gaps",
        "safety",
    ],
)

PERSISTED_BLUEPRINT_CONTRACT_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint"},
        "schema_version": {"const": "1"},
        "schema_available": {"type": "boolean"},
        "schema_sha256": _nullable({"type": "string", "pattern": "^[0-9a-f]{64}$"}),
        "schema_artifact": {"const": "creative-blueprint-v1.schema.json"},
        "persisted_read_only": {"const": True},
        "creation_authority_verified": {"const": False},
        "ledger_coverage_scope": {"const": "creative_blueprint"},
        "complete_project_history": {"const": False},
    },
    [
        "object",
        "schema_version",
        "schema_available",
        "schema_sha256",
        "schema_artifact",
        "persisted_read_only",
        "creation_authority_verified",
        "ledger_coverage_scope",
        "complete_project_history",
    ],
)

PERSISTED_BLUEPRINT_SAFETY_OUTPUT = _exact_output_schema(
    {
        **SAFETY_OUTPUT["properties"],
        "private_ephemeral_snapshot": {"const": True},
        "source_database_written": {"const": False},
        "blueprint_written": {"const": False},
        "operation_receipts_read": {"type": "boolean"},
        "operation_receipts_validated": {"type": "boolean"},
        "raw_operation_receipts_returned": {"const": False},
        "raw_operation_json_returned": {"const": False},
        "private_locator_returned_by_history": {"const": False},
    },
    [
        *SAFETY_OUTPUT["required"],
        "private_ephemeral_snapshot",
        "source_database_written",
        "blueprint_written",
        "operation_receipts_read",
        "operation_receipts_validated",
        "raw_operation_receipts_returned",
        "raw_operation_json_returned",
        "private_locator_returned_by_history",
    ],
)

PERSISTED_BLUEPRINT_HEAD_REF_OUTPUT = _exact_output_schema(
    {
        "revision": {"type": "integer", "minimum": 1},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "semantic_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "operation_id": {"type": "string"},
    },
    ["revision", "content_sha256", "semantic_sha256", "operation_id"],
)

PERSISTED_BLUEPRINT_SELECTION_OUTPUT = _exact_output_schema(
    {
        "kind": {"enum": ["current", "exact_revision"]},
        "explicit_head": {"const": True},
        "is_current": {"type": "boolean"},
        "current_head": _nullable(PERSISTED_BLUEPRINT_HEAD_REF_OUTPUT),
    },
    ["kind", "explicit_head", "is_current", "current_head"],
)

PERSISTED_BLUEPRINT_PROJECTION_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint_projection"},
        "schema_version": {"const": "1"},
        "schema_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "project_id": {"type": "string"},
        "revision": {"type": "integer", "minimum": 1},
        "parent": _nullable(PROJECT_BLUEPRINT_PARENT_OUTPUT),
        "created_by_operation_id": {"type": "string"},
        "status": {"const": "proposal"},
        "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "semantic_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "created_at": {
            "type": "string",
            "minLength": 20,
            "maxLength": 64,
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$",
        },
        "semantic": {"type": "object"},
        "lineage": {"type": "object"},
        "evidence_manifest": {"type": "array", "items": {"type": "object"}},
        "authority": {"type": "object"},
        "creation_authority": BLUEPRINT_CREATION_AUTHORITY_OUTPUT,
        "authority_projection": BLUEPRINT_DECISION_AUTHORITY_OUTPUT,
        "authority_summary": PROJECT_BLUEPRINT_AUTHORITY_OUTPUT,
        "outline": PROJECT_BLUEPRINT_OUTLINE_OUTPUT,
        "data_trust": {"const": "untrusted_data"},
        "untrusted_data_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    [
        "object",
        "schema_version",
        "schema_sha256",
        "project_id",
        "revision",
        "parent",
        "created_by_operation_id",
        "status",
        "content_sha256",
        "semantic_sha256",
        "created_at",
        "semantic",
        "lineage",
        "evidence_manifest",
        "authority",
        "creation_authority",
        "authority_projection",
        "authority_summary",
        "outline",
        "data_trust",
        "untrusted_data_fields",
    ],
)

PERSISTED_BLUEPRINT_OPERATION_OUTPUT = _exact_output_schema(
    {
        "operation_id": {"type": "string"},
        "sequence": {"type": "integer", "minimum": 1},
        "parent_operation_id": _nullable({"type": "string"}),
        "coverage_scope": {"const": "creative_blueprint"},
        "complete_project_history": {"const": False},
        "command_type": {"enum": ["blueprint.commit_proposal", "blueprint.restore_revision"]},
        "command_version": {"const": "1"},
        "effect_class": {"const": "reversible_project_write"},
        "intent_code": {"type": "string"},
        "result_kind": {"enum": ["revision_created", "no_change", "revision_restored"]},
        "result_revision": {"type": "integer", "minimum": 1},
        "result_content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "changed_sections": {
            "type": "array",
            "uniqueItems": True,
            "items": {
                "enum": [
                    "intent",
                    "script",
                    "direction",
                    "output",
                    "constraints",
                    "material_hints",
                    "reference_refs",
                    "technique_refs",
                    "bindings",
                    "assumptions",
                    "missing_evidence",
                    "open_decisions",
                ]
            },
        },
        "restore_from": _nullable(PROJECT_BLUEPRINT_PARENT_OUTPUT),
        "created_at": {
            "type": "string",
            "minLength": 20,
            "maxLength": 64,
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$",
        },
    },
    [
        "operation_id",
        "sequence",
        "parent_operation_id",
        "coverage_scope",
        "complete_project_history",
        "command_type",
        "command_version",
        "effect_class",
        "intent_code",
        "result_kind",
        "result_revision",
        "result_content_sha256",
        "changed_sections",
        "restore_from",
        "created_at",
    ],
)

PERSISTED_BLUEPRINT_COMMON_PROPERTIES = {
    "schema_version": {"const": "1"},
    "status": {"enum": ["completed", "incomplete", "capability_unavailable"]},
    "source": {"const": "sqlite_read_only"},
    "mode": {"const": "safe_default_read_only"},
    "capability_available": {"type": "boolean"},
    "project_id": {"type": "string"},
    "contract": PERSISTED_BLUEPRINT_CONTRACT_OUTPUT,
    "gaps": {"type": "array", "items": BLUEPRINT_GAP_OUTPUT},
    "safety": PERSISTED_BLUEPRINT_SAFETY_OUTPUT,
}

PERSISTED_BLUEPRINT_READ_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint_read"},
        **PERSISTED_BLUEPRINT_COMMON_PROPERTIES,
        "requested_revision": _nullable({"type": "integer", "minimum": 1}),
        "selection": _nullable(PERSISTED_BLUEPRINT_SELECTION_OUTPUT),
        "blueprint": _nullable(PERSISTED_BLUEPRINT_PROJECTION_OUTPUT),
        "operation": _nullable(PERSISTED_BLUEPRINT_OPERATION_OUTPUT),
    },
    [
        "object",
        *PERSISTED_BLUEPRINT_COMMON_PROPERTIES,
        "requested_revision",
        "selection",
        "blueprint",
        "operation",
    ],
)

PERSISTED_BLUEPRINT_HISTORY_ITEM_OUTPUT = _exact_output_schema(
    {
        **PERSISTED_BLUEPRINT_OPERATION_OUTPUT["properties"],
        "result_head": _exact_output_schema(
            {
                **PERSISTED_BLUEPRINT_HEAD_REF_OUTPUT["properties"],
                "status": {"const": "proposal"},
                "creation_authority": BLUEPRINT_CREATION_AUTHORITY_OUTPUT,
                "authority_projection": BLUEPRINT_DECISION_AUTHORITY_OUTPUT,
                "outline": PROJECT_BLUEPRINT_OUTLINE_OUTPUT,
            },
            [
                *PERSISTED_BLUEPRINT_HEAD_REF_OUTPUT["required"],
                "status",
                "creation_authority",
                "authority_projection",
                "outline",
            ],
        ),
    },
    [*PERSISTED_BLUEPRINT_OPERATION_OUTPUT["required"], "result_head"],
)

PERSISTED_BLUEPRINT_HISTORY_OUTPUT = _exact_output_schema(
    {
        "object": {"const": "memolens.creative_blueprint_history"},
        **PERSISTED_BLUEPRINT_COMMON_PROPERTIES,
        "coverage_scope": {"const": "creative_blueprint"},
        "complete_project_history": {"const": False},
        "current_head": _nullable(PERSISTED_BLUEPRINT_HEAD_REF_OUTPUT),
        "result_count": {"type": "integer", "minimum": 0},
        "operations": {"type": "array", "items": PERSISTED_BLUEPRINT_HISTORY_ITEM_OUTPUT},
        "truncated": {"type": "boolean"},
    },
    [
        "object",
        *PERSISTED_BLUEPRINT_COMMON_PROPERTIES,
        "coverage_scope",
        "complete_project_history",
        "current_head",
        "result_count",
        "operations",
        "truncated",
    ],
)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "memolens_library_bootstrap_start",
        "title": "Ask MemoLens to confirm a local Library",
        "description": (
            "Queue one bounded, path-free app-state intent for MemoLens Electron to ask "
            "the user to choose a local media directory through a native dialog. The "
            "tool never accepts a path, database, project seed, token, or proof; it does "
            "not contact the backend, wake an unverified executable, create a Core "
            "Library/database/project/Blueprint, start a scan, or grant Timeline editing. "
            "Until a stable packaged-app wake is proven, queued_open_memolens means the "
            "user must open MemoLens themselves. Electron may later project either staged "
            "desktop authority or a permanent Core commit into the path-free status."
        ),
        "inputSchema": _schema(
            {
                "request_idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
                    "description": (
                        "Stable retry key; only its deterministic opaque request ID is stored."
                    ),
                }
            },
            ["request_idempotency_key"],
        ),
        "outputSchema": LIBRARY_BOOTSTRAP_OUTPUT,
        "annotations": {
            "title": "Ask MemoLens to confirm a local Library",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_library_bootstrap_status",
        "title": "Check native Library request",
        "description": (
            "Read one bounded, path-free bootstrap spool state by opaque request ID. "
            "It never returns a local path, database locator, device identity, native "
            "ticket, token, proof, project seed, or Timeline capability."
        ),
        "inputSchema": _schema(
            {
                "request_id": {
                    "type": "string",
                    "pattern": "^lb_[0-9a-f]{64}$",
                }
            },
            ["request_id"],
        ),
        "outputSchema": LIBRARY_BOOTSTRAP_OUTPUT,
        "annotations": {
            "title": "Check native Library request",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
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
            "Find photos that match a remembered scene or idea. Results require an exact "
            "current canonical image observation from read-only local SQLite; valid "
            "matches may include a traversal-checked path for explicit local inspection."
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
                "filters": _schema(
                    {
                        "unused_only": {"type": "boolean"},
                        "prefer_unused": {"type": "boolean"},
                        "allow_reuse": {"type": "boolean"},
                        "used_in": {
                            "oneOf": [
                                {
                                    "type": "string",
                                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                                },
                                _schema(
                                    {
                                        "project_id": {
                                            "type": "string",
                                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                                        },
                                        "export_revision": {
                                            "type": "integer",
                                            "minimum": 1,
                                            "maximum": 1_000_000,
                                        },
                                    },
                                    ["project_id"],
                                ),
                            ]
                        },
                        "residual_of": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                        },
                    }
                ),
            },
            ["query"],
        ),
        "outputSchema": _output_schema(
            {
                "object": {"const": "memolens.mixed_search"},
                "status": {"type": "string"},
                "source": {"type": "string"},
                "query": {"type": "string"},
                "search_revision": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "derivative_revision": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "usage_revision": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
                "derivative_exclusion": {"type": "object"},
                "result_count": {"type": "integer"},
                "results": {"type": "array", "items": {"type": "object"}},
            },
            [
                "object",
                "status",
                "source",
                "query",
                "search_revision",
                "derivative_revision",
                "derivative_exclusion",
                "result_count",
                "results",
            ],
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
        "name": "memolens_wiki_status",
        "title": "Check the live media Wiki",
        "description": (
            "Describe indexed-media coverage, supported Wiki reads, and explicit gaps. "
            "The v0 view is live and honestly reports that no generation is pinned."
        ),
        "inputSchema": _schema(),
        "outputSchema": WIKI_STATUS_OUTPUT,
        "annotations": {
            "title": "Check the live media Wiki",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_wiki_list",
        "title": "Browse live media Wiki pages",
        "description": (
            "Browse compact Asset page summaries without scanning the Library, opening "
            "media, or returning absolute paths. Archived assets are excluded by default."
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
        "outputSchema": WIKI_LIST_OUTPUT,
        "annotations": {
            "title": "Browse live media Wiki pages",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_wiki_search",
        "title": "Find live Wiki pages and evidence",
        "description": (
            "Project existing mixed-media search results into navigable Asset or current "
            "Span pages with stable evidence references."
        ),
        "inputSchema": _schema(
            {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4096,
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
        "outputSchema": WIKI_SEARCH_OUTPUT,
        "annotations": {
            "title": "Find live Wiki pages and evidence",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_wiki_open",
        "title": "Open a live media Wiki page",
        "description": (
            "Open only a validated memolens:// Library, Asset, or current successful "
            "video Span page. Media text is marked as untrusted data."
        ),
        "inputSchema": _schema(
            {"page_id": {"type": "string", "minLength": 1, "maxLength": 260}},
            ["page_id"],
        ),
        "outputSchema": WIKI_PAGE_OUTPUT,
        "annotations": {
            "title": "Open a live media Wiki page",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_wiki_evidence",
        "title": "Read exact media Wiki evidence",
        "description": (
            "Resolve a validated asset identity or current successful video span evidence "
            "reference to stable hash, source, revision, and integer-millisecond facts."
        ),
        "inputSchema": _schema(
            {
                "evidence_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 260,
                }
            },
            ["evidence_id"],
        ),
        "outputSchema": WIKI_EVIDENCE_OUTPUT,
        "annotations": {
            "title": "Read exact media Wiki evidence",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_project_list",
        "title": "List resumable creative projects",
        "description": (
            "List bounded read-only creative-project summaries so a new Agent can find "
            "work without importing a prior chat. Current includes draft and active projects."
        ),
        "inputSchema": _schema(
            {
                "status": {
                    "enum": ["current", "draft", "active", "archived", "all"],
                    "default": "current",
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
        "outputSchema": PROJECT_LIST_OUTPUT,
        "annotations": {
            "title": "List resumable creative projects",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_project_open",
        "title": "Resume an existing creative project",
        "description": (
            "Read one bounded Resume Capsule from the persisted project, legacy brief, "
            "latest observed Timeline head, and stable evidence references."
        ),
        "inputSchema": _schema(
            {
                "project_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                }
            },
            ["project_id"],
        ),
        "outputSchema": PROJECT_RESUME_OUTPUT,
        "annotations": {
            "title": "Resume an existing creative project",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_project_history",
        "title": "Review project revision history",
        "description": (
            "Read bounded Timeline revision and safe operation-type summaries across a "
            "project without returning raw Timeline or provenance JSON."
        ),
        "inputSchema": _schema(
            {
                "project_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                },
            },
            ["project_id"],
        ),
        "outputSchema": PROJECT_HISTORY_OUTPUT,
        "annotations": {
            "title": "Review project revision history",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_blueprint_shadow",
        "title": "Read a legacy Blueprint shadow",
        "description": (
            "Project the latest verified legacy brief and observed Timeline baseline into "
            "a non-authoritative, non-persisted Creative Blueprint candidate."
        ),
        "inputSchema": _schema(
            {
                "project_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                }
            },
            ["project_id"],
        ),
        "outputSchema": BLUEPRINT_SHADOW_OUTPUT,
        "annotations": {
            "title": "Read a legacy Blueprint shadow",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_blueprint_get",
        "title": "Read the persisted Creative Blueprint",
        "description": (
            "Read the explicit canonical Blueprint head or one exact immutable revision "
            "from a private read-only SQLite snapshot. A corrupt head never falls back."
        ),
        "inputSchema": _schema(
            {
                "project_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                },
                "revision": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1_000_000,
                },
            },
            ["project_id"],
        ),
        "outputSchema": PERSISTED_BLUEPRINT_READ_OUTPUT,
        "annotations": {
            "title": "Read the persisted Creative Blueprint",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_blueprint_history",
        "title": "Review Blueprint operation history",
        "description": (
            "Read bounded Blueprint-only operation and revision summaries without "
            "returning script text, reference locators, paths, keys, or raw operation JSON."
        ),
        "inputSchema": _schema(
            {
                "project_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50,
                },
            },
            ["project_id"],
        ),
        "outputSchema": PERSISTED_BLUEPRINT_HISTORY_OUTPUT,
        "annotations": {
            "title": "Review Blueprint operation history",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_blueprint_validate",
        "title": "Validate a Creative Blueprint candidate",
        "description": (
            "Strictly validate one bounded Blueprint candidate and resolve any persisted "
            "baseline or evidence references without saving project state."
        ),
        "inputSchema": _schema(
            {"candidate": _BLUEPRINT_CANDIDATE_INPUT_SCHEMA},
            ["candidate"],
        ),
        "outputSchema": BLUEPRINT_VALIDATION_OUTPUT,
        "annotations": {
            "title": "Validate a Creative Blueprint candidate",
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
        "name": "memolens_canonical_editor_handoff",
        "title": "Open the MemoLens Canonical Editor",
        "description": (
            "Open the exact current canonical Timeline for one project in a bounded, "
            "loopback-only editor. The tool accepts only project_id: it reads and "
            "cross-checks the current Blueprint, Coverage, Timeline, fixed source "
            "manifest, and same-Beat verified replacement eligibility itself. The page "
            "supports only move_clip, trim_clip, set_clip_duration, and replace_clip, "
            "plus read-only inspection of canonical Timeline revision K. A separately "
            "approved timeline.preview_media action may enable Play/Pause/seek for "
            "supported MP4/H.264 current clips through a bounded source-preview proxy. "
            "Raw MP4 transport may contain audio, but the page disables audio playback "
            "and keeps output muted; audio-stream presence/content/mix are not attested. "
            "The preview grants no write and is not final-fidelity proof. An explicit "
            "edit Save uses a server-held project pairing for timeline.apply_edit. A "
            "separately staged restore requires the independent timeline.restore_revision "
            "action and appends exact K as N+1; both paths reread canonical state before "
            "reporting success. The Browser "
            "never receives pairing proof material, a desktop/main token, a source path, "
            "or a general write capability. Neither preview nor Save is semantic "
            "confirmation, render, export, or publish authority."
        ),
        "inputSchema": _schema(
            {
                "project_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
                    "description": "Exact existing canonical MemoLens project identity.",
                }
            },
            ["project_id"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Open the MemoLens Canonical Editor",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "memolens_editor_handoff",
        "title": "Open the MemoLens Unsaved Draft Lab",
        "description": (
            "Open one validated Timeline 1.0 value in the Unsaved Draft Lab, a "
            "process-scoped, loopback-only "
            "editing surface. A supported Agent host may open the returned uiHandoff.url; "
            "otherwise it must present a user-clicked link. Call a Timeline data tool first, "
            "then pass its timeline here. Replacement candidates are kind-discriminated, "
            "exact source-bound values: video requires analysis and source-range identity, "
            "audio requires a source range, and image does not accept a source range. "
            "Split, ripple trim, move, replace, delete, volume, fit, canvas, drag reorder, "
            "undo, and redo are real in-memory draft operations; nothing is persisted, "
            "rendered, exported, or written to media."
        ),
        "inputSchema": _schema(
            {
                "timeline": {"type": "object"},
                "replacement_candidates": {
                    "type": "array",
                    "maxItems": 24,
                    "description": (
                        "Exact replacement candidates. Each candidate must declare kind and "
                        "satisfy that kind's complete contract; private locators are rejected."
                    ),
                    "items": _REPLACEMENT_CANDIDATE_INPUT,
                },
            },
            ["timeline"],
        ),
        "outputSchema": COMMON_OUTPUT,
        "annotations": {
            "title": "Open the MemoLens Unsaved Draft Lab",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
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


class _LazyMemoLensGateway:
    """Construct the Core-facing gateway only for a tool that actually needs it."""

    def __init__(self) -> None:
        self._gateway: MemoLensGateway | None = None

    def get(self) -> MemoLensGateway:
        if self._gateway is None:
            self._gateway = MemoLensGateway()
        return self._gateway


def _resolve_gateway(
    gateway: MemoLensGateway | _LazyMemoLensGateway,
) -> MemoLensGateway:
    return gateway.get() if isinstance(gateway, _LazyMemoLensGateway) else gateway


def _call_project_tool(
    name: str, args: dict[str, Any], gateway: MemoLensGateway
) -> dict[str, Any]:
    if name == "memolens_project_list":
        return gateway.project_list(
            status=args.get("status", "current"),
            limit=args.get("limit", 24),
            cursor=args.get("cursor"),
        )
    if name == "memolens_project_open":
        return gateway.project_open(args.get("project_id"))
    return gateway.project_history(
        args.get("project_id"),
        limit=args.get("limit", 50),
    )


def _call_blueprint_tool(
    name: str, args: dict[str, Any], gateway: MemoLensGateway
) -> dict[str, Any]:
    if name == "memolens_blueprint_get":
        project_id = args.get("project_id")
        if not isinstance(project_id, str):
            raise MemoLensError(
                "project_id must be a string.", code="invalid_argument"
            )
        return gateway.blueprint_get(project_id, revision=args.get("revision"))
    if name == "memolens_blueprint_history":
        project_id = args.get("project_id")
        if not isinstance(project_id, str):
            raise MemoLensError(
                "project_id must be a string.", code="invalid_argument"
            )
        return gateway.blueprint_history(project_id, limit=args.get("limit", 50))
    if name == "memolens_blueprint_shadow":
        project_id = args.get("project_id")
        if not isinstance(project_id, str):
            raise MemoLensError(
                "project_id must be a string.", code="invalid_argument"
            )
        return gateway.blueprint_shadow(project_id)
    candidate = args.get("candidate")
    if not isinstance(candidate, dict):
        raise MemoLensError(
            "candidate must be an object.", code="invalid_argument"
        )
    return gateway.blueprint_validate(candidate)


def _call_wiki_tool(
    name: str, args: dict[str, Any], gateway: MemoLensGateway
) -> dict[str, Any]:
    if name == "memolens_wiki_status":
        return gateway.wiki_status()
    if name == "memolens_wiki_list":
        return gateway.wiki_list(
            kinds=args.get("kinds"),
            limit=args.get("limit", 24),
            cursor=args.get("cursor"),
        )
    if name == "memolens_wiki_search":
        query = args.get("query")
        if not isinstance(query, str):
            raise MemoLensError("query must be a string.", code="invalid_argument")
        return gateway.wiki_search(query, limit=args.get("limit", 12))
    if name == "memolens_wiki_open":
        page_id = args.get("page_id")
        if not isinstance(page_id, str):
            raise MemoLensError("page_id must be a string.", code="invalid_argument")
        return gateway.wiki_open(page_id)
    evidence_id = args.get("evidence_id")
    if not isinstance(evidence_id, str):
        raise MemoLensError(
            "evidence_id must be a string.", code="invalid_argument"
        )
    return gateway.wiki_evidence(evidence_id)


TOOL_ARGUMENT_FIELDS: dict[str, frozenset[str]] = {
    "memolens_library_bootstrap_start": frozenset({"request_idempotency_key"}),
    "memolens_library_bootstrap_status": frozenset({"request_id"}),
    "memolens_status": frozenset(),
    "memolens_search": frozenset({"query", "limit"}),
    "memolens_creator_context": frozenset(),
    "memolens_mixed_search": frozenset({"query", "limit", "filters"}),
    "memolens_memories": frozenset({"query", "limit"}),
    "memolens_cleanup": frozenset(),
    "memolens_video_search": frozenset({"query", "limit"}),
    "memolens_media_list": frozenset({"kinds", "limit", "cursor"}),
    "memolens_media_get": frozenset({"asset_id"}),
    "memolens_wiki_status": frozenset(),
    "memolens_wiki_list": frozenset({"kinds", "limit", "cursor"}),
    "memolens_wiki_search": frozenset({"query", "limit"}),
    "memolens_wiki_open": frozenset({"page_id"}),
    "memolens_wiki_evidence": frozenset({"evidence_id"}),
    "memolens_project_list": frozenset({"status", "limit", "cursor"}),
    "memolens_project_open": frozenset({"project_id"}),
    "memolens_project_history": frozenset({"project_id", "limit"}),
    "memolens_blueprint_shadow": frozenset({"project_id"}),
    "memolens_blueprint_get": frozenset({"project_id", "revision"}),
    "memolens_blueprint_history": frozenset({"project_id", "limit"}),
    "memolens_blueprint_validate": frozenset({"candidate"}),
    "memolens_inbox_list": frozenset({"state", "kinds", "limit", "cursor"}),
    "memolens_timeline_draft": frozenset(
        {"project_id", "items", "created_at", "format", "brief_revision"}
    ),
    "memolens_timeline_revise_draft": frozenset(
        {"timeline", "operations", "created_at"}
    ),
    "memolens_timeline_validate": frozenset({"timeline"}),
    "memolens_canonical_editor_handoff": frozenset({"project_id"}),
    "memolens_editor_handoff": frozenset(
        {"timeline", "replacement_candidates"}
    ),
    "memolens_timeline_list": frozenset({"project_id", "limit", "cursor"}),
    "memolens_timeline_get": frozenset({"timeline_id", "revision"}),
}


def _call_mixed_search_tool(
    args: dict[str, Any],
    gateway: MemoLensGateway,
) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str):
        raise MemoLensError("query must be a string.", code="invalid_argument")
    filters = args.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise MemoLensError("filters must be an object.", code="invalid_argument")
    return gateway.mixed_search(
        query,
        limit=args.get("limit", 12),
        filters=filters,
    )


def _call_library_bootstrap_tool(
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    if name == "memolens_library_bootstrap_start":
        request_idempotency_key = args.get("request_idempotency_key")
        if not isinstance(request_idempotency_key, str):
            raise MemoLensError(
                "request_idempotency_key must be a string.",
                code="invalid_argument",
            )
        return start_library_bootstrap(request_idempotency_key)
    request_id = args.get("request_id")
    if not isinstance(request_id, str):
        raise MemoLensError("request_id must be a string.", code="invalid_argument")
    return library_bootstrap_status(request_id)


def call_tool(
    name: str,
    arguments: Any,
    gateway: MemoLensGateway | _LazyMemoLensGateway,
) -> dict[str, Any]:
    args = _arguments(arguments)
    if name not in TOOL_ARGUMENT_FIELDS:
        raise MemoLensError(f"Unknown tool: {name}", code="unknown_tool")
    unexpected = sorted(set(args) - TOOL_ARGUMENT_FIELDS[name])
    if unexpected:
        raise MemoLensError(
            "Tool arguments contain unsupported fields.", code="invalid_argument"
        )
    if name in {
        "memolens_library_bootstrap_start",
        "memolens_library_bootstrap_status",
    }:
        return _call_library_bootstrap_tool(name, args)
    gateway = _resolve_gateway(gateway)
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
        return _call_mixed_search_tool(args, gateway)
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
    if name.startswith("memolens_wiki_"):
        return _call_wiki_tool(name, args, gateway)
    if name.startswith("memolens_project_"):
        return _call_project_tool(name, args, gateway)
    if name.startswith("memolens_blueprint_"):
        return _call_blueprint_tool(name, args, gateway)
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
    if name == "memolens_canonical_editor_handoff":
        project_id = args.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise MemoLensError(
                "A canonical project_id is required.",
                code="canonical_editor_project_required",
            )
        try:
            return create_canonical_editor_handoff(project_id=project_id)
        except EditorServerError as exc:
            raise MemoLensError(str(exc), code=exc.code) from exc
    if name == "memolens_editor_handoff":
        try:
            return create_editor_handoff(
                timeline=args.get("timeline"),
                replacement_candidates=args.get("replacement_candidates"),
            )
        except EditorServerError as exc:
            raise MemoLensError(str(exc), code=exc.code) from exc
    if name == "memolens_timeline_list":
        return gateway.timeline_list(
            project_id=args.get("project_id"),
            limit=args.get("limit", 24),
            cursor=args.get("cursor"),
        )
    if name == "memolens_timeline_get":
        return gateway.timeline_get(
            args.get("timeline_id"), revision=args.get("revision")
        )
    raise MemoLensError(f"Unknown tool: {name}", code="unknown_tool")


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def handle_message(
    message: Any,
    gateway: MemoLensGateway | _LazyMemoLensGateway,
) -> dict[str, Any] | None:
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
                    "MemoLens MCP data tools are local and read-only. The safe default never "
                    "opens a socket and supports confirmed Creator Memory, Media Inbox, a live "
                    "read-only Media Wiki, exact canonical Creative Blueprint proposal resume "
                    "with a Blueprint-only operation ledger, candidate validation, SQLite media "
                    "search/read, and in-memory timeline drafting. "
                    "memolens_canonical_editor_handoff(project_id) is the primary project "
                    "editing entry: it derives exact current state server-side, permits only "
                    "the four canonical edit operations, and exposes canonical Timeline history "
                    "as read-only revision K. An edit Save requires timeline.apply_edit; a "
                    "separately staged K-to-N+1 restore requires the independent "
                    "timeline.restore_revision action. Both use an explicit project pairing and "
                    "finish with a canonical reread. The Browser never "
                    "receives the pairing secret. The legacy memolens_editor_handoff call opens "
                    "the Unsaved Draft Lab. Its split, trim, move, replace, delete, fit, volume, canvas, and "
                    "undo/redo operations revise transient memory only; they do not save a "
                    "Timeline, modify media, render, or export. Creation authority and "
                    "current decision authority are separate; never infer user confirmation from "
                    "an Agent proposal or from 8/8 confirmed units. The separate standard-library "
                    "CLI may request a short-lived, desktop-approved, project-scoped capability "
                    "for reversible Blueprint proposal commit/restore; it is not an MCP capability "
                    "and cannot confirm decisions, render, export, publish, delete, or write an "
                    "arbitrary path. The Wiki v0 is not generation-pinned. Do not enable local API "
                    "trust or manufacture pairing credentials for the user."
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
    gateway = _LazyMemoLensGateway()
    stream = sys.stdin.buffer
    while True:
        line = stream.readline(MCP_JSON_LIMITS.max_bytes + 2)
        if not line:
            break
        has_newline = line.endswith(b"\n")
        payload = line[:-1] if has_newline else line
        if payload.endswith(b"\r"):
            payload = payload[:-1]
        oversized = len(payload) > MCP_JSON_LIMITS.max_bytes
        if oversized and not has_newline:
            while line and not line.endswith(b"\n"):
                line = stream.readline(MCP_JSON_LIMITS.max_bytes + 2)
        if oversized:
            response = _error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        try:
            if not payload.strip():
                continue
            message = decode_strict_json(payload, limits=MCP_JSON_LIMITS)
        except StrictJsonError:
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
