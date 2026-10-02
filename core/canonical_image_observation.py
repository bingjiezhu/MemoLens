"""Shared presentation contract for canonical image-analysis observations.

The observation is a path-free read document.  ``verified_current`` is the
only variant that grants a consumer permission to ground or select an image;
pending, unavailable, unknown, and legacy variants preserve audit facts only.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from .image_analysis_contract import IMAGE_ANALYSIS_STAGES, source_binding_sha256


CANONICAL_IMAGE_OBSERVATION_OBJECT = "memolens.canonical_image_observation"
CANONICAL_IMAGE_OBSERVATION_SCHEMA_VERSION = "1"
CANONICAL_IMAGE_OBSERVATION_AUTHORITY = "canonical_image_analysis"
CANONICAL_IMAGE_OBSERVATION_FIELDS = frozenset(
    {
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
    }
)
CANONICAL_IMAGE_PROJECTION_FIELDS = frozenset(
    {
        "status",
        "generation_id",
        "processing_generation_id",
        "receipt_sha256",
        "row_sha256",
        "reason_code",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _typed_image_stages(result: Mapping[str, object]) -> dict[str, object]:
    stages = result["stages"]
    if not isinstance(stages, dict) or set(stages) != set(IMAGE_ANALYSIS_STAGES):
        raise ValueError("canonical_image_observation_invalid")
    projected: dict[str, object] = {}
    for name in IMAGE_ANALYSIS_STAGES:
        stage = stages[name]
        if not isinstance(stage, dict):
            raise ValueError("canonical_image_observation_invalid")
        output = stage.get("output")
        if name == "embedding" and isinstance(output, dict):
            raw_vectors = output.get("vectors")
            vectors = raw_vectors if isinstance(raw_vectors, list) else []
            safe_output: dict[str, object] | None = {
                "combined_text_sha256": output.get("combined_text_sha256"),
                "vectors": [
                    {
                        "purpose": vector.get("purpose"),
                        "signal": vector.get("signal"),
                        "model_id": vector.get("model_id"),
                        "dimensions": vector.get("dimensions"),
                    }
                    for vector in vectors
                    if isinstance(vector, dict)
                ],
            }
        else:
            safe_output = deepcopy(output) if isinstance(output, dict) else None
        provenance = stage.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError("canonical_image_observation_invalid")
        projected[name] = {
            "status": stage["status"],
            "provenance": deepcopy(provenance),
            "output": safe_output,
            "reason_code": stage["reason_code"],
        }
    return projected


def build_current_canonical_image_observation(
    *,
    asset_id: str,
    analysis_binding: Mapping[str, object],
    result: Mapping[str, object],
    projection: Mapping[str, object],
) -> dict[str, object]:
    """Build the sole image observation variant that may ground selection."""

    normalized_binding = {
        "analysis_run_id": str(analysis_binding["analysis_run_id"]),
        "revision": int(analysis_binding["revision"]),
        "content_sha256": str(analysis_binding["content_sha256"]),
    }
    if (
        result.get("asset_id") != asset_id
        or result.get("analysis_run_id")
        != normalized_binding["analysis_run_id"]
        or result.get("revision") != normalized_binding["revision"]
        or result.get("content_sha256")
        != normalized_binding["content_sha256"]
    ):
        raise ValueError("canonical_image_observation_invalid")
    source_binding = result.get("source_binding")
    if not isinstance(source_binding, dict):
        raise ValueError("canonical_image_observation_invalid")
    required_projection = {
        key: projection[key]
        for key in (
            "generation_id",
            "processing_generation_id",
            "receipt_sha256",
            "row_sha256",
        )
    }
    if any(
        not isinstance(value, str) or not value
        for value in required_projection.values()
    ):
        raise ValueError("canonical_image_observation_invalid")
    return {
        "object": CANONICAL_IMAGE_OBSERVATION_OBJECT,
        "schema_version": CANONICAL_IMAGE_OBSERVATION_SCHEMA_VERSION,
        "status": "current",
        "authority": CANONICAL_IMAGE_OBSERVATION_AUTHORITY,
        "provenance_status": "verified_current",
        "asset_id": asset_id,
        "analysis_binding": normalized_binding,
        "source_binding_sha256": source_binding_sha256(source_binding),
        "projection": {
            "status": "current",
            **required_projection,
            "reason_code": None,
        },
        "stages": _typed_image_stages(result),
        "reason_code": None,
    }


def build_audit_canonical_image_observation(
    *,
    asset_id: str,
    status: str,
    provenance_status: str,
    reason_code: str,
    analysis_binding: Mapping[str, object] | None = None,
    source_binding_digest: str | None = None,
    projection: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a non-grounding v1 observation without inventing missing proof."""

    if status not in {"pending", "unavailable"}:
        raise ValueError("canonical_image_observation_invalid")
    if not reason_code:
        raise ValueError("canonical_image_observation_invalid")
    normalized_binding = None
    if analysis_binding is not None:
        normalized_binding = {
            "analysis_run_id": str(analysis_binding["analysis_run_id"]),
            "revision": int(analysis_binding["revision"]),
            "content_sha256": str(analysis_binding["content_sha256"]),
        }
    projection_map = projection if isinstance(projection, Mapping) else {}
    return {
        "object": CANONICAL_IMAGE_OBSERVATION_OBJECT,
        "schema_version": CANONICAL_IMAGE_OBSERVATION_SCHEMA_VERSION,
        "status": status,
        "authority": CANONICAL_IMAGE_OBSERVATION_AUTHORITY,
        "provenance_status": provenance_status,
        "asset_id": asset_id,
        "analysis_binding": normalized_binding,
        "source_binding_sha256": source_binding_digest,
        "projection": {
            "status": status,
            "generation_id": projection_map.get("generation_id"),
            "processing_generation_id": projection_map.get(
                "processing_generation_id"
            ),
            "receipt_sha256": projection_map.get("receipt_sha256"),
            "row_sha256": None,
            "reason_code": reason_code,
        },
        "stages": None,
        "reason_code": reason_code,
    }


def is_verified_current_canonical_image_observation(
    value: object,
    *,
    asset_id: object | None = None,
) -> bool:
    """Validate the closed presentation shape used at consumer admission."""

    if not isinstance(value, dict) or set(value) != CANONICAL_IMAGE_OBSERVATION_FIELDS:
        return False
    if (
        value.get("object") != CANONICAL_IMAGE_OBSERVATION_OBJECT
        or value.get("schema_version")
        != CANONICAL_IMAGE_OBSERVATION_SCHEMA_VERSION
        or value.get("status") != "current"
        or value.get("authority") != CANONICAL_IMAGE_OBSERVATION_AUTHORITY
        or value.get("provenance_status") != "verified_current"
        or value.get("reason_code") is not None
        or (asset_id is not None and value.get("asset_id") != asset_id)
    ):
        return False
    binding = value.get("analysis_binding")
    source_digest = value.get("source_binding_sha256")
    projection = value.get("projection")
    stages = value.get("stages")
    if (
        not isinstance(binding, dict)
        or set(binding) != {"analysis_run_id", "revision", "content_sha256"}
        or not isinstance(binding.get("analysis_run_id"), str)
        or not binding["analysis_run_id"]
        or type(binding.get("revision")) is not int
        or int(binding["revision"]) < 1
        or not isinstance(binding.get("content_sha256"), str)
        or _SHA256.fullmatch(str(binding["content_sha256"])) is None
        or not isinstance(source_digest, str)
        or _SHA256.fullmatch(source_digest) is None
        or not isinstance(projection, dict)
        or set(projection) != CANONICAL_IMAGE_PROJECTION_FIELDS
        or projection.get("status") != "current"
        or projection.get("reason_code") is not None
        or not isinstance(stages, dict)
        or set(stages) != set(IMAGE_ANALYSIS_STAGES)
    ):
        return False
    for key in ("generation_id", "processing_generation_id"):
        if not isinstance(projection.get(key), str) or not projection[key]:
            return False
    for key in ("receipt_sha256", "row_sha256"):
        digest = projection.get(key)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            return False
    return True


__all__ = [
    "CANONICAL_IMAGE_OBSERVATION_FIELDS",
    "CANONICAL_IMAGE_OBSERVATION_OBJECT",
    "CANONICAL_IMAGE_OBSERVATION_SCHEMA_VERSION",
    "CANONICAL_IMAGE_PROJECTION_FIELDS",
    "build_audit_canonical_image_observation",
    "build_current_canonical_image_observation",
    "is_verified_current_canonical_image_observation",
]
