"""Cold-audited canonical Export facts for standalone material search.

This module intentionally has no dependency on MemoLens Core or the backend.
The Codex and DeepSeek plugin runtimes use it against one caller-owned,
read-only SQLite snapshot before making any derivative-absence claim.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import re
import sqlite3
from typing import Any, Mapping, Sequence

from memolens_contracts import MemoLensError
from memolens_creative_blueprint import canonical_json, canonical_sha256
from memolens_persisted_blueprint import (
    _decode_document as _decode_persisted_blueprint_document,
)
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json


_TABLES = (
    "canonical_export_operations",
    "canonical_export_jobs",
    "canonical_export_revisions",
    "canonical_usage_occurrences",
    "canonical_export_receipts",
)
_MAX_OPERATIONS = 4_096
_MAX_OCCURRENCES = 262_144
_MAX_PROJECTS = 100_000
_MAX_JSON_BYTES = 128 * 1_048_576
_JSON_LIMITS = StrictJsonLimits(
    max_bytes=2_097_152,
    max_depth=32,
    max_nodes=100_000,
    max_object_items=4_096,
    max_array_items=262_144,
    max_string_chars=1_048_576,
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00\Z"
)
_PACKAGE_ROLES = (
    "final_video",
    "script",
    "package_manifest",
    "human_usage_list",
    "completion_marker",
)
_JOB_STAGES = {
    "requested": "requested",
    "rendering": "rendering",
    "packaging": "packaging",
    "commit_pending": "commit_pending",
    "succeeded": "completed",
    "failed": "failed",
    "cancelling": "cancelling",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}
_TIMELINE_COMPILER = {
    "id": "memolens.timeline-lowerer/v1",
    "media": "image_and_video_span",
    "transitions": "hard_cut",
    "audio": "silent",
    "subtitles": "none",
}
_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:5"}


def _integrity() -> MemoLensError:
    return MemoLensError(
        "Canonical Export derivative authority failed integrity validation.",
        code="derivative_authority_integrity_error",
    )


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _valid_timestamp(value: object) -> bool:
    if not (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and _UTC_TIMESTAMP.fullmatch(value) is not None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (OverflowError, ValueError):
        return False
    return parsed.utcoffset() == timedelta(0) and parsed.isoformat() == value


def _strict_json(raw: object, expected_type: type[Any]) -> Any:
    if not isinstance(raw, str):
        raise _integrity()
    try:
        value = decode_strict_json(
            raw.encode("utf-8"),
            require_object=expected_type is dict,
            limits=_JSON_LIMITS,
        )
    except (StrictJsonError, UnicodeError) as exc:
        raise _integrity() from exc
    if type(value) is not expected_type or canonical_json(value) != raw:
        raise _integrity()
    return value


def _require_path_free(value: object) -> None:
    forbidden = {
        "path",
        "canonical_path",
        "relative_path",
        "root_path",
        "output_path",
        "directory",
        "credential",
        "credentials",
        "token",
        "runtime_token",
        "native_nonce",
    }

    def visit(item: object, depth: int) -> None:
        if depth > 32:
            raise _integrity()
        if item is None or type(item) in {bool, int}:
            return
        if type(item) is str:
            if (
                item.startswith(("/", "file://", "./", "../"))
                or (len(item) >= 3 and item[0].isascii() and item[0].isalpha() and item[1:3] == ":/")
                or "\\" in item
                or "/../" in item
                or "/./" in item
                or "\x00" in item
            ):
                raise _integrity()
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or key.casefold() in forbidden:
                    raise _integrity()
                visit(child, depth + 1)
            return
        raise _integrity()

    visit(value, 0)


def _safe_package_basename(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 1 <= len(value) <= 120
        and value not in {".", ".."}
        and value[0].isascii()
        and value[0].isalnum()
        and value[-1] not in {".", " "}
        and ".." not in value
        and all(
            character.isascii()
            and (character.isalnum() or character in "._-")
            for character in value
        )
    )


def _database_uuid(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT database_uuid FROM database_meta WHERE singleton=1 LIMIT 2"
    ).fetchall()
    if len(rows) != 1 or not _valid_identifier(rows[0]["database_uuid"]):
        raise _integrity()
    return str(rows[0]["database_uuid"])


def _audit_export_root_anchors(connection: sqlite3.Connection) -> None:
    """Cross-check the durable root created with every canonical command.

    ``output_roots`` is outside the five-table Export ledger, but Core
    materializes a ``user_export`` row in the same transaction that creates
    operation/job/receipt.  It therefore closes the otherwise indistinguishable
    all-five-ledger-rows-deleted case without introducing a new manifest.

    The job's ``output_root_id`` and the root's ``kind`` are durable identity.
    ``permission_fingerprint``, ``status``, and ``updated_at`` are mutable
    operational state: Core may refresh them after the historical command has
    completed.  The operation/job/attestation/receipt chain already freezes and
    authenticates the command-time fingerprint, so comparing that historical
    value with the root's current fingerprint would reject a legitimate refresh.
    """

    broken = connection.execute(
        """SELECT 1
             FROM output_roots AS root
             LEFT JOIN canonical_export_jobs AS job
               ON job.output_root_id=root.id
            WHERE root.kind='user_export' AND job.id IS NULL
            LIMIT 1"""
    ).fetchone()
    if broken is not None:
        raise _integrity()
    broken = connection.execute(
        """SELECT 1
             FROM canonical_export_jobs AS job
             LEFT JOIN output_roots AS root ON root.id=job.output_root_id
            WHERE root.id IS NULL
               OR root.kind!='user_export'
            LIMIT 1"""
    ).fetchone()
    if broken is not None:
        raise _integrity()


def _operation(row: Mapping[str, object]) -> dict[str, object]:
    presentation = _strict_json(row["presentation_json"], dict)
    result = _strict_json(row["result_json"], dict)
    binding = presentation.get("timeline_binding")
    blueprint = binding.get("blueprint_binding") if isinstance(binding, dict) else None
    coverage = binding.get("coverage_binding") if isinstance(binding, dict) else None
    envelope = {
        "object": "canonical_export.request",
        "schema_version": "1",
        "database_uuid": presentation.get("database_uuid"),
        "presentation": presentation,
        "presentation_sha256": row["presentation_sha256"],
        "main_native_user_gesture": True,
        "runtime_epoch": row["runtime_epoch"],
        "native_nonce": row["native_nonce"],
        "output_root_id": row["output_root_id"],
        "permission_fingerprint": row["permission_fingerprint"],
        "package_basename": row["package_basename"],
        "profile": row["profile"],
    }
    if not (
        _valid_identifier(row["id"])
        and _valid_identifier(row["project_id"])
        and type(row["sequence"]) is int
        and 1 <= int(row["sequence"]) <= _MAX_OPERATIONS
        and row["command_type"] == "export.create"
        and row["command_version"] == "1"
        and row["effect_class"] == "native_user_egress"
        and row["main_native_user_gesture"] == 1
        and _valid_identifier(row["runtime_epoch"])
        and _valid_identifier(row["native_nonce"])
        and _valid_identifier(presentation.get("database_uuid"))
        and canonical_sha256(presentation) == row["presentation_sha256"]
        and canonical_sha256(envelope) == row["request_sha256"]
        and isinstance(binding, dict)
        and isinstance(blueprint, dict)
        and isinstance(coverage, dict)
        and row["timeline_revision"] == binding.get("revision")
        and row["timeline_revision_sha256"] == binding.get("revision_sha256")
        and row["timeline_id"] == binding.get("timeline_id")
        and row["timeline_content_sha256"] == binding.get("timeline_content_sha256")
        and row["timeline_source_bindings_sha256"] == binding.get("source_bindings_sha256")
        and row["blueprint_revision"] == blueprint.get("revision")
        and row["blueprint_content_sha256"] == blueprint.get("content_sha256")
        and row["blueprint_semantic_sha256"] == blueprint.get("semantic_sha256")
        and row["blueprint_operation_id"] == blueprint.get("operation_id")
        and row["coverage_revision"] == coverage.get("revision")
        and row["coverage_content_sha256"] == coverage.get("content_sha256")
        and row["coverage_evidence_manifest_sha256"] == coverage.get("evidence_manifest_sha256")
        and row["coverage_operation_id"] == coverage.get("operation_id")
        and _valid_identifier(row["output_root_id"])
        and _safe_package_basename(row["package_basename"])
        and row["profile"] == "export-1080p"
        and _valid_identifier(row["job_id"])
        and _valid_timestamp(row["created_at"])
    ):
        raise _integrity()
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "sequence": int(row["sequence"]),
        "parent_operation_id": row["parent_operation_id"],
        "presentation": presentation,
        "presentation_sha256": str(row["presentation_sha256"]),
        "request_sha256": str(row["request_sha256"]),
        "timeline_binding": binding,
        "output_root_id": str(row["output_root_id"]),
        "permission_fingerprint": str(row["permission_fingerprint"]),
        "package_basename": str(row["package_basename"]),
        "profile": str(row["profile"]),
        "job_id": str(row["job_id"]),
        "result": result,
        "created_at": str(row["created_at"]),
    }


def _timeline_snapshot(
    connection: sqlite3.Connection,
    *,
    operation: Mapping[str, object],
    database_uuid: str,
) -> dict[str, object]:
    binding = operation["timeline_binding"]
    assert isinstance(binding, dict)
    rows = connection.execute(
        """SELECT * FROM canonical_timeline_revisions
             WHERE project_id=? AND revision=? AND revision_sha256=? LIMIT 2""",
        (
            operation["project_id"],
            binding.get("revision"),
            binding.get("revision_sha256"),
        ),
    ).fetchall()
    if len(rows) != 1:
        raise _integrity()
    row = rows[0]
    timeline = _strict_json(row["timeline_json"], dict)
    sources = _strict_json(row["source_bindings_json"], list)
    _require_path_free(timeline)
    _require_path_free(sources)
    blueprint = {
        "revision": row["blueprint_revision"],
        "content_sha256": row["blueprint_content_sha256"],
        "semantic_sha256": row["blueprint_semantic_sha256"],
        "operation_id": row["blueprint_operation_id"],
    }
    coverage = {
        "revision": row["coverage_revision"],
        "content_sha256": row["coverage_content_sha256"],
        "evidence_manifest_sha256": row["coverage_evidence_manifest_sha256"],
        "operation_id": row["coverage_operation_id"],
    }
    exact_binding = {
        "revision": row["revision"],
        "revision_sha256": row["revision_sha256"],
        "timeline_id": row["timeline_id"],
        "timeline_content_sha256": row["timeline_content_sha256"],
        "source_bindings_sha256": row["source_bindings_sha256"],
        "blueprint_binding": blueprint,
        "coverage_binding": coverage,
    }
    revision_material = {
        "project_id": row["project_id"],
        "revision": row["revision"],
        "parent_revision": row["parent_revision"],
        "parent_revision_sha256": row["parent_revision_sha256"],
        "timeline_id": row["timeline_id"],
        "timeline_content_sha256": row["timeline_content_sha256"],
        "blueprint_binding": blueprint,
        "coverage_binding": coverage,
        "source_bindings_sha256": row["source_bindings_sha256"],
        "operation_id": row["operation_id"],
    }
    parent = (
        None
        if row["parent_revision"] is None
        else {
            "revision": row["parent_revision"],
            "content_sha256": row["parent_revision_sha256"],
        }
    )
    if not (
        exact_binding == binding
        and row["project_id"] == operation["project_id"]
        and type(row["revision"]) is int
        and 1 <= int(row["revision"]) <= 1_000_000
        and (
            row["revision"] == 1
            and row["parent_revision"] is None
            and row["parent_revision_sha256"] is None
            or row["revision"] > 1
            and row["parent_revision"] == row["revision"] - 1
            and _valid_digest(row["parent_revision_sha256"])
        )
        and row["schema_version"] in {"1", "2"}
        and row["compiler_id"] == _TIMELINE_COMPILER["id"]
        and _valid_identifier(row["timeline_id"])
        and _valid_identifier(row["operation_id"])
        and _valid_timestamp(row["created_at"])
        and canonical_sha256(timeline) == row["timeline_content_sha256"]
        and canonical_sha256(sources) == row["source_bindings_sha256"]
        and canonical_sha256(revision_material) == row["revision_sha256"]
        and set(timeline)
        == {
            "object",
            "schema_version",
            "timeline_id",
            "project_id",
            "revision",
            "parent",
            "blueprint_binding",
            "coverage_binding",
            "compiler",
            "output",
            "tracks",
        }
        and timeline.get("object") == "memolens.canonical_timeline"
        and timeline.get("schema_version") == row["schema_version"]
        and timeline.get("timeline_id") == row["timeline_id"]
        and timeline.get("project_id") == row["project_id"]
        and timeline.get("revision") == row["revision"]
        and timeline.get("parent") == parent
        and timeline.get("blueprint_binding") == blueprint
        and timeline.get("coverage_binding") == coverage
        and timeline.get("compiler") == _TIMELINE_COMPILER
    ):
        raise _integrity()
    output = timeline.get("output")
    tracks = timeline.get("tracks")
    if not (
        type(output) is dict
        and set(output) == {"duration_ms", "aspect_ratio"}
        and type(output.get("duration_ms")) is int
        and 0 < int(output["duration_ms"]) <= 7 * 24 * 60 * 60 * 1_000
        and output.get("aspect_ratio") in _ASPECT_RATIOS
        and type(tracks) is list
        and len(tracks) == 1
        and type(tracks[0]) is dict
        and set(tracks[0]) == {"track_id", "kind", "clips"}
        and _valid_identifier(tracks[0].get("track_id"))
        and tracks[0].get("kind") == "primary_visual"
        and type(tracks[0].get("clips")) is list
        and 1 <= len(tracks[0]["clips"]) <= 256
        and len(sources) == len(tracks[0]["clips"])
    ):
        raise _integrity()
    occurrences: list[dict[str, object]] = []
    cursor = 0
    for ordinal, (clip, source) in enumerate(
        zip(tracks[0]["clips"], sources, strict=True)
    ):
        if not (type(clip) is dict and type(source) is dict):
            raise _integrity()
        media_kind = clip.get("media_kind")
        common = ("clip_id", "evidence_ref", "asset_id", "asset_sha256", "media_kind")
        if not (
            media_kind in {"image", "video"}
            and clip.get("ordinal") == ordinal
            and _valid_identifier(clip.get("clip_id"))
            and _valid_identifier(clip.get("asset_id"))
            and _valid_digest(clip.get("asset_sha256"))
            and clip.get("fit") == "cover"
            and clip.get("audio_enabled") is False
            and type(clip.get("start_ms")) is int
            and type(clip.get("end_ms")) is int
            and clip["start_ms"] == cursor
            and clip["end_ms"] > clip["start_ms"]
            and all(source.get(key) == clip.get(key) for key in common)
            and _valid_identifier(source.get("asset_source_id"))
            and _valid_digest(source.get("coverage_proof_sha256"))
        ):
            raise _integrity()
        source_start = clip.get("source_in_ms") if media_kind == "video" else None
        source_end = clip.get("source_out_ms") if media_kind == "video" else None
        if media_kind == "video" and not (
            type(source_start) is int
            and type(source_end) is int
            and 0 <= source_start < source_end
            and source_end - source_start == clip["end_ms"] - clip["start_ms"]
            and source.get("source_in_ms") == source_start
            and source.get("source_out_ms") == source_end
        ):
            raise _integrity()
        occurrences.append(
            {
                "ordinal": ordinal,
                "clip_id": clip["clip_id"],
                "asset_id": clip["asset_id"],
                "asset_sha256": clip["asset_sha256"],
                "asset_source_id": source["asset_source_id"],
                "media_kind": media_kind,
                "timeline_start_ms": clip["start_ms"],
                "timeline_end_ms": clip["end_ms"],
                "source_start_ms": source_start,
                "source_end_ms": source_end,
                "source_binding_sha256": canonical_sha256(source),
            }
        )
        cursor = int(clip["end_ms"])
    if cursor != output["duration_ms"]:
        raise _integrity()
    expected_presentation = {
        "object": "canonical_export.presentation",
        "schema_version": "1",
        "database_uuid": database_uuid,
        "project_id": operation["project_id"],
        "timeline_binding": exact_binding,
        "profile": "export-1080p",
        "package_roles": list(_PACKAGE_ROLES),
        "duration_ms": output["duration_ms"],
        "aspect_ratio": output["aspect_ratio"],
        "clip_count": len(occurrences),
    }
    if operation["presentation"] != expected_presentation:
        raise _integrity()
    return {
        "timeline": timeline,
        "source_bindings": sources,
        "occurrences": occurrences,
    }


def _artifact_proof(value: object) -> dict[str, object]:
    keys = {
        "video_sha256",
        "video_size_bytes",
        "video_duration_ms",
        "script_sha256",
        "script_size_bytes",
        "package_manifest",
        "package_manifest_sha256",
        "human_usage_sha256",
        "completion_marker_sha256",
    }
    if type(value) is not dict or set(value) != keys:
        raise _integrity()
    proof = deepcopy(value)
    manifest = proof.get("package_manifest")
    if not (
        _valid_digest(proof.get("video_sha256"))
        and type(proof.get("video_size_bytes")) is int
        and int(proof["video_size_bytes"]) >= 0
        and type(proof.get("video_duration_ms")) is int
        and int(proof["video_duration_ms"]) > 0
        and _valid_digest(proof.get("script_sha256"))
        and type(proof.get("script_size_bytes")) is int
        and int(proof["script_size_bytes"]) >= 0
        and type(manifest) is dict
        and _valid_digest(proof.get("package_manifest_sha256"))
        and _valid_digest(proof.get("human_usage_sha256"))
        and _valid_digest(proof.get("completion_marker_sha256"))
        and manifest.get("video")
        == {
            "sha256": proof["video_sha256"],
            "size_bytes": proof["video_size_bytes"],
            "duration_ms": proof["video_duration_ms"],
        }
        and manifest.get("script")
        == {
            "sha256": proof["script_sha256"],
            "size_bytes": proof["script_size_bytes"],
        }
    ):
        raise _integrity()
    _require_path_free(proof)
    physical = _physical_bindings(manifest)
    if any(
        proof[key] != physical[key]
        for key in (
            "package_manifest_sha256",
            "human_usage_sha256",
            "completion_marker_sha256",
        )
    ):
        raise _integrity()
    return proof


def _attestation(row: Mapping[str, object]) -> dict[str, object] | None:
    raw = row["commit_attestation_json"]
    digest = row["commit_attestation_sha256"]
    if raw is None and digest is None:
        return None
    if raw is None or not _valid_digest(digest):
        raise _integrity()
    value = _strict_json(raw, dict)
    if set(value) != {
        "object",
        "schema_version",
        "database_uuid",
        "job_id",
        "operation_id",
        "project_id",
        "presentation_sha256",
        "request_sha256",
        "timeline_revision_sha256",
        "output_binding",
        "artifact_proof",
        "usage_sha256",
        "runtime_manifest_sha256",
        "attested_at",
    }:
        raise _integrity()
    proof = _artifact_proof(value.get("artifact_proof"))
    manifest = proof["package_manifest"]
    if not (
        value.get("object") == "canonical_export.commit_attestation"
        and value.get("schema_version") == "1"
        and value.get("database_uuid") == row["database_uuid"]
        and value.get("job_id") == row["id"]
        and value.get("operation_id") == row["operation_id"]
        and value.get("project_id") == row["project_id"]
        and value.get("presentation_sha256") == row["presentation_sha256"]
        and value.get("request_sha256") == row["request_sha256"]
        and value.get("timeline_revision_sha256") == row["timeline_revision_sha256"]
        and value.get("output_binding")
        == {
            "output_root_id": row["output_root_id"],
            "permission_fingerprint": row["permission_fingerprint"],
            "package_basename": row["package_basename"],
            "profile": row["profile"],
        }
        and _valid_digest(value.get("usage_sha256"))
        and _valid_digest(value.get("runtime_manifest_sha256"))
        and _valid_timestamp(value.get("attested_at"))
        and manifest.get("usage_sha256") == value["usage_sha256"]
        and manifest.get("runtime_manifest_sha256") == value["runtime_manifest_sha256"]
        and canonical_sha256(value) == digest
    ):
        raise _integrity()
    return {**value, "attestation_sha256": str(digest)}


def _job(
    row: Mapping[str, object],
    *,
    database_uuid: str,
    operation: Mapping[str, object],
    revision_present: bool,
) -> tuple[dict[str, object], dict[str, object] | None]:
    error = None if row["error_json"] is None else _strict_json(row["error_json"], dict)
    if error is not None and not (
        set(error) == {"code", "message", "retryable"}
        and _valid_identifier(error.get("code"))
        and isinstance(error.get("message"), str)
        and 1 <= len(error["message"]) <= 1_000
        and type(error.get("retryable")) is bool
    ):
        raise _integrity()
    status = row["status"]
    attestation = _attestation(row)
    if not (
        _valid_identifier(row["id"])
        and row["database_uuid"] == database_uuid
        and row["id"] == operation["job_id"]
        and row["project_id"] == operation["project_id"]
        and row["operation_id"] == operation["id"]
        and row["presentation_sha256"] == operation["presentation_sha256"]
        and row["request_sha256"] == operation["request_sha256"]
        and row["timeline_revision"] == operation["timeline_binding"]["revision"]
        and row["timeline_revision_sha256"] == operation["timeline_binding"]["revision_sha256"]
        and row["timeline_content_sha256"] == operation["timeline_binding"]["timeline_content_sha256"]
        and row["timeline_source_bindings_sha256"] == operation["timeline_binding"]["source_bindings_sha256"]
        and row["output_root_id"] == operation["output_root_id"]
        and row["permission_fingerprint"] == operation["permission_fingerprint"]
        and row["package_basename"] == operation["package_basename"]
        and row["profile"] == operation["profile"] == "export-1080p"
        and status in _JOB_STAGES
        and row["stage"] == _JOB_STAGES[status]
        and type(row["progress"]) in {int, float}
        and 0 <= float(row["progress"]) <= 1
        and type(row["attempt"]) is int
        and 1 <= int(row["attempt"]) <= 1_000
        and row["cancel_requested"] in {0, 1}
        and _valid_timestamp(row["created_at"])
        and all(
            value is None or _valid_timestamp(value)
            for value in (row["started_at"], row["heartbeat_at"], row["finished_at"])
        )
        and (status != "succeeded" or error is None)
        and (status not in {"commit_pending", "succeeded"} or attestation is not None)
        and (status not in {"requested", "rendering", "packaging"} or attestation is None)
        and (status == "succeeded") == revision_present
    ):
        raise _integrity()
    return (
        {
            "id": str(row["id"]),
            "project_id": str(row["project_id"]),
            "operation_id": str(row["operation_id"]),
            "presentation_sha256": str(row["presentation_sha256"]),
            "request_sha256": str(row["request_sha256"]),
            "timeline_binding": {
                "revision": int(row["timeline_revision"]),
                "revision_sha256": str(row["timeline_revision_sha256"]),
                "timeline_content_sha256": str(row["timeline_content_sha256"]),
                "source_bindings_sha256": str(
                    row["timeline_source_bindings_sha256"]
                ),
            },
            "output_binding": {
                "output_root_id": str(row["output_root_id"]),
                "permission_fingerprint": str(row["permission_fingerprint"]),
                "package_basename": str(row["package_basename"]),
                "profile": str(row["profile"]),
            },
            "status": "requested",
            "stage": "requested",
            "progress": 0.0,
            "attempt": 1,
            "cancel_requested": False,
            "error": None,
            "created_at": str(row["created_at"]),
            "started_at": None,
            "heartbeat_at": None,
            "finished_at": None,
            "result": None,
        },
        attestation,
    )


def _receipt(
    row: Mapping[str, object],
    *,
    database_uuid: str,
    operation: Mapping[str, object],
) -> None:
    response = _strict_json(row["response_json"], dict)
    response_sha256 = canonical_sha256(response)
    material = {
        "database_uuid": row["database_uuid"],
        "authenticated_principal": row["authenticated_principal"],
        "project_id": row["project_id"],
        "command_type": row["command_type"],
        "command_version": row["command_version"],
        "idempotency_key": row["idempotency_key"],
        "request_sha256": row["request_sha256"],
        "operation_id": row["operation_id"],
        "response_status": row["response_status"],
        "response_sha256": row["response_sha256"],
        "resource_id": row["resource_id"],
        "created_at": row["created_at"],
    }
    if not (
        row["database_uuid"] == database_uuid
        and row["authenticated_principal"] == "main_native_user_gesture"
        and row["project_id"] == operation["project_id"]
        and row["command_type"] == "export.create"
        and row["command_version"] == "1"
        and _valid_identifier(row["idempotency_key"])
        and row["request_sha256"] == operation["request_sha256"]
        and row["operation_id"] == operation["id"]
        and row["response_status"] == 201
        and response_sha256 == row["response_sha256"]
        and canonical_sha256(material) == row["receipt_sha256"]
        and row["resource_id"] == operation["job_id"]
        and response == operation["result"]
        and _valid_timestamp(row["created_at"])
        and str(row["created_at"]) >= str(operation["created_at"])
    ):
        raise _integrity()


def _valid_occurrence(value: object) -> bool:
    if type(value) is not dict or set(value) != {
        "ordinal",
        "clip_id",
        "asset_id",
        "asset_sha256",
        "asset_source_id",
        "media_kind",
        "timeline_start_ms",
        "timeline_end_ms",
        "source_start_ms",
        "source_end_ms",
        "source_binding_sha256",
    }:
        return False
    media_kind = value.get("media_kind")
    return bool(
        type(value.get("ordinal")) is int
        and 0 <= value["ordinal"] <= 1_000_000
        and all(
            _valid_identifier(value.get(key))
            for key in ("clip_id", "asset_id", "asset_source_id")
        )
        and _valid_digest(value.get("asset_sha256"))
        and _valid_digest(value.get("source_binding_sha256"))
        and media_kind in {"image", "video"}
        and type(value.get("timeline_start_ms")) is int
        and type(value.get("timeline_end_ms")) is int
        and 0 <= value["timeline_start_ms"] < value["timeline_end_ms"]
        and (
            media_kind == "image"
            and value.get("source_start_ms") is None
            and value.get("source_end_ms") is None
            or media_kind == "video"
            and type(value.get("source_start_ms")) is int
            and type(value.get("source_end_ms")) is int
            and 0 <= value["source_start_ms"] < value["source_end_ms"]
        )
    )


def _occurrence(row: Mapping[str, object]) -> dict[str, object]:
    value = {
        key: row[key]
        for key in (
            "ordinal",
            "clip_id",
            "asset_id",
            "asset_sha256",
            "asset_source_id",
            "media_kind",
            "timeline_start_ms",
            "timeline_end_ms",
            "source_start_ms",
            "source_end_ms",
            "source_binding_sha256",
        )
    }
    if not (
        _valid_occurrence(value)
        and canonical_sha256(value) == row["occurrence_sha256"]
        and _valid_timestamp(row["created_at"])
    ):
        raise _integrity()
    return value


def _package_manifest(
    *,
    operation: Mapping[str, object],
    row: Mapping[str, object],
    occurrences: Sequence[Mapping[str, object]],
    runtime_manifest: Mapping[str, object],
) -> dict[str, object]:
    return {
        "object": "canonical_export.package_manifest",
        "schema_version": "1",
        "export_id": operation["id"],
        "job_id": row["job_id"],
        "project_id": row["project_id"],
        "timeline_binding": deepcopy(operation["timeline_binding"]),
        "profile": row["profile"],
        "package_basename": row["package_basename"],
        "package_roles": list(_PACKAGE_ROLES),
        "required_files": [
            "video.mp4",
            "script.txt",
            "manifest.json",
            "使用清单.txt",
            ".memolens-complete.json",
        ],
        "video": {
            "sha256": row["video_sha256"],
            "size_bytes": row["video_size_bytes"],
            "duration_ms": row["video_duration_ms"],
        },
        "script": {
            "sha256": row["script_sha256"],
            "size_bytes": row["script_size_bytes"],
        },
        "usage_occurrences": [deepcopy(dict(item)) for item in occurrences],
        "usage_sha256": canonical_sha256(list(occurrences)),
        "runtime_manifest": deepcopy(dict(runtime_manifest)),
        "runtime_manifest_sha256": canonical_sha256(runtime_manifest),
        "source_policy": {
            "library_originals_copied": False,
            "library_originals_moved": False,
            "library_originals_modified": False,
            "automatic_upload": False,
        },
        "optional_roles": {"subtitle": "absent", "cover": "absent"},
    }


def _human_usage_text(manifest: Mapping[str, object]) -> str:
    occurrences = manifest.get("usage_occurrences")
    video = manifest.get("video")
    runtime = manifest.get("runtime_manifest")
    actual_read = runtime.get("actual_read_manifest") if isinstance(runtime, dict) else None
    snapshots = actual_read.get("snapshots") if isinstance(actual_read, dict) else None
    if not (
        type(occurrences) is list
        and all(_valid_occurrence(item) for item in occurrences)
        and isinstance(video, dict)
        and _valid_digest(video.get("sha256"))
        and type(snapshots) is list
    ):
        raise _integrity()
    labels = {
        str(item.get("asset_source_id")): str(item["source_label"])
        for item in snapshots
        if isinstance(item, dict) and isinstance(item.get("source_label"), str)
    }
    lines = [
        "MemoLens 使用清单",
        f"项目：{manifest.get('project_id', 'unknown')}",
        f"导出：{manifest.get('export_id', 'unknown')}",
        f"成片 SHA-256：{video['sha256']}",
        "",
        "素材出现（按 Timeline 原始顺序，重复出现保留）：",
    ]
    for item in occurrences:
        timeline = f"[{item['timeline_start_ms']},{item['timeline_end_ms']}) ms"
        label = labels.get(str(item["asset_source_id"]), str(item["asset_id"]))
        source = (
            "图片（无 source interval）"
            if item["media_kind"] == "image"
            else f"视频 [{item['source_start_ms']},{item['source_end_ms']}) ms"
        )
        lines.append(
            f"{int(item['ordinal']) + 1}. {label}；{source}；Timeline {timeline}；"
            f"asset={item['asset_id']}；clip={item['clip_id']}"
        )
    lines += [
        "",
        "原件声明：本轻量包未复制、移动、删除或修改 Library 原素材。",
        "区间语义：视频 source interval 严格采用左闭右开 [start,end) 毫秒。",
        "",
    ]
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > 1_048_576:
        raise _integrity()
    return result


def _physical_bindings(manifest: Mapping[str, object]) -> dict[str, str]:
    manifest_sha256 = canonical_sha256(manifest)
    human_sha256 = hashlib.sha256(
        _human_usage_text(manifest).encode("utf-8")
    ).hexdigest()
    video = manifest.get("video")
    script = manifest.get("script")
    if not (
        isinstance(video, dict)
        and isinstance(script, dict)
        and _valid_digest(video.get("sha256"))
        and _valid_digest(script.get("sha256"))
        and _safe_package_basename(manifest.get("package_basename"))
    ):
        raise _integrity()
    marker = {
        "object": "memolens.lightweight_export_package_completion",
        "schema_version": "1",
        "completion_state": "complete",
        "package_basename": manifest["package_basename"],
        "package_manifest_sha256": manifest_sha256,
        "required_file_sha256": {
            "video.mp4": video["sha256"],
            "script.txt": script["sha256"],
            "manifest.json": manifest_sha256,
            "使用清单.txt": human_sha256,
        },
    }
    return {
        "package_manifest_sha256": manifest_sha256,
        "human_usage_sha256": human_sha256,
        "completion_marker_sha256": canonical_sha256(marker),
    }


def _validate_runtime_manifest(
    connection: sqlite3.Connection,
    value: Mapping[str, object],
    *,
    timeline_snapshot: Mapping[str, object],
) -> None:
    fields = {
        "object",
        "schema_version",
        "renderer",
        "profile",
        "timeline_content_sha256",
        "timeline_source_bindings_sha256",
        "width",
        "height",
        "fps",
        "audio",
        "fit",
        "transitions",
        "actual_read_manifest",
        "actual_read_manifest_sha256",
        "runtime_identity",
    }
    timeline = timeline_snapshot.get("timeline")
    sources = timeline_snapshot.get("source_bindings")
    if not (
        type(value) is dict
        and set(value) == fields
        and type(timeline) is dict
        and type(sources) is list
    ):
        raise _integrity()
    output = timeline.get("output")
    dimensions = (
        {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:5": (1080, 1350),
        }.get(output.get("aspect_ratio"))
        if isinstance(output, dict)
        else None
    )
    actual = value.get("actual_read_manifest")
    if not (
        dimensions is not None
        and value.get("object") == "memolens.canonical_export_runtime_manifest"
        and value.get("schema_version") == "1"
        and value.get("renderer") == "memolens.canonical-export-artifacts/v1"
        and value.get("profile") == "export-1080p"
        and value.get("timeline_content_sha256")
        == canonical_sha256(timeline)
        and value.get("timeline_source_bindings_sha256")
        == canonical_sha256(sources)
        and (value.get("width"), value.get("height")) == dimensions
        and value.get("fps") == 30
        and value.get("audio") == "silent"
        and value.get("fit") == "cover"
        and value.get("transitions") == "hard_cut"
        and type(actual) is dict
        and type(value.get("runtime_identity")) is dict
        and canonical_sha256(actual) == value.get("actual_read_manifest_sha256")
    ):
        raise _integrity()
    snapshots = actual.get("snapshots")
    if not (
        set(actual)
        == {
            "object",
            "schema_version",
            "timeline_content_sha256",
            "source_bindings",
            "source_bindings_sha256",
            "snapshots",
        }
        and actual.get("object") == "memolens.canonical_actual_read_manifest"
        and actual.get("schema_version") == "1"
        and actual.get("timeline_content_sha256") == canonical_sha256(timeline)
        and actual.get("source_bindings") == sources
        and actual.get("source_bindings_sha256") == canonical_sha256(sources)
        and type(snapshots) is list
        and len(snapshots) == len(sources)
    ):
        raise _integrity()
    snapshot_fields = {
        "ordinal",
        "clip_id",
        "asset_source_id",
        "asset_id",
        "asset_sha256",
        "size_bytes",
        "media_kind",
        "source_label",
        "source_label_role",
    }
    for ordinal, (snapshot, source) in enumerate(
        zip(snapshots, sources, strict=True)
    ):
        asset = (
            connection.execute(
                "SELECT file_size FROM assets WHERE id=? AND sha256=?",
                (source.get("asset_id"), source.get("asset_sha256")),
            ).fetchone()
            if isinstance(source, dict)
            else None
        )
        label = snapshot.get("source_label") if isinstance(snapshot, dict) else None
        if not (
            type(snapshot) is dict
            and type(source) is dict
            and set(snapshot) == snapshot_fields
            and snapshot.get("ordinal") == ordinal
            and all(
                snapshot.get(key) == source.get(key)
                for key in (
                    "clip_id",
                    "asset_source_id",
                    "asset_id",
                    "asset_sha256",
                    "media_kind",
                )
            )
            and asset is not None
            and snapshot.get("size_bytes") == asset["file_size"]
            and isinstance(label, str)
            and 1 <= len(label) <= 512
            and label not in {".", ".."}
            and "/" not in label
            and "\\" not in label
            and "\x00" not in label
            and snapshot.get("source_label_role") == "locator_hint_only"
        ):
            raise _integrity()
    _require_path_free(value)


def _script_projection(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    blueprint_binding: Mapping[str, object],
) -> dict[str, object]:
    row = connection.execute(
        """SELECT * FROM creative_blueprint_revisions
             WHERE project_id=? AND revision=? AND content_sha256=?
               AND semantic_sha256=? AND operation_id=?""",
        (
            project_id,
            blueprint_binding.get("revision"),
            blueprint_binding.get("content_sha256"),
            blueprint_binding.get("semantic_sha256"),
            blueprint_binding.get("operation_id"),
        ),
    ).fetchone()
    if row is None:
        raise _integrity()
    try:
        document = _decode_persisted_blueprint_document(dict(row))
    except (MemoLensError, TypeError, ValueError) as exc:
        raise _integrity() from exc
    semantic = document.get("semantic")
    script = semantic.get("script") if isinstance(semantic, dict) else None
    blocks = script.get("blocks") if isinstance(script, dict) else None
    if type(blocks) is not list or not blocks:
        raise _integrity()
    normalized: list[str] = []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str) or "\x00" in text:
            raise _integrity()
        normalized.append(
            text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        )
    projected = "\n\n".join(normalized) + "\n"
    try:
        encoded = projected.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _integrity() from exc
    if not encoded or len(encoded) > 4 * 1_048_576:
        raise _integrity()
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _revision(
    row: Mapping[str, object],
    *,
    connection: sqlite3.Connection,
    operation: Mapping[str, object],
    occurrences: Sequence[Mapping[str, object]],
    attestation: Mapping[str, object] | None,
    timeline_snapshot: Mapping[str, object],
) -> dict[str, object]:
    package_manifest = _strict_json(row["package_manifest_json"], dict)
    runtime_manifest = _strict_json(row["runtime_manifest_json"], dict)
    _require_path_free(package_manifest)
    _require_path_free(runtime_manifest)
    _validate_runtime_manifest(
        connection,
        runtime_manifest,
        timeline_snapshot=timeline_snapshot,
    )
    timeline = operation["timeline_binding"]
    blueprint = timeline["blueprint_binding"]
    coverage = timeline["coverage_binding"]
    output = {
        "output_root_id": operation["output_root_id"],
        "permission_fingerprint": operation["permission_fingerprint"],
        "package_basename": operation["package_basename"],
        "profile": operation["profile"],
    }
    video = {
        "sha256": row["video_sha256"],
        "size_bytes": row["video_size_bytes"],
        "duration_ms": row["video_duration_ms"],
    }
    script = {
        "sha256": row["script_sha256"],
        "size_bytes": row["script_size_bytes"],
    }
    expected_script = _script_projection(
        connection,
        project_id=str(row["project_id"]),
        blueprint_binding=blueprint,
    )
    expected_manifest = _package_manifest(
        operation=operation,
        row=row,
        occurrences=occurrences,
        runtime_manifest=runtime_manifest,
    )
    physical = _physical_bindings(expected_manifest)
    material = {
        "project_id": row["project_id"],
        "revision": row["revision"],
        "job_id": row["job_id"],
        "operation_id": row["operation_id"],
        "timeline_binding": timeline,
        "output_binding": output,
        "video": video,
        "script": script,
        "package_manifest_sha256": row["package_manifest_sha256"],
        "usage_sha256": row["usage_sha256"],
        "runtime_manifest_sha256": row["runtime_manifest_sha256"],
        "human_usage_sha256": row["human_usage_sha256"],
        "completion_marker_sha256": row["completion_marker_sha256"],
        "completed_at": row["completed_at"],
    }
    proof = attestation.get("artifact_proof") if isinstance(attestation, dict) else None
    if not (
        type(row["revision"]) is int
        and 1 <= int(row["revision"]) <= _MAX_OPERATIONS
        and row["operation_id"] == operation["id"]
        and row["job_id"] == operation["job_id"]
        and row["timeline_revision"] == timeline["revision"]
        and row["timeline_revision_sha256"] == timeline["revision_sha256"]
        and row["timeline_id"] == timeline["timeline_id"]
        and row["timeline_content_sha256"] == timeline["timeline_content_sha256"]
        and row["timeline_source_bindings_sha256"] == timeline["source_bindings_sha256"]
        and row["blueprint_revision"] == blueprint["revision"]
        and row["blueprint_content_sha256"] == blueprint["content_sha256"]
        and row["blueprint_semantic_sha256"] == blueprint["semantic_sha256"]
        and row["blueprint_operation_id"] == blueprint["operation_id"]
        and row["coverage_revision"] == coverage["revision"]
        and row["coverage_content_sha256"] == coverage["content_sha256"]
        and row["coverage_evidence_manifest_sha256"] == coverage["evidence_manifest_sha256"]
        and row["coverage_operation_id"] == coverage["operation_id"]
        and row["output_root_id"] == operation["output_root_id"]
        and row["permission_fingerprint"] == operation["permission_fingerprint"]
        and row["package_basename"] == operation["package_basename"]
        and row["profile"] == operation["profile"] == "export-1080p"
        and _valid_digest(row["video_sha256"])
        and type(row["video_size_bytes"]) is int
        and int(row["video_size_bytes"]) >= 0
        and type(row["video_duration_ms"]) is int
        and int(row["video_duration_ms"]) > 0
        and abs(
            int(row["video_duration_ms"])
            - int(timeline_snapshot["timeline"]["output"]["duration_ms"])
        )
        <= max(50, int(2000 / 30))
        and _valid_digest(row["script_sha256"])
        and type(row["script_size_bytes"]) is int
        and int(row["script_size_bytes"]) >= 0
        and script == expected_script
        and package_manifest == expected_manifest
        and canonical_sha256(package_manifest) == row["package_manifest_sha256"]
        and canonical_sha256(list(occurrences)) == row["usage_sha256"]
        and canonical_sha256(runtime_manifest) == row["runtime_manifest_sha256"]
        and all(
            physical[key] == row[key]
            for key in (
                "package_manifest_sha256",
                "human_usage_sha256",
                "completion_marker_sha256",
            )
        )
        and _valid_timestamp(row["completed_at"])
        and canonical_sha256(material) == row["revision_sha256"]
        and isinstance(proof, dict)
        and proof["video_sha256"] == row["video_sha256"]
        and proof["video_size_bytes"] == row["video_size_bytes"]
        and proof["video_duration_ms"] == row["video_duration_ms"]
        and proof["script_sha256"] == row["script_sha256"]
        and proof["script_size_bytes"] == row["script_size_bytes"]
        and proof["package_manifest"] == package_manifest
        and attestation["usage_sha256"] == row["usage_sha256"]
        and attestation["runtime_manifest_sha256"] == row["runtime_manifest_sha256"]
    ):
        raise _integrity()
    return {
        "project_id": str(row["project_id"]),
        "export_revision": int(row["revision"]),
        "export_revision_sha256": str(row["revision_sha256"]),
        "job_id": str(row["job_id"]),
        "operation_id": str(row["operation_id"]),
        "output_role": "canonical_rendered_video",
        "video_sha256": str(row["video_sha256"]),
    }


def _validate_project(
    connection: sqlite3.Connection,
    project_id: str,
    *,
    database_uuid: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    counts = {
        table: int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        )
        for table in _TABLES
    }
    if (
        any(counts[table] > _MAX_OPERATIONS for table in _TABLES if table != "canonical_usage_occurrences")
        or counts["canonical_usage_occurrences"] > _MAX_OCCURRENCES
        or counts["canonical_export_operations"] == 0
        or counts["canonical_export_jobs"] != counts["canonical_export_operations"]
        or counts["canonical_export_receipts"] != counts["canonical_export_operations"]
    ):
        raise _integrity()
    total_json = connection.execute(
        """SELECT
             COALESCE((SELECT SUM(length(CAST(presentation_json AS BLOB))+
                                  length(CAST(result_json AS BLOB)))
                         FROM canonical_export_operations WHERE project_id=?),0)+
             COALESCE((SELECT SUM(length(CAST(response_json AS BLOB)))
                         FROM canonical_export_receipts WHERE project_id=?),0)+
             COALESCE((SELECT SUM(length(CAST(commit_attestation_json AS BLOB)))
                         FROM canonical_export_jobs WHERE project_id=?),0)+
             COALESCE((SELECT SUM(length(CAST(package_manifest_json AS BLOB))+
                                  length(CAST(runtime_manifest_json AS BLOB)))
                         FROM canonical_export_revisions WHERE project_id=?),0)""",
        (project_id, project_id, project_id, project_id),
    ).fetchone()[0]
    if type(total_json) is not int or not 0 <= total_json <= _MAX_JSON_BYTES:
        raise _integrity()

    operations: dict[str, dict[str, object]] = {}
    operation_timelines: dict[str, dict[str, object]] = {}
    attestations: dict[str, dict[str, object] | None] = {}
    expected_parent: str | None = None
    rows = connection.execute(
        "SELECT * FROM canonical_export_operations WHERE project_id=? ORDER BY sequence",
        (project_id,),
    ).fetchall()
    for expected_sequence, operation_row in enumerate(rows, 1):
        operation = _operation(operation_row)
        if not (
            operation["sequence"] == expected_sequence
            and operation["parent_operation_id"] == expected_parent
            and operation["project_id"] == project_id
        ):
            raise _integrity()
        timeline_snapshot = _timeline_snapshot(
            connection,
            operation=operation,
            database_uuid=database_uuid,
        )
        job_row = connection.execute(
            "SELECT * FROM canonical_export_jobs WHERE project_id=? AND operation_id=?",
            (project_id, operation["id"]),
        ).fetchone()
        receipt_row = connection.execute(
            "SELECT * FROM canonical_export_receipts WHERE project_id=? AND operation_id=?",
            (project_id, operation["id"]),
        ).fetchone()
        revision_row = connection.execute(
            "SELECT 1 FROM canonical_export_revisions WHERE project_id=? AND operation_id=?",
            (project_id, operation["id"]),
        ).fetchone()
        if job_row is None or receipt_row is None:
            raise _integrity()
        initial_job, attestation = _job(
            job_row,
            database_uuid=database_uuid,
            operation=operation,
            revision_present=revision_row is not None,
        )
        expected_response = {
            "object": "canonical_export.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "operation_id": operation["id"],
            "job": initial_job,
        }
        if operation["result"] != expected_response:
            raise _integrity()
        _receipt(
            receipt_row,
            database_uuid=database_uuid,
            operation=operation,
        )
        operations[str(operation["id"])] = operation
        operation_timelines[str(operation["id"])] = timeline_snapshot
        attestations[str(operation["id"])] = attestation
        expected_parent = str(operation["id"])

    facts: list[dict[str, object]] = []
    projected_occurrences: list[dict[str, object]] = []
    total_occurrences = 0
    revisions = connection.execute(
        "SELECT * FROM canonical_export_revisions WHERE project_id=? ORDER BY revision",
        (project_id,),
    ).fetchall()
    for expected_revision, row in enumerate(revisions, 1):
        if row["revision"] != expected_revision:
            raise _integrity()
        operation = operations.get(str(row["operation_id"]))
        timeline_snapshot = operation_timelines.get(str(row["operation_id"]))
        if operation is None or timeline_snapshot is None:
            raise _integrity()
        occurrence_rows = connection.execute(
            """SELECT * FROM canonical_usage_occurrences
                 WHERE project_id=? AND export_revision=? ORDER BY ordinal""",
            (project_id, expected_revision),
        ).fetchall()
        occurrences = [_occurrence(item) for item in occurrence_rows]
        if not (
            [item["ordinal"] for item in occurrences]
            == list(range(len(occurrences)))
            and occurrences == timeline_snapshot["occurrences"]
        ):
            raise _integrity()
        fact = _revision(
            row,
            connection=connection,
            operation=operation,
            occurrences=occurrences,
            attestation=attestations.get(str(operation["id"])),
            timeline_snapshot=timeline_snapshot,
        )
        facts.append(fact)
        projected_occurrences.extend(
            {
                "project_id": project_id,
                "export_revision": expected_revision,
                **occurrence,
            }
            for occurrence in occurrences
        )
        total_occurrences += len(occurrences)
    succeeded = int(
        connection.execute(
            "SELECT COUNT(*) FROM canonical_export_jobs WHERE project_id=? AND status='succeeded'",
            (project_id,),
        ).fetchone()[0]
    )
    if not (
        succeeded == len(revisions)
        and len(revisions) == counts["canonical_export_revisions"]
        and total_occurrences == counts["canonical_usage_occurrences"]
    ):
        raise _integrity()
    return facts, projected_occurrences


def project_validated_derivative_facts(
    connection: sqlite3.Connection,
) -> dict[str, object]:
    """Cold-audit the five-table Export domain and project exact output SHA facts."""

    try:
        database_uuid = _database_uuid(connection)
        _audit_export_root_anchors(connection)
        project_rows = connection.execute(
            """SELECT project_id FROM canonical_export_operations
                   UNION SELECT project_id FROM canonical_export_jobs
                   UNION SELECT project_id FROM canonical_export_revisions
                   UNION SELECT project_id FROM canonical_usage_occurrences
                   UNION SELECT project_id FROM canonical_export_receipts
                ORDER BY project_id"""
        ).fetchall()
        if len(project_rows) > _MAX_PROJECTS:
            raise _integrity()
        project_ids = [str(row["project_id"]) for row in project_rows]
        if any(not _valid_identifier(project_id) for project_id in project_ids):
            raise _integrity()
        facts: list[dict[str, object]] = []
        occurrences: list[dict[str, object]] = []
        for project_id in project_ids:
            project_facts, project_occurrences = _validate_project(
                connection,
                project_id,
                database_uuid=database_uuid,
            )
            facts.extend(project_facts)
            occurrences.extend(project_occurrences)
        facts.sort(
            key=lambda item: (
                item["project_id"],
                item["export_revision"],
                item["export_revision_sha256"],
                item["job_id"],
                item["operation_id"],
                item["video_sha256"],
            )
        )
        occurrences.sort(
            key=lambda item: (
                item["asset_id"],
                item["project_id"],
                item["export_revision"],
                item["ordinal"],
            )
        )
        return {
            "usage_occurrences": occurrences,
            "derivative_facts": facts,
            "derivative_revision": canonical_sha256(facts),
            "video_sha256": frozenset(str(item["video_sha256"]) for item in facts),
        }
    except MemoLensError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, sqlite3.Error) as exc:
        raise _integrity() from exc


__all__ = ["project_validated_derivative_facts"]
