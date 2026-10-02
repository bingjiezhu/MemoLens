"""Deterministic canonical-image to legacy ``image_index`` projection.

This module is deliberately pure: it validates one sealed Image Analysis
Result and its exact artifact set, then returns a JSON-safe row document.  It
does not open a database, resolve a path, read a clock, or invent fallback
vectors.  Wall-clock envelope values are added only by ``materialize_*``.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any

from core.image_analysis_contract import (
    IMAGE_PROJECTION_CONTRACT,
    ImageAnalysisContractError,
    build_canonical_image_combined_text,
    canonical_sha256,
    require_valid_image_analysis_result,
    source_binding_sha256,
)


PROJECTED_IMAGE_INDEX_ROW_OBJECT = "memolens.projected_image_index_row"
PROJECTED_IMAGE_INDEX_ROW_SCHEMA_VERSION = "1"
IMAGE_PROJECTOR_VERSION = "memolens.image-projector/v1"

IMAGE_INDEX_SQL_COLUMNS = (
    "id",
    "sha256",
    "filename",
    "relative_path",
    "mime_type",
    "file_size",
    "width",
    "height",
    "taken_at",
    "lat",
    "lon",
    "altitude",
    "place_name",
    "country",
    "description",
    "tags_json",
    "combined_text",
    "text_embedding_model",
    "combined_text_embedding",
    "embedding_backend",
    "embedding",
    "aesthetic_score",
    "aesthetic_model",
    "technical_quality_score",
    "aesthetic_updated_at",
    "created_at",
    "updated_at",
)

# Blob columns use explicit transport names so the domain document remains
# JSON-safe and cannot be mistaken for a SQLite parameter mapping.
PROJECTED_IMAGE_INDEX_ROW_FIELDS = (
    *IMAGE_INDEX_SQL_COLUMNS[:18],
    "combined_text_embedding_b64",
    "embedding_backend",
    "embedding_b64",
    *IMAGE_INDEX_SQL_COLUMNS[21:],
)

_DOCUMENT_FIELDS = {
    "object",
    "schema_version",
    "projection_contract",
    "projector_version",
    "asset_id",
    "analysis_binding",
    "source_binding_sha256",
    "row",
    "vector_bindings",
    "row_sha256",
}
_VECTOR_BINDING_FIELDS = {
    "purpose",
    "signal",
    "model_id",
    "backend",
    "dimensions",
    "media_type",
    "artifact_sha256",
}
_ARTIFACT_FIELDS = {
    "stage",
    "name",
    "media_type",
    "signal",
    "model_id",
    "dimensions",
    "artifact_sha256",
    "bytes",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")


@dataclass
class ImageProjectionRenderError(ValueError):
    """Stable blocked projection outcome.

    Projectors should persist ``code`` as the projection receipt reason rather
    than parsing exception prose.
    """

    code: str

    def __init__(self, code: str) -> None:
        object.__setattr__(self, "code", code)
        ValueError.__init__(self, code)


def _blocked(code: str) -> None:
    raise ImageProjectionRenderError(code)


def _is_plain_mapping(value: object) -> bool:
    return type(value) is dict


def _require_closed_mapping(value: object, fields: set[str], code: str) -> dict[str, Any]:
    if not _is_plain_mapping(value) or set(value) != fields:
        _blocked(code)
    return value


def _require_source_projection(value: object) -> dict[str, str]:
    row = _require_closed_mapping(
        value,
        {"filename", "relative_path"},
        "image_projection_source_projection_invalid",
    )
    filename = row.get("filename")
    relative_path = row.get("relative_path")
    if (
        type(filename) is not str
        or not 1 <= len(filename) <= 1_024
        or filename in {".", ".."}
        or "/" in filename
        or "\x00" in filename
        or type(relative_path) is not str
        or not 1 <= len(relative_path) <= 8_192
        or "\x00" in relative_path
    ):
        _blocked("image_projection_source_projection_invalid")
    parsed = PurePosixPath(relative_path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        _blocked("image_projection_source_projection_invalid")
    if parsed.name != filename:
        _blocked("image_projection_source_projection_invalid")
    return {"filename": filename, "relative_path": relative_path}


def _stage_output(result: Mapping[str, object], stage: str) -> dict[str, Any] | None:
    stages = result["stages"]
    assert type(stages) is dict
    outcome = stages[stage]
    assert type(outcome) is dict
    if outcome["status"] not in {"succeeded", "partial"}:
        return None
    output = outcome["output"]
    assert type(output) is dict
    return output


def _normalized_tags(vision: Mapping[str, object] | None) -> list[str]:
    if vision is None:
        return []
    raw = vision.get("tags")
    assert type(raw) is list
    tags = {tag.strip() for tag in raw if type(tag) is str and tag.strip()}
    return sorted(tags)


def _expected_artifacts(result: Mapping[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    stages = result["stages"]
    assert type(stages) is dict
    expected: dict[tuple[str, str], dict[str, object]] = {}
    for stage in ("metadata", "geocode", "vision", "embedding", "quality"):
        outcome = stages[stage]
        assert type(outcome) is dict
        digest = outcome.get("artifact_sha256")
        if type(digest) is str:
            expected[(stage, "stage_output")] = {
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": digest,
            }
    embedding = _stage_output(result, "embedding")
    if embedding is not None:
        vectors = embedding["vectors"]
        assert type(vectors) is list
        for vector in vectors:
            assert type(vector) is dict
            purpose = vector["purpose"]
            assert type(purpose) is str
            expected[("embedding", f"vector:{purpose}")] = {
                "signal": vector["signal"],
                "model_id": vector["model_id"],
                "dimensions": vector["dimensions"],
                "artifact_sha256": vector["artifact_sha256"],
            }
    return expected


def _result_vector(result: Mapping[str, object], purpose: str) -> dict[str, Any] | None:
    embedding = _stage_output(result, "embedding")
    if embedding is None:
        return None
    vectors = embedding["vectors"]
    assert type(vectors) is list
    for vector in vectors:
        assert type(vector) is dict
        if vector["purpose"] == purpose:
            return vector
    return None


def _validated_artifacts(
    result: Mapping[str, object],
    artifacts: Sequence[Mapping[str, object]],
) -> dict[tuple[str, str], dict[str, object]]:
    if isinstance(artifacts, (str, bytes, bytearray)):
        _blocked("image_projection_artifact_set_invalid")
    expected = _expected_artifacts(result)
    observed: dict[tuple[str, str], dict[str, object]] = {}
    for value in artifacts:
        row = _require_closed_mapping(value, _ARTIFACT_FIELDS, "image_projection_artifact_set_invalid")
        stage = row.get("stage")
        name = row.get("name")
        media_type = row.get("media_type")
        payload = row.get("bytes")
        if (
            type(stage) is not str
            or type(name) is not str
            or type(media_type) is not str
            or not 1 <= len(media_type) <= 200
            or type(payload) is not bytes
            or not 1 <= len(payload) <= 64 * 1024 * 1024
        ):
            _blocked("image_projection_artifact_set_invalid")
        identity = (stage, name)
        if identity in observed or identity not in expected:
            _blocked("image_projection_artifact_set_invalid")
        required = expected[identity]
        digest = hashlib.sha256(payload).hexdigest()
        if (
            row.get("artifact_sha256") != digest
            or row.get("artifact_sha256") != required["artifact_sha256"]
            or row.get("signal") != required["signal"]
            or row.get("model_id") != required["model_id"]
            or row.get("dimensions") != required["dimensions"]
        ):
            _blocked("image_projection_artifact_mismatch")
        if name.startswith("vector:"):
            dimensions = row["dimensions"]
            if type(dimensions) is not int or len(payload) != dimensions * 4:
                _blocked("image_projection_vector_shape_mismatch")
        observed[identity] = deepcopy(row)
    if set(observed) != set(expected):
        _blocked("image_projection_artifact_set_incomplete")
    return observed


def _vector_backend(vector: Mapping[str, object]) -> str:
    # ``signal`` is kept in the physical legacy field so text-derived vectors
    # can never be mistaken for visual evidence.  Model identity remains exact.
    return f"{vector['signal']}:{vector['model_id']}"


def _vector_binding(
    purpose: str,
    vector: Mapping[str, object],
    artifact: Mapping[str, object],
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "signal": vector["signal"],
        "model_id": vector["model_id"],
        "backend": _vector_backend(vector),
        "dimensions": vector["dimensions"],
        "media_type": artifact["media_type"],
        "artifact_sha256": vector["artifact_sha256"],
    }


def _quality_model(result: Mapping[str, object]) -> str | None:
    quality = result["stages"]["quality"]
    assert type(quality) is dict
    provenance = quality["provenance"]
    assert type(provenance) is dict
    model_id = provenance.get("model_id")
    return model_id if type(model_id) is str else None


def render_projected_image_index_row(
    *,
    result: Mapping[str, object],
    source_projection: Mapping[str, object],
    artifacts: Sequence[Mapping[str, object]],
    projector_version: str = IMAGE_PROJECTOR_VERSION,
) -> dict[str, object]:
    """Render one exact current Image Analysis Result into a closed row document."""

    try:
        require_valid_image_analysis_result(result)
    except ImageAnalysisContractError:
        _blocked("image_projection_result_invalid")
    if type(projector_version) is not str or not 1 <= len(projector_version) <= 200:
        _blocked("image_projection_projector_version_invalid")
    source = _require_source_projection(source_projection)
    image_vector = _result_vector(result, "image_search")
    if image_vector is None:
        _blocked("image_projection_image_search_vector_missing")
    text_vector = _result_vector(result, "text_search")
    if text_vector is not None and text_vector["signal"] != "text_derived":
        _blocked("image_projection_text_vector_signal_invalid")
    artifact_rows = _validated_artifacts(result, artifacts)

    metadata = _stage_output(result, "metadata")
    if metadata is None:
        _blocked("image_projection_metadata_not_projectable")
    assert metadata is not None
    if (
        metadata.get("mime_type") is None
        or metadata.get("file_size") != result["source_binding"]["observed_size"]
        or metadata.get("width") is None
        or metadata.get("height") is None
    ):
        _blocked("image_projection_metadata_not_projectable")

    geocode = _stage_output(result, "geocode")
    vision = _stage_output(result, "vision")
    quality = _stage_output(result, "quality")
    tags = _normalized_tags(vision)
    description_value = vision.get("description") if vision is not None else None
    description = description_value.strip() if type(description_value) is str else ""
    place_name_value = geocode.get("place_name") if geocode is not None else None
    country_value = geocode.get("country") if geocode is not None else None
    place_name = place_name_value.strip() if type(place_name_value) is str and place_name_value.strip() else None
    country = country_value.strip() if type(country_value) is str and country_value.strip() else None
    combined_text = build_canonical_image_combined_text(
        vision_output=vision,
        geocode_output=geocode,
    )

    embedding_output = _stage_output(result, "embedding")
    assert embedding_output is not None
    combined_text_digest = embedding_output.get("combined_text_sha256")
    text_bound = text_vector is not None or image_vector["signal"] == "text_derived"
    if text_bound and combined_text_digest is None:
        _blocked("image_projection_combined_text_binding_missing")
    if (
        type(combined_text_digest) is str
        and hashlib.sha256(combined_text.encode("utf-8")).hexdigest() != combined_text_digest
    ):
        _blocked("image_projection_combined_text_digest_mismatch")

    image_artifact = artifact_rows[("embedding", "vector:image_search")]
    text_artifact = artifact_rows.get(("embedding", "vector:text_search"))
    image_binding = _vector_binding("image_search", image_vector, image_artifact)
    text_binding = (
        _vector_binding("text_search", text_vector, text_artifact)
        if text_vector is not None and text_artifact is not None
        else None
    )
    image_bytes = image_artifact["bytes"]
    assert type(image_bytes) is bytes
    text_bytes = text_artifact["bytes"] if text_artifact is not None else None
    assert text_bytes is None or type(text_bytes) is bytes

    row: dict[str, object] = {
        "id": result["asset_id"],
        "sha256": result["asset_sha256"],
        "filename": source["filename"],
        "relative_path": source["relative_path"],
        "mime_type": metadata["mime_type"],
        "file_size": metadata["file_size"],
        "width": metadata["width"],
        "height": metadata["height"],
        "taken_at": metadata["taken_at"],
        "lat": metadata["latitude"],
        "lon": metadata["longitude"],
        "altitude": metadata["altitude"],
        "place_name": place_name,
        "country": country,
        "description": description,
        "tags_json": json.dumps(tags, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        "combined_text": combined_text,
        "text_embedding_model": text_vector["model_id"] if text_vector is not None else None,
        "combined_text_embedding_b64": base64.b64encode(text_bytes).decode("ascii") if text_bytes is not None else None,
        "embedding_backend": image_binding["backend"],
        "embedding_b64": base64.b64encode(image_bytes).decode("ascii"),
        "aesthetic_score": quality.get("aesthetic_score") if quality is not None else None,
        "aesthetic_model": _quality_model(result) if quality is not None else None,
        "technical_quality_score": quality.get("technical_quality_score") if quality is not None else None,
        "aesthetic_updated_at": None,
        "created_at": None,
        "updated_at": None,
    }
    row_digest = projected_image_index_row_sha256(row)
    document: dict[str, object] = {
        "object": PROJECTED_IMAGE_INDEX_ROW_OBJECT,
        "schema_version": PROJECTED_IMAGE_INDEX_ROW_SCHEMA_VERSION,
        "projection_contract": IMAGE_PROJECTION_CONTRACT,
        "projector_version": projector_version,
        "asset_id": result["asset_id"],
        "analysis_binding": {
            "analysis_run_id": result["analysis_run_id"],
            "revision": result["revision"],
            "content_sha256": result["content_sha256"],
        },
        "source_binding_sha256": source_binding_sha256(result["source_binding"]),
        "row": row,
        "vector_bindings": {
            "image_search": image_binding,
            "text_search": text_binding,
        },
        "row_sha256": row_digest,
    }
    require_valid_projected_image_index_row(document)
    return document


def _decode_b64(value: object, code: str, *, nullable: bool) -> bytes | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value:
        _blocked(code)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        _blocked(code)
    if not decoded or base64.b64encode(decoded).decode("ascii") != value:
        _blocked(code)
    return decoded


def _normalized_domain_row(row: Mapping[str, object]) -> dict[str, object]:
    closed = _require_closed_mapping(
        row,
        set(PROJECTED_IMAGE_INDEX_ROW_FIELDS),
        "image_projection_row_invalid",
    )
    normalized = deepcopy(closed)
    normalized.pop("created_at")
    normalized.pop("updated_at")
    return normalized


def projected_image_index_row_sha256(row: Mapping[str, object]) -> str:
    """Hash business row content while excluding wall-clock envelope fields."""

    normalized = _normalized_domain_row(row)
    try:
        return canonical_sha256(normalized)
    except (TypeError, ValueError, UnicodeError):
        _blocked("image_projection_row_invalid")


def _require_vector_binding(value: object, purpose: str) -> dict[str, Any]:
    row = _require_closed_mapping(value, _VECTOR_BINDING_FIELDS, "image_projection_document_invalid")
    if (
        row.get("purpose") != purpose
        or row.get("signal") not in {"visual", "text_derived"}
        or type(row.get("model_id")) is not str
        or row.get("backend") != f"{row.get('signal')}:{row.get('model_id')}"
        or type(row.get("dimensions")) is not int
        or row["dimensions"] <= 0
        or type(row.get("media_type")) is not str
        or not _SHA256.fullmatch(str(row.get("artifact_sha256") or ""))
    ):
        _blocked("image_projection_document_invalid")
    return row


def require_valid_projected_image_index_row(value: object) -> None:
    """Reject tampered or open projected-row documents before materialization."""

    document = _require_closed_mapping(value, _DOCUMENT_FIELDS, "image_projection_document_invalid")
    if (
        document.get("object") != PROJECTED_IMAGE_INDEX_ROW_OBJECT
        or document.get("schema_version") != PROJECTED_IMAGE_INDEX_ROW_SCHEMA_VERSION
        or document.get("projection_contract") != IMAGE_PROJECTION_CONTRACT
        or type(document.get("projector_version")) is not str
        or not _ASSET_ID.fullmatch(str(document.get("asset_id") or ""))
        or not _SHA256.fullmatch(str(document.get("source_binding_sha256") or ""))
        or not _SHA256.fullmatch(str(document.get("row_sha256") or ""))
    ):
        _blocked("image_projection_document_invalid")
    analysis = _require_closed_mapping(
        document.get("analysis_binding"),
        {"analysis_run_id", "revision", "content_sha256"},
        "image_projection_document_invalid",
    )
    if (
        not _ANALYSIS_RUN_ID.fullmatch(str(analysis.get("analysis_run_id") or ""))
        or type(analysis.get("revision")) is not int
        or analysis["revision"] <= 0
        or not _SHA256.fullmatch(str(analysis.get("content_sha256") or ""))
    ):
        _blocked("image_projection_document_invalid")
    row = _require_closed_mapping(
        document.get("row"),
        set(PROJECTED_IMAGE_INDEX_ROW_FIELDS),
        "image_projection_document_invalid",
    )
    if (
        row.get("id") != document["asset_id"]
        or row.get("created_at") is not None
        or row.get("updated_at") is not None
        or type(row.get("embedding_backend")) is not str
    ):
        _blocked("image_projection_document_invalid")
    try:
        tags = json.loads(row["tags_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        _blocked("image_projection_document_invalid")
    if type(tags) is not list or any(type(tag) is not str for tag in tags) or tags != sorted(set(tags)):
        _blocked("image_projection_document_invalid")
    vectors = _require_closed_mapping(
        document.get("vector_bindings"),
        {"image_search", "text_search"},
        "image_projection_document_invalid",
    )
    image_binding = _require_vector_binding(vectors.get("image_search"), "image_search")
    if row["embedding_backend"] != image_binding["backend"]:
        _blocked("image_projection_document_invalid")
    image_bytes = _decode_b64(row.get("embedding_b64"), "image_projection_document_invalid", nullable=False)
    assert image_bytes is not None
    if (
        len(image_bytes) != image_binding["dimensions"] * 4
        or hashlib.sha256(image_bytes).hexdigest() != image_binding["artifact_sha256"]
    ):
        _blocked("image_projection_document_invalid")
    text_value = vectors.get("text_search")
    text_bytes = _decode_b64(
        row.get("combined_text_embedding_b64"),
        "image_projection_document_invalid",
        nullable=True,
    )
    if text_value is None:
        if text_bytes is not None or row.get("text_embedding_model") is not None:
            _blocked("image_projection_document_invalid")
    else:
        text_binding = _require_vector_binding(text_value, "text_search")
        if (
            text_binding["signal"] != "text_derived"
            or text_bytes is None
            or row.get("text_embedding_model") != text_binding["model_id"]
            or len(text_bytes) != text_binding["dimensions"] * 4
            or hashlib.sha256(text_bytes).hexdigest() != text_binding["artifact_sha256"]
        ):
            _blocked("image_projection_document_invalid")
    if projected_image_index_row_sha256(row) != document["row_sha256"]:
        _blocked("image_projection_row_digest_mismatch")


def materialize_projected_image_index_row(
    document: Mapping[str, object],
    *,
    created_at: str,
    updated_at: str,
) -> dict[str, object]:
    """Convert a verified JSON document into exact SQLite column values."""

    require_valid_projected_image_index_row(document)
    if type(created_at) is not str or not created_at or type(updated_at) is not str or not updated_at:
        _blocked("image_projection_materialization_envelope_invalid")
    projected = document["row"]
    assert type(projected) is dict
    row: dict[str, object] = {}
    for column in IMAGE_INDEX_SQL_COLUMNS:
        if column == "combined_text_embedding":
            row[column] = _decode_b64(
                projected["combined_text_embedding_b64"],
                "image_projection_document_invalid",
                nullable=True,
            )
        elif column == "embedding":
            row[column] = _decode_b64(
                projected["embedding_b64"],
                "image_projection_document_invalid",
                nullable=False,
            )
        elif column == "created_at":
            row[column] = created_at
        elif column == "updated_at":
            row[column] = updated_at
        else:
            row[column] = projected[column]
    return row


def materialize_projected_image_index_values(
    document: Mapping[str, object],
    *,
    created_at: str,
    updated_at: str,
) -> tuple[object, ...]:
    row = materialize_projected_image_index_row(
        document,
        created_at=created_at,
        updated_at=updated_at,
    )
    return tuple(row[column] for column in IMAGE_INDEX_SQL_COLUMNS)


__all__ = [
    "IMAGE_INDEX_SQL_COLUMNS",
    "IMAGE_PROJECTOR_VERSION",
    "PROJECTED_IMAGE_INDEX_ROW_FIELDS",
    "PROJECTED_IMAGE_INDEX_ROW_OBJECT",
    "PROJECTED_IMAGE_INDEX_ROW_SCHEMA_VERSION",
    "ImageProjectionRenderError",
    "materialize_projected_image_index_row",
    "materialize_projected_image_index_values",
    "projected_image_index_row_sha256",
    "render_projected_image_index_row",
    "require_valid_projected_image_index_row",
]
