"""Canonical Timeline editor authority and browser-safe projections.

This module is deliberately independent from the legacy Timeline 1.0 draft
editor.  It reads three Core-owned canonical views, cross-checks their exact
database/project/upstream/head bindings, and exposes only an allowlisted,
path-free browser projection.  Pairing credentials and MLCAP1 proof material
remain in this Python process.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping

from memolens_agent import AgentPairingWorkflow
from memolens_agent_client import (
    TIMELINE_PREVIEW_ACTION,
    AgentApiClient,
    AgentApiError,
    AgentPreviewMediaResponse,
    canonical_sha256,
)
from memolens_agent_credentials import load_agent_credential
from memolens_contracts import MemoLensError


CANONICAL_EDITOR_OBJECT = "memolens.canonical_editor_handoff"
CANONICAL_EDITOR_SCHEMA_VERSION = "1"
CANONICAL_EDITOR_ORIGIN = "agent_plugin_editor"
TIMELINE_EDIT_ACTION = "timeline.apply_edit"
TIMELINE_STRUCTURAL_EDIT_ACTION = "timeline.apply_structural_edit"
TIMELINE_RESTORE_ACTION = "timeline.restore_revision"
MAX_TIMELINE_DURATION_MS = 1_800_000
MAX_SOURCE_TIME_MS = 9_007_199_254_740_991
MIN_VIDEO_TRIM_MS = 100
MAX_TIMELINE_CLIPS = 256

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_IMAGE_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_IMAGE_ASSET_ID = re.compile(r"^(?:asset|img)_[0-9a-f]{24}$")
_IMAGE_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/asset/(?P<asset_id>(?:asset|img)_[0-9a-f]{24})$"
)
_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/(?:asset/(?:asset|img)_[0-9a-f]{24}|"
    r"span/seg_[0-9a-f]{24}_[1-9][0-9]{0,6}_(?:0|[1-9][0-9]{0,6}))$"
)
_EDIT_FIELDS = {
    "move_clip": {"op", "clip_id", "to_index"},
    "trim_clip": {"op", "clip_id", "source_in_ms", "source_out_ms"},
    "set_clip_duration": {"op", "clip_id", "duration_ms"},
    "replace_clip": {"op", "clip_id", "assignment_id"},
}
_STRUCTURAL_EDIT_FIELDS = {
    "split_clip": {"op", "clip_id", "source_split_ms"},
    "delete_clip": {"op", "clip_id"},
}
_IMAGE_ANALYSIS_FIELDS = {
    "analysis_run_id",
    "analysis_revision",
    "analysis_content_sha256",
    "source_binding_sha256",
}
_LEGACY_IMAGE_PROOF_FIELDS = {"kind", "asset_id", "asset_sha256"}
_CURRENT_IMAGE_PROOF_FIELDS = _LEGACY_IMAGE_PROOF_FIELDS | _IMAGE_ANALYSIS_FIELDS
_IMAGE_CLIP_COMMON_FIELDS = {
    "clip_id",
    "ordinal",
    "beat_id",
    "assignment_id",
    "evidence_ref",
    "asset_id",
    "asset_sha256",
    "start_ms",
    "end_ms",
    "media_kind",
    "fit",
    "audio_enabled",
}
_SOURCE_BINDING_COMMON_FIELDS = {
    "clip_id",
    "evidence_ref",
    "asset_id",
    "asset_sha256",
    "asset_source_id",
    "coverage_proof_sha256",
    "media_kind",
}


class CanonicalEditorError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _fail(code: str, message: str, *, status: int = 409) -> None:
    raise CanonicalEditorError(code, message, status=status)


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("canonical_editor_projection_invalid", f"{field} is invalid.")
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail("canonical_editor_projection_invalid", f"{field} is invalid.")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("canonical_editor_projection_invalid", f"{field} is invalid.")
    return value


def _image_analysis_fields(
    value: Mapping[str, Any],
    field: str,
) -> dict[str, object]:
    run_id = value.get("analysis_run_id")
    if type(run_id) is not str or _IMAGE_ANALYSIS_RUN_ID.fullmatch(run_id) is None:
        _fail("canonical_editor_projection_invalid", f"{field}.analysis_run_id is invalid.")
    return {
        "analysis_run_id": run_id,
        "analysis_revision": _integer(
            value.get("analysis_revision"),
            f"{field}.analysis_revision",
            minimum=1,
            maximum=1_800_000,
        ),
        "analysis_content_sha256": _digest(
            value.get("analysis_content_sha256"),
            f"{field}.analysis_content_sha256",
        ),
        "source_binding_sha256": _digest(
            value.get("source_binding_sha256"),
            f"{field}.source_binding_sha256",
        ),
    }


def _image_evidence_currency(
    value: Mapping[str, Any],
    *,
    common_fields: set[str],
    field: str,
) -> str:
    fields = set(value)
    if fields == common_fields:
        return "legacy_non_current"
    if fields == common_fields | _IMAGE_ANALYSIS_FIELDS:
        _image_analysis_fields(value, field)
        return "current"
    _fail(
        "canonical_editor_projection_invalid",
        f"{field} has partial or unsupported image analysis evidence.",
    )


def _image_proof(
    value: object,
    field: str,
) -> tuple[dict[str, object], str]:
    row = _mapping(value, field)
    currency = _image_evidence_currency(
        row,
        common_fields=_LEGACY_IMAGE_PROOF_FIELDS,
        field=field,
    )
    if row.get("kind") != "asset":
        _fail("canonical_editor_projection_invalid", f"{field}.kind is invalid.")
    asset_id = row.get("asset_id")
    asset_sha256 = row.get("asset_sha256")
    if (
        type(asset_id) is not str
        or _IMAGE_ASSET_ID.fullmatch(asset_id) is None
        or type(asset_sha256) is not str
        or _SHA256.fullmatch(asset_sha256) is None
        or asset_id != f"{asset_id.split('_', 1)[0]}_{asset_sha256[:24]}"
    ):
        _fail(
            "canonical_editor_projection_invalid",
            f"{field} image asset identity is invalid.",
        )
    proof: dict[str, object] = {
        "kind": "asset",
        "asset_id": asset_id,
        "asset_sha256": asset_sha256,
    }
    if currency == "current":
        proof.update(_image_analysis_fields(row, field))
    return proof, currency


def _expected_image_clip_id(timeline_id: str, clip: Mapping[str, Any]) -> str:
    parts: list[object] = [
        timeline_id,
        clip["beat_id"],
        clip["assignment_id"],
        clip["evidence_ref"],
        clip["asset_id"],
        clip["asset_sha256"],
        "image",
    ]
    if set(clip) & _IMAGE_ANALYSIS_FIELDS:
        parts.extend(
            clip[field]
            for field in (
                "analysis_run_id",
                "analysis_revision",
                "analysis_content_sha256",
                "source_binding_sha256",
            )
        )
    material = "\0".join(str(part) for part in parts)
    return f"clip_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _integer(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_SOURCE_TIME_MS,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("canonical_editor_projection_invalid", f"{field} is invalid.")
    return value


def _blueprint_binding(value: object, field: str) -> dict[str, object]:
    row = _mapping(value, field)
    return {
        "revision": _integer(row.get("revision"), f"{field}.revision", minimum=1),
        "content_sha256": _digest(
            row.get("content_sha256"), f"{field}.content_sha256"
        ),
        "semantic_sha256": _digest(
            row.get("semantic_sha256"), f"{field}.semantic_sha256"
        ),
        "operation_id": _identifier(
            row.get("operation_id"), f"{field}.operation_id"
        ),
    }


def _coverage_binding(value: object, field: str) -> dict[str, object]:
    row = _mapping(value, field)
    return {
        "revision": _integer(row.get("revision"), f"{field}.revision", minimum=1),
        "content_sha256": _digest(
            row.get("content_sha256"), f"{field}.content_sha256"
        ),
        "evidence_manifest_sha256": _digest(
            row.get("evidence_manifest_sha256"),
            f"{field}.evidence_manifest_sha256",
        ),
        "operation_id": _identifier(
            row.get("operation_id"), f"{field}.operation_id"
        ),
    }


def _timeline_head(value: object, field: str) -> dict[str, object]:
    row = _mapping(value, field)
    return {
        "revision": _integer(row.get("revision"), f"{field}.revision", minimum=1),
        "revision_sha256": _digest(
            row.get("revision_sha256"), f"{field}.revision_sha256"
        ),
        "timeline_id": _identifier(
            row.get("timeline_id"), f"{field}.timeline_id"
        ),
        "timeline_content_sha256": _digest(
            row.get("timeline_content_sha256"),
            f"{field}.timeline_content_sha256",
        ),
        "blueprint_binding": _blueprint_binding(
            row.get("blueprint_binding"), f"{field}.blueprint_binding"
        ),
        "coverage_binding": _coverage_binding(
            row.get("coverage_binding"), f"{field}.coverage_binding"
        ),
        "operation_id": _identifier(
            row.get("operation_id"), f"{field}.operation_id"
        ),
    }


def _coverage_head(value: object, field: str) -> dict[str, object]:
    row = _mapping(value, field)
    return {
        "revision": _integer(row.get("revision"), f"{field}.revision", minimum=1),
        "content_sha256": _digest(
            row.get("content_sha256"), f"{field}.content_sha256"
        ),
    }


def _clip(value: object, index: int) -> dict[str, object]:
    field = f"timeline.tracks[0].clips[{index}]"
    row = _mapping(value, field)
    media_kind = row.get("media_kind")
    if media_kind not in {"image", "video"}:
        _fail("canonical_editor_projection_invalid", f"{field}.media_kind is invalid.")
    result: dict[str, object] = {
        "clip_id": _identifier(row.get("clip_id"), f"{field}.clip_id"),
        "ordinal": _integer(row.get("ordinal"), f"{field}.ordinal"),
        "beat_id": _identifier(row.get("beat_id"), f"{field}.beat_id"),
        "assignment_id": _identifier(
            row.get("assignment_id"), f"{field}.assignment_id"
        ),
        "evidence_ref": str(row.get("evidence_ref") or ""),
        "asset_id": _identifier(row.get("asset_id"), f"{field}.asset_id"),
        "asset_sha256": _digest(row.get("asset_sha256"), f"{field}.asset_sha256"),
        "start_ms": _integer(row.get("start_ms"), f"{field}.start_ms"),
        "end_ms": _integer(row.get("end_ms"), f"{field}.end_ms", minimum=1),
        "media_kind": media_kind,
        "fit": "cover",
        "audio_enabled": False,
    }
    if _EVIDENCE_REF.fullmatch(str(result["evidence_ref"])) is None:
        _fail(
            "canonical_editor_projection_invalid",
            f"{field}.evidence_ref is invalid.",
        )
    if row.get("fit") != "cover" or row.get("audio_enabled") is not False:
        _fail("canonical_editor_projection_invalid", f"{field} capability is invalid.")
    if result["end_ms"] <= result["start_ms"]:
        _fail("canonical_editor_projection_invalid", f"{field} duration is invalid.")
    if media_kind == "image":
        evidence_match = _IMAGE_EVIDENCE_REF.fullmatch(str(result["evidence_ref"]))
        if (
            evidence_match is None
            or evidence_match.group("asset_id") != result["asset_id"]
            or result["asset_id"]
            != f"{str(result['asset_id']).split('_', 1)[0]}_{str(result['asset_sha256'])[:24]}"
        ):
            _fail(
                "canonical_editor_projection_invalid",
                f"{field} image asset identity is invalid.",
            )
        currency = _image_evidence_currency(
            row,
            common_fields=set(result),
            field=field,
        )
        if currency == "current":
            result.update(_image_analysis_fields(row, field))
    else:
        result.update(
            {
                "span_id": _identifier(row.get("span_id"), f"{field}.span_id"),
                "analysis_run_id": _identifier(
                    row.get("analysis_run_id"), f"{field}.analysis_run_id"
                ),
                "analysis_revision": _integer(
                    row.get("analysis_revision"),
                    f"{field}.analysis_revision",
                    minimum=1,
                ),
                "input_asset_sha256": _digest(
                    row.get("input_asset_sha256"),
                    f"{field}.input_asset_sha256",
                ),
                "source_in_ms": _integer(
                    row.get("source_in_ms"), f"{field}.source_in_ms"
                ),
                "source_out_ms": _integer(
                    row.get("source_out_ms"),
                    f"{field}.source_out_ms",
                    minimum=1,
                ),
            }
        )
        if result["source_out_ms"] <= result["source_in_ms"]:
            _fail(
                "canonical_editor_projection_invalid",
                f"{field} source range is invalid.",
            )
    return result


def _timeline_document(value: object, project_id: str) -> dict[str, object]:
    row = _mapping(value, "timeline")
    schema_version = row.get("schema_version")
    if (
        row.get("object") != "memolens.canonical_timeline"
        or schema_version not in {"1", "2"}
    ):
        _fail("canonical_editor_projection_invalid", "Timeline contract is invalid.")
    tracks = row.get("tracks")
    if type(tracks) is not list or len(tracks) != 1:
        _fail("canonical_editor_projection_invalid", "Timeline track is invalid.")
    track = _mapping(tracks[0], "timeline.tracks[0]")
    raw_clips = track.get("clips")
    if (
        track.get("kind") != "primary_visual"
        or type(raw_clips) is not list
        or not raw_clips
        or len(raw_clips) > 256
    ):
        _fail("canonical_editor_projection_invalid", "Timeline clips are invalid.")
    clips = [_clip(item, index) for index, item in enumerate(raw_clips)]
    timeline_id = _identifier(row.get("timeline_id"), "timeline.timeline_id")
    cursor = 0
    for index, clip in enumerate(clips):
        if clip["ordinal"] != index or clip["start_ms"] != cursor:
            _fail(
                "canonical_editor_projection_invalid",
                "Timeline clip order is not canonical.",
            )
        if (
            clip["media_kind"] == "image"
            and clip["clip_id"] != _expected_image_clip_id(timeline_id, clip)
        ):
            _fail(
                "canonical_editor_projection_invalid",
                "Image clip identity does not bind its exact analysis revision.",
            )
        cursor = int(clip["end_ms"])
    output = _mapping(row.get("output"), "timeline.output")
    duration = _integer(
        output.get("duration_ms"),
        "timeline.output.duration_ms",
        minimum=1,
        maximum=MAX_TIMELINE_DURATION_MS,
    )
    if duration != cursor or output.get("aspect_ratio") not in {
        "16:9",
        "9:16",
        "1:1",
        "4:5",
    }:
        _fail("canonical_editor_projection_invalid", "Timeline output is invalid.")
    result = {
        "object": "memolens.canonical_timeline",
        "schema_version": schema_version,
        "timeline_id": timeline_id,
        "project_id": _identifier(row.get("project_id"), "timeline.project_id"),
        "revision": _integer(row.get("revision"), "timeline.revision", minimum=1),
        "parent": deepcopy(row.get("parent")),
        "blueprint_binding": _blueprint_binding(
            row.get("blueprint_binding"), "timeline.blueprint_binding"
        ),
        "coverage_binding": _coverage_binding(
            row.get("coverage_binding"), "timeline.coverage_binding"
        ),
        "compiler": deepcopy(row.get("compiler")),
        "output": {"duration_ms": duration, "aspect_ratio": output["aspect_ratio"]},
        "tracks": [
            {
                "track_id": _identifier(
                    track.get("track_id"), "timeline.tracks[0].track_id"
                ),
                "kind": "primary_visual",
                "clips": clips,
            }
        ],
    }
    if result["project_id"] != project_id or result != row:
        _fail(
            "canonical_editor_projection_invalid",
            "Timeline must be the exact closed canonical document.",
        )
    return result


def _manifest_index(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = plan.get("evidence_manifest")
    if type(raw) is not list or not raw or len(raw) > 1_024:
        _fail("canonical_editor_projection_invalid", "Coverage evidence is invalid.")
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(raw):
        row = _mapping(item, f"coverage.evidence_manifest[{index}]")
        ref = row.get("evidence_ref")
        if type(ref) is not str or _EVIDENCE_REF.fullmatch(ref) is None or ref in result:
            _fail("canonical_editor_projection_invalid", "Coverage evidence identity is invalid.")
        result[ref] = row
    return result


def _coverage_image_evidence_currency(plan: Mapping[str, Any]) -> str:
    currency = "current"
    for index, evidence in enumerate(_manifest_index(plan).values()):
        proof = evidence.get("proof")
        if not isinstance(proof, Mapping) or proof.get("kind") != "asset":
            continue
        normalized, proof_currency = _image_proof(
            proof,
            f"coverage.evidence_manifest[{index}].proof",
        )
        evidence_match = _IMAGE_EVIDENCE_REF.fullmatch(
            str(evidence.get("evidence_ref") or "")
        )
        proof_sha = evidence.get("proof_sha256")
        if (
            evidence_match is None
            or evidence_match.group("asset_id") != normalized["asset_id"]
            or type(proof_sha) is not str
            or _SHA256.fullmatch(proof_sha) is None
            or canonical_sha256(normalized) != proof_sha
        ):
            _fail(
                "canonical_editor_projection_invalid",
                "Coverage image proof digest is invalid.",
            )
        if proof_currency == "legacy_non_current":
            currency = proof_currency
    return currency


def _safe_alternatives(
    plan: Mapping[str, Any],
    timeline: Mapping[str, Any],
) -> dict[str, list[dict[str, object]]]:
    manifest = _manifest_index(plan)
    raw_beats = plan.get("beats")
    if type(raw_beats) is not list or not raw_beats or len(raw_beats) > 256:
        _fail("canonical_editor_projection_invalid", "Coverage Beats are invalid.")
    beats: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(raw_beats):
        beat = _mapping(item, f"coverage.beats[{index}]")
        beat_id = _identifier(beat.get("beat_id"), f"coverage.beats[{index}].beat_id")
        if beat_id in beats:
            _fail("canonical_editor_projection_invalid", "Coverage Beat identity is invalid.")
        beats[beat_id] = beat
    clips = timeline["tracks"][0]["clips"]
    assert isinstance(clips, list)
    safe: dict[str, list[dict[str, object]]] = {}
    for clip in clips:
        assert isinstance(clip, Mapping)
        clip_id = str(clip["clip_id"])
        beat = beats.get(str(clip["beat_id"]))
        if beat is None or type(beat.get("alternatives")) is not list:
            _fail("canonical_editor_projection_invalid", "Timeline Beat is not covered.")
        occupied = {
            str(other["evidence_ref"])
            for other in clips
            if isinstance(other, Mapping) and other.get("clip_id") != clip_id
        }
        current_duration = int(clip["end_ms"]) - int(clip["start_ms"])
        candidates: list[dict[str, object]] = []
        for raw in beat["alternatives"]:
            alternative = _mapping(raw, "coverage.alternative")
            assignment_id = _identifier(
                alternative.get("assignment_id"), "coverage.alternative.assignment_id"
            )
            evidence_ref = alternative.get("evidence_ref")
            if (
                assignment_id == clip["assignment_id"]
                or type(evidence_ref) is not str
                or evidence_ref in occupied
            ):
                continue
            evidence = manifest.get(evidence_ref)
            proof = evidence.get("proof") if evidence is not None else None
            proof_sha = evidence.get("proof_sha256") if evidence is not None else None
            if (
                evidence is None
                or evidence.get("status") != "verified"
                or not isinstance(proof, Mapping)
                or type(proof_sha) is not str
                or _SHA256.fullmatch(proof_sha) is None
                or canonical_sha256(dict(proof)) != proof_sha
            ):
                continue
            kind = proof.get("kind")
            if kind == "span":
                start = proof.get("start_ms")
                end = proof.get("end_ms")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or end - start < current_duration
                ):
                    continue
                media_kind = "video"
                available_duration: int | None = end - start
            elif kind == "asset":
                normalized_proof, proof_currency = _image_proof(
                    proof,
                    "coverage.alternative.proof",
                )
                if proof_currency != "current":
                    # Historical identity-only evidence remains inspectable in
                    # Coverage, but it never becomes a replacement route.
                    continue
                proof = normalized_proof
                media_kind = "image"
                available_duration = None
            else:
                continue
            candidate = {
                    "assignment_id": assignment_id,
                    "evidence_ref": evidence_ref,
                    "media_kind": media_kind,
                    "asset_id": _identifier(
                        proof.get("asset_id"), "coverage.alternative.asset_id"
                    ),
                    "asset_sha256": _digest(
                        proof.get("asset_sha256"),
                        "coverage.alternative.asset_sha256",
                    ),
                    "current_slot_duration_ms": current_duration,
                    "available_duration_ms": available_duration,
                }
            if media_kind == "image":
                candidate.update(
                    {field: proof[field] for field in _IMAGE_ANALYSIS_FIELDS}
                )
            candidates.append(candidate)
        safe[clip_id] = candidates
    return safe


def _validate_source_bindings(
    value: object,
    *,
    timeline: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> str:
    bindings = value
    clips = timeline["tracks"][0]["clips"]
    if type(bindings) is not list or len(bindings) != len(clips):
        _fail("canonical_editor_projection_invalid", "Fixed source manifest is incomplete.")
    manifest = _manifest_index(plan)
    by_clip: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(bindings):
        row = _mapping(item, f"source_bindings[{index}]")
        clip_id = _identifier(row.get("clip_id"), f"source_bindings[{index}].clip_id")
        if clip_id in by_clip:
            _fail("canonical_editor_projection_invalid", "Fixed source identity is duplicated.")
        source_id = row.get("asset_source_id")
        if type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
            _fail(
                "canonical_editor_projection_invalid",
                f"source_bindings[{index}].asset_source_id is invalid.",
            )
        by_clip[clip_id] = row
    evidence_currency = "current"
    for clip in clips:
        assert isinstance(clip, Mapping)
        binding = by_clip.get(str(clip["clip_id"]))
        evidence = manifest.get(str(clip["evidence_ref"]))
        if binding is None or evidence is None:
            _fail("canonical_editor_projection_invalid", "Fixed source binding is missing.")
        video_fields = {
            "span_id",
            "analysis_run_id",
            "analysis_revision",
            "input_asset_sha256",
            "source_in_ms",
            "source_out_ms",
        }
        if clip["media_kind"] == "video":
            if set(binding) != _SOURCE_BINDING_COMMON_FIELDS | video_fields:
                _fail(
                    "canonical_editor_projection_invalid",
                    "Fixed source binding is not a closed canonical value.",
                )
        else:
            clip_currency = _image_evidence_currency(
                clip,
                common_fields=_IMAGE_CLIP_COMMON_FIELDS,
                field="timeline.image_clip",
            )
            source_currency = _image_evidence_currency(
                binding,
                common_fields=_SOURCE_BINDING_COMMON_FIELDS,
                field="source_binding.image",
            )
            if clip_currency != source_currency:
                _fail(
                    "canonical_editor_projection_invalid",
                    "Image clip and fixed source carry different analysis evidence.",
                )
            if clip_currency == "legacy_non_current":
                evidence_currency = clip_currency
        expected = {
            "evidence_ref": clip["evidence_ref"],
            "asset_id": clip["asset_id"],
            "asset_sha256": clip["asset_sha256"],
            "media_kind": clip["media_kind"],
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            _fail("canonical_editor_projection_invalid", "Fixed source binding is stale.")
        if binding.get("coverage_proof_sha256") != evidence.get("proof_sha256"):
            _fail("canonical_editor_projection_invalid", "Coverage proof binding is stale.")
        proof = evidence.get("proof")
        proof_sha = evidence.get("proof_sha256")
        if (
            evidence.get("status") != "verified"
            or not isinstance(proof, Mapping)
            or type(proof_sha) is not str
            or _SHA256.fullmatch(proof_sha) is None
            or canonical_sha256(dict(proof)) != proof_sha
        ):
            _fail(
                "canonical_editor_projection_invalid",
                "Timeline source does not retain one verified Coverage proof.",
            )
        if (
            proof.get("asset_id") != clip.get("asset_id")
            or proof.get("asset_sha256") != clip.get("asset_sha256")
        ):
            _fail(
                "canonical_editor_projection_invalid",
                "Timeline source asset does not match its Coverage proof.",
            )
        if clip["media_kind"] == "video":
            if any(
                binding.get(key) != clip.get(key)
                for key in (
                    "span_id",
                    "analysis_run_id",
                    "analysis_revision",
                    "input_asset_sha256",
                    "source_in_ms",
                    "source_out_ms",
                )
            ):
                _fail("canonical_editor_projection_invalid", "Video source binding is stale.")
        else:
            normalized_proof, proof_currency = _image_proof(
                proof,
                "coverage.timeline_image.proof",
            )
            if proof_currency != clip_currency:
                _fail(
                    "canonical_editor_projection_invalid",
                    "Timeline image and Coverage carry different analysis evidence.",
                )
            if proof_currency == "legacy_non_current":
                evidence_currency = proof_currency
            elif any(
                clip.get(field) != normalized_proof.get(field)
                or binding.get(field) != normalized_proof.get(field)
                for field in _IMAGE_ANALYSIS_FIELDS
            ):
                _fail(
                    "canonical_editor_projection_invalid",
                    "Image clip and source do not match the exact Coverage analysis proof.",
                )
    return evidence_currency


def _validate_historical_source_binding_shape(
    value: object,
    *,
    timeline: Mapping[str, Any],
) -> str:
    """Validate a historical manifest without treating it as current authority."""

    bindings = value
    clips = timeline["tracks"][0]["clips"]
    if type(bindings) is not list or len(bindings) != len(clips):
        _fail("canonical_editor_history_mismatch", "Historical source manifest is incomplete.")
    by_clip: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(bindings):
        row = _mapping(item, f"historical_source_bindings[{index}]")
        clip_id = _identifier(
            row.get("clip_id"),
            f"historical_source_bindings[{index}].clip_id",
        )
        if clip_id in by_clip:
            _fail(
                "canonical_editor_history_mismatch",
                "Historical source identity is duplicated.",
            )
        source_id = row.get("asset_source_id")
        if type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
            _fail(
                "canonical_editor_history_mismatch",
                "Historical source identity is invalid.",
            )
        by_clip[clip_id] = row
    evidence_currency = "current"
    for clip in clips:
        assert isinstance(clip, Mapping)
        binding = by_clip.get(str(clip["clip_id"]))
        if binding is None:
            _fail(
                "canonical_editor_history_mismatch",
                "Historical source binding is missing.",
            )
        video_fields = {
            "span_id",
            "analysis_run_id",
            "analysis_revision",
            "input_asset_sha256",
            "source_in_ms",
            "source_out_ms",
        }
        if clip["media_kind"] == "video":
            if set(binding) != _SOURCE_BINDING_COMMON_FIELDS | video_fields:
                _fail(
                    "canonical_editor_history_mismatch",
                    "Historical source binding is not a closed canonical value.",
                )
        else:
            clip_currency = _image_evidence_currency(
                clip,
                common_fields=_IMAGE_CLIP_COMMON_FIELDS,
                field="historical_timeline.image_clip",
            )
            source_currency = _image_evidence_currency(
                binding,
                common_fields=_SOURCE_BINDING_COMMON_FIELDS,
                field="historical_source_binding.image",
            )
            if clip_currency != source_currency:
                _fail(
                    "canonical_editor_history_mismatch",
                    "Historical image clip and source carry different analysis evidence.",
                )
            if clip_currency == "legacy_non_current":
                evidence_currency = clip_currency
        _digest(
            binding.get("coverage_proof_sha256"),
            "historical_source_binding.coverage_proof_sha256",
        )
        if any(
            binding.get(key) != clip.get(key)
            for key in ("evidence_ref", "asset_id", "asset_sha256", "media_kind")
        ):
            _fail(
                "canonical_editor_history_mismatch",
                "Historical source binding does not match its Timeline clip.",
            )
        if clip["media_kind"] == "video" and any(
            binding.get(key) != clip.get(key)
            for key in (
                "span_id",
                "analysis_run_id",
                "analysis_revision",
                "input_asset_sha256",
                "source_in_ms",
                "source_out_ms",
            )
        ):
            _fail(
                "canonical_editor_history_mismatch",
                "Historical video binding does not match its Timeline clip.",
            )
        if clip["media_kind"] == "image" and clip_currency == "current" and any(
            binding.get(field) != clip.get(field)
            for field in _IMAGE_ANALYSIS_FIELDS
        ):
            _fail(
                "canonical_editor_history_mismatch",
                "Historical image binding does not match its exact Timeline analysis.",
            )
    return evidence_currency


@dataclass(frozen=True)
class CanonicalEditorSnapshot:
    public: dict[str, Any]
    expected_blueprint: dict[str, object]
    expected_coverage: dict[str, object]
    expected_timeline_head: dict[str, object]
    coverage_plan: dict[str, Any]
    source_bindings: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CanonicalEditorHistoricalSnapshot:
    public: dict[str, Any]
    restore_from: dict[str, object]
    restore_eligible: bool


def _require_current_editor_evidence(snapshot: CanonicalEditorSnapshot) -> None:
    if snapshot.public.get("eligible_for_current_use") is not True:
        _fail(
            "canonical_editor_evidence_non_current",
            "Identity-only image evidence is audit-only and cannot be edited, saved, restored, or preview-granted.",
            status=409,
        )


def canonical_editor_snapshot(
    *,
    project_id: str,
    credential_database_uuid: str,
    project_response: object,
    coverage_response: object,
    timeline_response: object,
) -> CanonicalEditorSnapshot:
    normalized_project = _identifier(project_id, "project_id")
    project_envelope = _mapping(project_response, "project_response")
    project = _mapping(project_envelope.get("project"), "project")
    coverage = _mapping(coverage_response, "coverage")
    timeline_workspace = _mapping(timeline_response, "timeline_workspace")
    database_uuid = _identifier(project.get("database_uuid"), "project.database_uuid")
    if database_uuid != credential_database_uuid:
        _fail(
            "canonical_editor_database_mismatch",
            "The paired database changed; pair the project again.",
        )
    if (
        project.get("object") != "creative.project_workspace"
        or project.get("schema_version") != "1"
        or project.get("canonical_source") != "creative_blueprint"
        or project.get("workspace_mode") != "canonical_blueprint"
        or project.get("project_id") != normalized_project
        or coverage.get("object") != "coverage_plan.workspace"
        or coverage.get("schema_version") != "1"
        or coverage.get("database_uuid") != database_uuid
        or coverage.get("project_id") != normalized_project
        or timeline_workspace.get("object") != "canonical_timeline.workspace"
        or timeline_workspace.get("schema_version") != "1"
        or timeline_workspace.get("database_uuid") != database_uuid
        or timeline_workspace.get("project_id") != normalized_project
    ):
        _fail(
            "canonical_editor_projection_mismatch",
            "Project, Coverage, and Timeline are not one canonical workspace.",
        )
    project_capabilities = _mapping(project.get("capabilities"), "project.capabilities")
    if project.get("status") == "archived" or project_capabilities.get(
        "timeline_mutation"
    ) is not True:
        _fail(
            "canonical_editor_not_editable",
            "The current project does not admit canonical Timeline edits.",
        )
    if _mapping(coverage.get("freshness"), "coverage.freshness").get("state") != "current":
        _fail("canonical_editor_not_current", "Coverage is not current.")
    if _mapping(timeline_workspace.get("freshness"), "timeline.freshness").get(
        "state"
    ) != "current":
        _fail("canonical_editor_not_current", "Canonical Timeline is not current.")

    blueprint = _mapping(project.get("current_blueprint"), "project.current_blueprint")
    expected_blueprint = _blueprint_binding(
        blueprint, "project.current_blueprint"
    )
    project_coverage = _mapping(project.get("coverage"), "project.coverage")
    project_timeline = _mapping(project.get("timeline"), "project.timeline")
    coverage_head = _coverage_head(coverage.get("head"), "coverage.head")
    coverage_selected = _mapping(
        coverage.get("selected_revision"), "coverage.selected_revision"
    )
    if coverage_selected.get("is_head") is not True:
        _fail("canonical_editor_projection_mismatch", "Coverage selection is not its head.")
    expected_coverage = {
        **coverage_head,
        "evidence_manifest_sha256": _digest(
            coverage_selected.get("evidence_manifest_sha256"),
            "coverage.selected_revision.evidence_manifest_sha256",
        ),
        "operation_id": _identifier(
            coverage_selected.get("operation_id"),
            "coverage.selected_revision.operation_id",
        ),
    }
    if any(coverage_selected.get(key) != value for key, value in coverage_head.items()):
        _fail("canonical_editor_projection_mismatch", "Coverage head does not match selection.")
    timeline_head = _timeline_head(timeline_workspace.get("head"), "timeline.head")
    selected = _mapping(
        timeline_workspace.get("selected_revision"), "timeline.selected_revision"
    )
    if selected.get("is_head") is not True or _timeline_head(
        selected, "timeline.selected_revision"
    ) != timeline_head:
        _fail("canonical_editor_projection_mismatch", "Timeline selection is not its head.")
    timeline = _timeline_document(
        timeline_workspace.get("timeline"), normalized_project
    )
    plan = dict(_mapping(coverage.get("coverage_plan"), "coverage.coverage_plan"))
    if (
        plan.get("object") != "memolens.coverage_plan"
        or plan.get("schema_version") != "1"
        or plan.get("project_id") != normalized_project
        or plan.get("revision") != expected_coverage["revision"]
        or canonical_sha256(plan) != expected_coverage["content_sha256"]
        or canonical_sha256(plan.get("evidence_manifest"))
        != expected_coverage["evidence_manifest_sha256"]
        or timeline_head["timeline_content_sha256"] != canonical_sha256(timeline)
        or timeline_head["timeline_id"] != timeline["timeline_id"]
        or timeline_head["revision"] != timeline["revision"]
        or timeline_head["blueprint_binding"] != expected_blueprint
        or timeline_head["coverage_binding"] != expected_coverage
        or timeline["blueprint_binding"] != expected_blueprint
        or timeline["coverage_binding"] != expected_coverage
        or project_timeline.get("state") != "current"
        or _timeline_head(project_timeline.get("head"), "project.timeline.head")
        != timeline_head
        or project_coverage.get("state") != "current"
        or _coverage_head(project_coverage.get("head"), "project.coverage.head")
        != coverage_head
    ):
        _fail(
            "canonical_editor_projection_mismatch",
            "Canonical upstream and Timeline heads do not match exactly.",
        )
    plan_blueprint = _mapping(plan.get("blueprint_binding"), "coverage.blueprint_binding")
    if any(
        plan_blueprint.get(key) != expected_blueprint[key]
        for key in ("revision", "content_sha256", "semantic_sha256")
    ):
        _fail(
            "canonical_editor_projection_mismatch",
            "Coverage does not bind the exact current Blueprint.",
        )
    raw_source_bindings = timeline_workspace.get("source_bindings")
    source_currency = _validate_source_bindings(
        raw_source_bindings,
        timeline=timeline,
        plan=plan,
    )
    coverage_currency = _coverage_image_evidence_currency(plan)
    evidence_currency = (
        "legacy_non_current"
        if "legacy_non_current" in {source_currency, coverage_currency}
        else "current"
    )
    alternatives = _safe_alternatives(plan, timeline)
    public = {
        "object": "memolens.canonical_editor_projection",
        "schema_version": "1",
        "origin": CANONICAL_EDITOR_ORIGIN,
        "database_uuid": database_uuid,
        "project_id": normalized_project,
        "blueprint_head": expected_blueprint,
        "coverage_head": expected_coverage,
        "timeline_head": timeline_head,
        "timeline": timeline,
        "replacement_candidates": alternatives,
        "evidence_currency": evidence_currency,
        "eligible_for_current_use": evidence_currency == "current",
        "reason_code": (
            "legacy_image_identity_only"
            if evidence_currency == "legacy_non_current"
            else None
        ),
        "content_preview": {
            "canonical_timing_exact": True,
            "identity_redacted": False,
            "fixed_execution_source_proven": False,
            "media_playback": False,
        },
    }
    return CanonicalEditorSnapshot(
        public=public,
        expected_blueprint=expected_blueprint,
        expected_coverage=expected_coverage,
        expected_timeline_head=timeline_head,
        coverage_plan=plan,
        source_bindings=tuple(
            deepcopy(dict(row))
            for row in raw_source_bindings
            if isinstance(row, Mapping)
        ),
    )


def canonical_editor_historical_snapshot(
    *,
    current: CanonicalEditorSnapshot,
    timeline_response: object,
) -> CanonicalEditorHistoricalSnapshot:
    """Validate one read-only K against the exact current N snapshot."""

    workspace = _mapping(timeline_response, "historical_timeline_workspace")
    database_uuid = current.public["database_uuid"]
    project_id = current.public["project_id"]
    freshness = _mapping(
        workspace.get("freshness"),
        "historical_timeline.freshness",
    )
    freshness_state = freshness.get("state")
    if freshness_state not in {
        "current",
        "stale_blueprint",
        "stale_coverage",
        "stale_source_binding",
    }:
        _fail(
            "canonical_editor_history_mismatch",
            "Historical Timeline freshness is invalid.",
        )
    if (
        workspace.get("object") != "canonical_timeline.workspace"
        or workspace.get("schema_version") != "1"
        or workspace.get("database_uuid") != database_uuid
        or workspace.get("project_id") != project_id
    ):
        _fail(
            "canonical_editor_history_mismatch",
            "Historical Timeline does not belong to the exact current workspace.",
        )
    observed_head = _timeline_head(
        workspace.get("head"), "historical_timeline.head"
    )
    selected_value = _mapping(
        workspace.get("selected_revision"),
        "historical_timeline.selected_revision",
    )
    restore_from = _timeline_head(
        selected_value,
        "historical_timeline.selected_revision",
    )
    timeline = _timeline_document(workspace.get("timeline"), str(project_id))
    current_head = current.expected_timeline_head
    if (
        observed_head != current_head
        or selected_value.get("is_head") is not False
        or restore_from["revision"] >= current_head["revision"]
        or restore_from["timeline_id"] != current_head["timeline_id"]
        or timeline["revision"] != restore_from["revision"]
        or timeline["timeline_id"] != restore_from["timeline_id"]
        or timeline["blueprint_binding"] != restore_from["blueprint_binding"]
        or timeline["coverage_binding"] != restore_from["coverage_binding"]
        or canonical_sha256(timeline) != restore_from["timeline_content_sha256"]
    ):
        _fail(
            "canonical_editor_history_mismatch",
            "Historical Timeline identity or upstream bindings do not match current N.",
        )
    source_bindings = workspace.get("source_bindings")
    evidence_currency = _validate_historical_source_binding_shape(
        source_bindings,
        timeline=timeline,
    )
    restore_eligible = (
        freshness_state == "current"
        and current.public.get("eligible_for_current_use") is True
        and evidence_currency == "current"
        and restore_from["blueprint_binding"] == current.expected_blueprint
        and restore_from["coverage_binding"] == current.expected_coverage
    )
    if restore_eligible:
        _validate_source_bindings(
            source_bindings,
            timeline=timeline,
            plan=current.coverage_plan,
        )
    return CanonicalEditorHistoricalSnapshot(
        public={
            "object": "memolens.canonical_editor_historical_projection",
            "schema_version": "1",
            "origin": CANONICAL_EDITOR_ORIGIN,
            "database_uuid": database_uuid,
            "project_id": project_id,
            "current_timeline_head": deepcopy(current_head),
            "selected_revision": deepcopy(restore_from),
            "freshness": deepcopy(freshness),
            "restore_eligible": restore_eligible,
            "evidence_currency": evidence_currency,
            "eligible_for_current_use": evidence_currency == "current",
            "reason_code": (
                "legacy_image_identity_only"
                if evidence_currency == "legacy_non_current"
                else None
            ),
            "timeline": timeline,
            "content_preview": {
                "canonical_timing_exact": True,
                "identity_redacted": False,
                "fixed_execution_source_proven": False,
                "media_playback": False,
                "read_only": True,
            },
        },
        restore_from=restore_from,
        restore_eligible=restore_eligible,
    )


class PairedCanonicalEditorBackend:
    """Server-side credential holder for one canonical editor session."""

    def __init__(
        self,
        *,
        project_id: str,
        credential: Mapping[str, Any],
        client: AgentApiClient,
    ) -> None:
        self.project_id = _identifier(project_id, "project_id")
        self.credential = dict(credential)
        self.client = client
        actions = self.credential.get("actions")
        self.actions = tuple(actions) if isinstance(actions, list) else ()
        self.can_edit = TIMELINE_EDIT_ACTION in self.actions
        self.can_structural_edit = TIMELINE_STRUCTURAL_EDIT_ACTION in self.actions
        self.can_restore = TIMELINE_RESTORE_ACTION in self.actions
        self.can_preview = TIMELINE_PREVIEW_ACTION in self.actions

    def read(self) -> CanonicalEditorSnapshot:
        database_uuid = self.credential.get("database_uuid")
        if type(database_uuid) is not str:
            _fail(
                "agent_pairing_required",
                "The paired Agent credential has no database identity.",
                status=403,
            )
        return canonical_editor_snapshot(
            project_id=self.project_id,
            credential_database_uuid=database_uuid,
            project_response=self.client.project_workspace(self.project_id),
            coverage_response=self.client.coverage_workspace(self.project_id),
            timeline_response=self.client.timeline_workspace(self.project_id),
        )

    def thumbnail_for_clip(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
        clip_id: str,
    ) -> bytes:
        """Resolve one current clip to a verified backend thumbnail.

        The Browser supplies only the opaque clip identity.  Asset/span
        identities are recovered from the already admitted server snapshot,
        so this method never accepts or returns a source locator.
        """

        normalized = _identifier(clip_id, "clip_id")
        clips = snapshot.public["timeline"]["tracks"][0]["clips"]
        clip = next(
            (row for row in clips if row.get("clip_id") == normalized),
            None,
        )
        if not isinstance(clip, Mapping):
            _fail(
                "canonical_editor_media_unavailable",
                "The requested clip is not in the current Timeline snapshot.",
                status=404,
            )
        media_kind = clip.get("media_kind")
        asset_id = clip.get("asset_id")
        if media_kind not in {"image", "video"} or type(asset_id) is not str:
            _fail(
                "canonical_editor_media_unavailable",
                "The requested clip has no supported verified thumbnail.",
                status=404,
            )
        span_id = clip.get("span_id") if media_kind == "video" else None
        try:
            return self.client.media_thumbnail(
                media_kind=media_kind,
                asset_id=asset_id,
                span_id=span_id if isinstance(span_id, str) else None,
            )
        except AgentApiError as exc:
            _fail(
                "canonical_editor_media_unavailable",
                "The verified clip thumbnail is unavailable.",
                status=exc.status if exc.status in {404, 409} else 503,
            )
        except MemoLensError:
            _fail(
                "canonical_editor_media_unavailable",
                "The verified clip thumbnail is unavailable.",
                status=503,
            )

    def thumbnail_for_replacement(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
        clip_id: str,
        assignment_id: str,
    ) -> bytes:
        """Resolve an eligible same-Beat alternative to verified media bytes.

        The Browser supplies only the current clip and Coverage assignment
        identities.  Media identities are recovered from the server-held
        replacement projection and checked again against the admitted
        same-Beat alternative plus its verified Coverage proof.
        """

        _require_current_editor_evidence(snapshot)

        normalized_clip = _identifier(clip_id, "clip_id")
        normalized_assignment = _identifier(assignment_id, "assignment_id")
        clips = snapshot.public["timeline"]["tracks"][0]["clips"]
        clip = next(
            (row for row in clips if row.get("clip_id") == normalized_clip),
            None,
        )
        candidates_by_clip = snapshot.public.get("replacement_candidates")
        candidates = (
            candidates_by_clip.get(normalized_clip)
            if isinstance(candidates_by_clip, Mapping)
            else None
        )
        candidate = next(
            (
                row
                for row in candidates
                if isinstance(row, Mapping)
                and row.get("assignment_id") == normalized_assignment
            ),
            None,
        ) if isinstance(candidates, list) else None
        if not isinstance(clip, Mapping) or not isinstance(candidate, Mapping):
            _fail(
                "canonical_editor_replacement_unavailable",
                "The requested assignment is not an eligible same-Beat verified alternative.",
                status=404,
            )

        beats = snapshot.coverage_plan.get("beats")
        beat = next(
            (
                row
                for row in beats
                if isinstance(row, Mapping) and row.get("beat_id") == clip.get("beat_id")
            ),
            None,
        ) if isinstance(beats, list) else None
        alternatives = beat.get("alternatives") if isinstance(beat, Mapping) else None
        admitted_alternative = next(
            (
                row
                for row in alternatives
                if isinstance(row, Mapping)
                and row.get("assignment_id") == normalized_assignment
                and row.get("evidence_ref") == candidate.get("evidence_ref")
            ),
            None,
        ) if isinstance(alternatives, list) else None
        manifest = snapshot.coverage_plan.get("evidence_manifest")
        evidence = next(
            (
                row
                for row in manifest
                if isinstance(row, Mapping)
                and row.get("evidence_ref") == candidate.get("evidence_ref")
            ),
            None,
        ) if isinstance(manifest, list) else None
        proof = evidence.get("proof") if isinstance(evidence, Mapping) else None
        proof_sha = evidence.get("proof_sha256") if isinstance(evidence, Mapping) else None
        media_kind = candidate.get("media_kind")
        asset_id = candidate.get("asset_id")
        asset_sha = candidate.get("asset_sha256")
        current_duration = int(clip["end_ms"]) - int(clip["start_ms"])
        proof_kind = proof.get("kind") if isinstance(proof, Mapping) else None
        common_valid = (
            isinstance(admitted_alternative, Mapping)
            and isinstance(evidence, Mapping)
            and evidence.get("status") == "verified"
            and isinstance(proof, Mapping)
            and type(proof_sha) is str
            and _SHA256.fullmatch(proof_sha) is not None
            and canonical_sha256(dict(proof)) == proof_sha
            and media_kind in {"image", "video"}
            and type(asset_id) is str
            and type(asset_sha) is str
            and proof.get("asset_id") == asset_id
            and proof.get("asset_sha256") == asset_sha
            and candidate.get("current_slot_duration_ms") == current_duration
        )
        span_id: str | None = None
        if media_kind == "image":
            kind_valid = (
                proof_kind == "asset"
                and set(proof) == _CURRENT_IMAGE_PROOF_FIELDS
                and all(
                    candidate.get(field) == proof.get(field)
                    for field in _IMAGE_ANALYSIS_FIELDS
                )
                and candidate.get("available_duration_ms") is None
            )
        else:
            proof_start = proof.get("start_ms") if isinstance(proof, Mapping) else None
            proof_end = proof.get("end_ms") if isinstance(proof, Mapping) else None
            proof_span = proof.get("span_id") if isinstance(proof, Mapping) else None
            kind_valid = (
                proof_kind == "span"
                and type(proof_span) is str
                and type(proof_start) is int
                and type(proof_end) is int
                and proof_end > proof_start
                and proof_end - proof_start >= current_duration
                and candidate.get("available_duration_ms") == proof_end - proof_start
            )
            if kind_valid:
                span_id = proof_span
        if not common_valid or not kind_valid:
            _fail(
                "canonical_editor_replacement_unavailable",
                "The requested assignment is not an eligible same-Beat verified alternative.",
                status=404,
            )
        try:
            return self.client.media_thumbnail(
                media_kind=media_kind,
                asset_id=asset_id,
                span_id=span_id,
            )
        except AgentApiError as exc:
            _fail(
                "canonical_editor_replacement_unavailable",
                "The verified replacement thumbnail is unavailable.",
                status=exc.status if exc.status in {404, 409} else 503,
            )
        except MemoLensError:
            _fail(
                "canonical_editor_replacement_unavailable",
                "The verified replacement thumbnail is unavailable.",
                status=503,
            )

    def mint_preview_lease(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
    ) -> dict[str, Any]:
        """Mint and cross-check one private exact-head source preview lease."""

        _require_current_editor_evidence(snapshot)

        if not self.can_preview:
            _fail(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical media preview.",
                status=403,
            )
        head = {
            "revision": snapshot.expected_timeline_head["revision"],
            "content_sha256": snapshot.expected_timeline_head[
                "timeline_content_sha256"
            ],
        }
        try:
            lease = self.client.mint_timeline_preview_lease(
                project_id=self.project_id,
                observed_head=head,
                credential=self.credential,
            )
        except AgentApiError as exc:
            _fail(exc.code, str(exc), status=exc.status)
        except MemoLensError as exc:
            _fail(exc.code, str(exc), status=503)
        timeline_clips = snapshot.public["timeline"]["tracks"][0]["clips"]
        expected = {
            str(clip["clip_id"]): {
                "clip_id": clip["clip_id"],
                "timeline_start_ms": clip["start_ms"],
                "timeline_end_ms": clip["end_ms"],
                "source_in_ms": clip["source_in_ms"],
                "source_out_ms": clip["source_out_ms"],
            }
            for clip in timeline_clips
            if clip.get("media_kind") == "video"
        }
        returned = {
            str(clip.get("clip_id")): clip
            for clip in lease.get("clips", [])
            if isinstance(clip, Mapping)
        }
        if (
            lease.get("project_id") != self.project_id
            or lease.get("observed_head") != head
            or set(returned) != set(expected)
            or any(
                any(row.get(key) != value for key, value in expected[clip_id].items())
                for clip_id, row in returned.items()
            )
        ):
            self.drop_preview_lease(lease)
            _fail(
                "canonical_editor_preview_mismatch",
                "The preview lease does not match the exact current Timeline.",
                status=502,
            )
        return lease

    def preview_media(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
        lease: Mapping[str, Any],
        clip_id: str,
        method: str,
        range_header: str | None,
    ) -> AgentPreviewMediaResponse:
        """Open one private upstream stream for an exact current video clip."""

        _require_current_editor_evidence(snapshot)

        if not self.can_preview:
            _fail(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical media preview.",
                status=403,
            )
        normalized = _identifier(clip_id, "clip_id")
        clips = snapshot.public["timeline"]["tracks"][0]["clips"]
        clip = next(
            (
                row
                for row in clips
                if row.get("clip_id") == normalized
                and row.get("media_kind") == "video"
            ),
            None,
        )
        observed = lease.get("observed_head")
        if (
            not isinstance(clip, Mapping)
            or observed
            != {
                "revision": snapshot.expected_timeline_head["revision"],
                "content_sha256": snapshot.expected_timeline_head[
                    "timeline_content_sha256"
                ],
            }
            or not any(
                isinstance(row, Mapping)
                and row.get("clip_id") == normalized
                and row.get("status") == "playable"
                for row in lease.get("clips", [])
            )
        ):
            _fail(
                "canonical_editor_media_unavailable",
                "The requested clip has no current playable source grant.",
                status=404,
            )
        try:
            return self.client.timeline_preview_media(
                lease=lease,
                clip_id=normalized,
                method=method,
                range_header=range_header,
            )
        except AgentApiError as exc:
            _fail(exc.code, str(exc), status=exc.status)
        except MemoLensError as exc:
            _fail(exc.code, str(exc), status=503)

    @staticmethod
    def drop_preview_lease(lease: dict[str, Any] | None) -> None:
        """Forget server-held authority immediately; Core expires it separately."""

        if lease is not None:
            lease.clear()

    def read_revision(
        self,
        revision: int,
        *,
        current: CanonicalEditorSnapshot,
    ) -> CanonicalEditorHistoricalSnapshot:
        if (
            type(revision) is not int
            or revision < 1
            or revision >= current.expected_timeline_head["revision"]
        ):
            _fail(
                "canonical_editor_history_invalid",
                "Historical revision must be below the exact current head.",
                status=422,
            )
        return canonical_editor_historical_snapshot(
            current=current,
            timeline_response=self.client.timeline_workspace(
                self.project_id,
                revision=revision,
            ),
        )

    def save(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
        edit: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_current_editor_evidence(snapshot)
        if not self.can_edit:
            _fail(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical Timeline edits.",
                status=403,
            )
        body = {
            "expected_blueprint": snapshot.expected_blueprint,
            "expected_coverage": snapshot.expected_coverage,
            "expected_timeline_head": snapshot.expected_timeline_head,
            "edit": dict(edit),
        }
        identity = [
            TIMELINE_EDIT_ACTION,
            "1",
            self.credential["database_uuid"],
            snapshot.expected_timeline_head,
            body["edit"],
        ]
        key = (
            "agent-timeline-edit-"
            + str(self.credential["database_uuid"]).replace("-", "")[:12]
            + f"-t{snapshot.expected_timeline_head['revision']}-"
            + canonical_sha256(identity)
        )
        return self.client.timeline_write(
            command="edit",
            project_id=self.project_id,
            body=body,
            idempotency_key=key,
            credential=self.credential,
        )

    def save_restore(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
        historical: CanonicalEditorHistoricalSnapshot,
    ) -> dict[str, Any]:
        _require_current_editor_evidence(snapshot)
        if not historical.restore_eligible:
            _fail(
                "canonical_editor_history_not_restorable",
                "Historical identity-only image evidence is audit-only and cannot become N+1.",
                status=409,
            )
        if not self.can_restore:
            _fail(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical Timeline restore.",
                status=403,
            )
        if (
            historical.public.get("current_timeline_head")
            != snapshot.expected_timeline_head
            or historical.restore_from["revision"]
            >= snapshot.expected_timeline_head["revision"]
        ):
            _fail(
                "canonical_editor_history_mismatch",
                "Historical selection no longer belongs to the exact current head.",
            )
        body = {
            "expected_blueprint": snapshot.expected_blueprint,
            "expected_coverage": snapshot.expected_coverage,
            "expected_timeline_head": snapshot.expected_timeline_head,
            "restore_from": historical.restore_from,
        }
        identity = [
            TIMELINE_RESTORE_ACTION,
            "1",
            self.credential["database_uuid"],
            snapshot.expected_timeline_head,
            historical.restore_from,
        ]
        key = (
            "agent-timeline-restore-"
            + str(self.credential["database_uuid"]).replace("-", "")[:12]
            + f"-n{snapshot.expected_timeline_head['revision']}"
            + f"-k{historical.restore_from['revision']}-"
            + canonical_sha256(identity)
        )
        return self.client.timeline_write(
            command="restore",
            project_id=self.project_id,
            body=body,
            idempotency_key=key,
            credential=self.credential,
        )

    def save_structural_edit(
        self,
        *,
        snapshot: CanonicalEditorSnapshot,
        structural_edit: Mapping[str, Any],
    ) -> dict[str, Any]:
        _require_current_editor_evidence(snapshot)
        if not self.can_structural_edit:
            _fail(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical Timeline structural edits.",
                status=403,
            )
        body = {
            "expected_blueprint": snapshot.expected_blueprint,
            "expected_coverage": snapshot.expected_coverage,
            "expected_timeline_head": snapshot.expected_timeline_head,
            "structural_edit": dict(structural_edit),
        }
        identity = [
            TIMELINE_STRUCTURAL_EDIT_ACTION,
            "1",
            self.credential["database_uuid"],
            snapshot.expected_timeline_head,
            body["structural_edit"],
        ]
        key = (
            "agent-timeline-structural-"
            + str(self.credential["database_uuid"]).replace("-", "")[:12]
            + f"-t{snapshot.expected_timeline_head['revision']}-"
            + canonical_sha256(identity)
        )
        return self.client.timeline_write(
            command="structural_edit",
            project_id=self.project_id,
            body=body,
            idempotency_key=key,
            credential=self.credential,
        )


def paired_canonical_editor_backend(
    project_id: str,
    *,
    timeout: float = 5.0,
) -> PairedCanonicalEditorBackend:
    normalized = _identifier(project_id, "project_id")
    credential = load_agent_credential(normalized)
    workflow = AgentPairingWorkflow(
        base_url=str(credential["base_url"]),
        timeout=timeout,
    )
    # Refresh once so a native-approved pending pairing can become active and
    # so an expired/revoked capability fails before a browser session is made.
    workflow.status(normalized)
    credential = load_agent_credential(normalized)
    if credential.get("status") != "active":
        raise MemoLensError(
            "The Agent pairing is not active. Review it in MemoLens Desktop.",
            code="agent_pairing_required",
        )
    paired_actions = credential.get("actions", [])
    if not any(
        action in paired_actions
        for action in (
            TIMELINE_EDIT_ACTION,
            TIMELINE_STRUCTURAL_EDIT_ACTION,
            TIMELINE_RESTORE_ACTION,
            TIMELINE_PREVIEW_ACTION,
        )
    ):
        raise MemoLensError(
            "The active pairing does not allow canonical Timeline edit, structural edit, restore, or media preview. Pair the project again with one explicit Timeline action.",
            code="agent_pairing_scope_denied",
        )
    return PairedCanonicalEditorBackend(
        project_id=normalized,
        credential=credential,
        client=workflow.client,
    )


def closed_canonical_edit(
    value: object,
    *,
    snapshot: CanonicalEditorSnapshot,
) -> tuple[dict[str, object], dict[str, Any] | None]:
    _require_current_editor_evidence(snapshot)
    if type(value) is not dict or value.get("op") not in _EDIT_FIELDS:
        _fail("invalid_canonical_editor_action", "Edit operation is unsupported.", status=422)
    op = str(value["op"])
    if set(value) != _EDIT_FIELDS[op]:
        _fail("invalid_canonical_editor_action", "Edit fields are not closed.", status=422)
    edit = dict(value)
    clip_id = _identifier(edit.get("clip_id"), "edit.clip_id")
    timeline = snapshot.public["timeline"]
    clips = timeline["tracks"][0]["clips"]
    index = next(
        (i for i, clip in enumerate(clips) if clip.get("clip_id") == clip_id),
        None,
    )
    if index is None:
        _fail("invalid_canonical_editor_action", "Clip is not in the current head.", status=422)
    clip = clips[index]
    next_clips = deepcopy(clips)
    preview: dict[str, Any] | None = None
    if op == "move_clip":
        to_index = edit.get("to_index")
        if type(to_index) is not int or not 0 <= to_index < len(clips) or to_index == index:
            _fail("invalid_canonical_editor_action", "Move destination is invalid.", status=422)
        moved = next_clips.pop(index)
        next_clips.insert(to_index, moved)
    elif op == "trim_clip":
        source_in = edit.get("source_in_ms")
        source_out = edit.get("source_out_ms")
        evidence = next(
            (
                row
                for row in snapshot.coverage_plan["evidence_manifest"]
                if row.get("evidence_ref") == clip["evidence_ref"]
            ),
            None,
        )
        proof = evidence.get("proof") if isinstance(evidence, Mapping) else None
        proof_start = proof.get("start_ms") if isinstance(proof, Mapping) else None
        proof_end = proof.get("end_ms") if isinstance(proof, Mapping) else None
        if (
            clip.get("media_kind") != "video"
            or type(source_in) is not int
            or type(source_out) is not int
            or source_in < 0
            or source_out > MAX_SOURCE_TIME_MS
            or source_out - source_in < MIN_VIDEO_TRIM_MS
            or not isinstance(proof, Mapping)
            or proof.get("kind") != "span"
            or proof.get("span_id") != clip.get("span_id")
            or type(proof_start) is not int
            or type(proof_end) is not int
            or source_in < proof_start
            or source_out > proof_end
        ):
            _fail("invalid_canonical_editor_action", "Trim range is invalid.", status=422)
        next_clips[index]["source_in_ms"] = source_in
        next_clips[index]["source_out_ms"] = source_out
        next_clips[index]["end_ms"] = int(next_clips[index]["start_ms"]) + source_out - source_in
    elif op == "set_clip_duration":
        duration = edit.get("duration_ms")
        if (
            clip.get("media_kind") != "image"
            or type(duration) is not int
            or not 1 <= duration <= MAX_TIMELINE_DURATION_MS
        ):
            _fail("invalid_canonical_editor_action", "Image duration is invalid.", status=422)
        next_clips[index]["end_ms"] = int(next_clips[index]["start_ms"]) + duration
    else:
        assignment_id = _identifier(edit.get("assignment_id"), "edit.assignment_id")
        candidates = snapshot.public["replacement_candidates"].get(clip_id, [])
        if not any(item.get("assignment_id") == assignment_id for item in candidates):
            _fail(
                "invalid_canonical_editor_action",
                "Replacement is not an eligible same-Beat verified alternative.",
                status=422,
            )
        return edit, None

    cursor = 0
    for ordinal, row in enumerate(next_clips):
        duration = int(row["end_ms"]) - int(row["start_ms"])
        if duration < 1:
            _fail("invalid_canonical_editor_action", "Edited duration is invalid.", status=422)
        row["ordinal"] = ordinal
        row["start_ms"] = cursor
        row["end_ms"] = cursor + duration
        cursor += duration
    if cursor > MAX_TIMELINE_DURATION_MS:
        _fail("invalid_canonical_editor_action", "Edited Timeline is too long.", status=422)
    preview_timeline = deepcopy(timeline)
    preview_timeline["output"]["duration_ms"] = cursor
    preview_timeline["tracks"][0]["clips"] = next_clips
    preview = {
        "object": "canonical_timeline.pending_preview",
        "schema_version": "1",
        "database_uuid": snapshot.public["database_uuid"],
        "project_id": snapshot.public["project_id"],
        "based_on_head": snapshot.expected_timeline_head,
        "timeline": preview_timeline,
        "canonical_timing_exact": True,
        "identity_redacted": False,
        "fixed_execution_source_proven": False,
    }
    return edit, preview


def closed_canonical_structural_edit(
    value: object,
    *,
    snapshot: CanonicalEditorSnapshot,
) -> tuple[dict[str, object], dict[str, Any]]:
    """Validate one closed structural intent and build a noncanonical preview.

    The preview carries deterministic process-local child labels only so the
    Browser can review geometry.  Those labels are never submitted to Core;
    Core remains the sole authority for canonical child clip identity.
    """

    _require_current_editor_evidence(snapshot)
    if type(value) is not dict or value.get("op") not in _STRUCTURAL_EDIT_FIELDS:
        _fail(
            "invalid_canonical_editor_action",
            "Structural edit operation is unsupported.",
            status=422,
        )
    op = str(value["op"])
    if set(value) != _STRUCTURAL_EDIT_FIELDS[op]:
        _fail(
            "invalid_canonical_editor_action",
            "Structural edit fields are not closed.",
            status=422,
        )
    structural_edit = dict(value)
    clip_id = _identifier(
        structural_edit.get("clip_id"),
        "structural_edit.clip_id",
    )
    timeline = snapshot.public["timeline"]
    clips = timeline["tracks"][0]["clips"]
    index = next(
        (i for i, clip in enumerate(clips) if clip.get("clip_id") == clip_id),
        None,
    )
    if index is None:
        _fail(
            "invalid_canonical_editor_action",
            "Clip is not in the current head.",
            status=422,
        )
    clip = clips[index]
    next_clips = deepcopy(clips)
    if op == "split_clip":
        if len(clips) >= MAX_TIMELINE_CLIPS:
            _fail(
                "invalid_canonical_editor_action",
                f"Structural Timeline cannot exceed {MAX_TIMELINE_CLIPS} clips.",
                status=422,
            )
        source_split = structural_edit.get("source_split_ms")
        source_in = clip.get("source_in_ms")
        source_out = clip.get("source_out_ms")
        timeline_duration = int(clip["end_ms"]) - int(clip["start_ms"])
        if (
            clip.get("media_kind") != "video"
            or type(source_split) is not int
            or type(source_in) is not int
            or type(source_out) is not int
            or source_out - source_in != timeline_duration
            or source_split < source_in + MIN_VIDEO_TRIM_MS
            or source_split > source_out - MIN_VIDEO_TRIM_MS
        ):
            _fail(
                "invalid_canonical_editor_action",
                "Split requires a video playhead strictly inside the clip with at least 100 ms on each side.",
                status=422,
            )
        identity_seed = [
            "pending_structural_split",
            snapshot.expected_timeline_head,
            clip_id,
            source_split,
        ]
        left = deepcopy(clip)
        right = deepcopy(clip)
        left["clip_id"] = (
            "pending_split_left_" + canonical_sha256([*identity_seed, "left"])[:24]
        )
        right["clip_id"] = (
            "pending_split_right_" + canonical_sha256([*identity_seed, "right"])[:24]
        )
        left["source_out_ms"] = source_split
        left["end_ms"] = int(clip["start_ms"]) + source_split - source_in
        right["source_in_ms"] = source_split
        right["start_ms"] = int(left["end_ms"])
        next_clips[index : index + 1] = [left, right]
    else:
        if len(clips) <= 1:
            _fail(
                "invalid_canonical_editor_action",
                "The last Timeline clip cannot be deleted.",
                status=422,
            )
        next_clips.pop(index)

    cursor = 0
    for ordinal, row in enumerate(next_clips):
        duration = int(row["end_ms"]) - int(row["start_ms"])
        if duration < 1:
            _fail(
                "invalid_canonical_editor_action",
                "Structural edit produced an invalid duration.",
                status=422,
            )
        row["ordinal"] = ordinal
        row["start_ms"] = cursor
        row["end_ms"] = cursor + duration
        cursor += duration
    preview_timeline = deepcopy(timeline)
    preview_timeline["output"]["duration_ms"] = cursor
    preview_timeline["tracks"][0]["clips"] = next_clips
    preview = {
        "object": "canonical_timeline.pending_preview",
        "schema_version": "1",
        "database_uuid": snapshot.public["database_uuid"],
        "project_id": snapshot.public["project_id"],
        "based_on_head": snapshot.expected_timeline_head,
        "timeline": preview_timeline,
        "canonical_timing_exact": True,
        "identity_redacted": True,
        "fixed_execution_source_proven": False,
    }
    return structural_edit, preview


def validate_saved_result(
    *,
    project_id: str,
    prior: CanonicalEditorSnapshot,
    edit: Mapping[str, Any],
    result: object,
    reread: CanonicalEditorSnapshot,
) -> dict[str, Any]:
    row = _mapping(result, "save_result")
    operation = _mapping(row.get("result"), "save_result.result")
    result_head = _timeline_head(operation.get("result_head"), "save_result.result_head")
    if (
        row.get("object") != "canonical_timeline.command_result"
        or row.get("schema_version") != "1"
        or row.get("project_id") != project_id
        or row.get("command_type") != TIMELINE_EDIT_ACTION
        or row.get("operation_id") != result_head["operation_id"]
        or operation.get("kind") != "revision_created"
        or operation.get("edit") != dict(edit)
        or result_head["revision"] != prior.expected_timeline_head["revision"] + 1
        or result_head["blueprint_binding"] != prior.expected_blueprint
        or result_head["coverage_binding"] != prior.expected_coverage
        or result_head != reread.expected_timeline_head
        or reread.public["timeline"]["revision"] != result_head["revision"]
        or reread.public["timeline_head"] != result_head
    ):
        _fail(
            "canonical_editor_save_reread_mismatch",
            "Saved result did not match the canonical reread.",
            status=502,
        )
    return {
        "status": "saved",
        "command_type": TIMELINE_EDIT_ACTION,
        "revision": result_head["revision"],
        "operation_id": result_head["operation_id"],
        "result_head": result_head,
    }


def validate_saved_structural_result(
    *,
    project_id: str,
    prior: CanonicalEditorSnapshot,
    structural_edit: Mapping[str, Any],
    result: object,
    reread: CanonicalEditorSnapshot,
) -> dict[str, Any]:
    """Require an exact N+1 reread and the structural effect requested.

    This is intentionally stricter than trusting the command receipt.  In
    particular, split children must carry fresh Core-owned clip identities so
    a subsequent preview lease can be minted against those exact identities.
    """

    row = _mapping(result, "structural_save_result")
    operation = _mapping(
        row.get("result"),
        "structural_save_result.result",
    )
    result_head = _timeline_head(
        operation.get("result_head"),
        "structural_save_result.result_head",
    )
    timeline = reread.public["timeline"]
    parent = timeline.get("parent")
    expected_parent = {
        "revision": prior.expected_timeline_head["revision"],
        "content_sha256": prior.expected_timeline_head[
            "timeline_content_sha256"
        ],
    }
    if (
        row.get("object") != "canonical_timeline.command_result"
        or row.get("schema_version") != "1"
        or row.get("project_id") != project_id
        or row.get("command_type") != TIMELINE_STRUCTURAL_EDIT_ACTION
        or row.get("operation_id") != result_head["operation_id"]
        or operation.get("kind") != "revision_created"
        or operation.get("structural_edit") != dict(structural_edit)
        or result_head["revision"]
        != prior.expected_timeline_head["revision"] + 1
        or result_head["timeline_id"]
        != prior.expected_timeline_head["timeline_id"]
        or result_head["blueprint_binding"] != prior.expected_blueprint
        or result_head["coverage_binding"] != prior.expected_coverage
        or result_head != reread.expected_timeline_head
        or reread.public["timeline_head"] != result_head
        or timeline["revision"] != result_head["revision"]
        or timeline["blueprint_binding"] != prior.expected_blueprint
        or timeline["coverage_binding"] != prior.expected_coverage
        or parent != expected_parent
        or canonical_sha256(timeline) != result_head["timeline_content_sha256"]
    ):
        _fail(
            "canonical_editor_save_reread_mismatch",
            "Structural save result did not match the exact canonical reread.",
            status=502,
        )

    prior_clips = prior.public["timeline"]["tracks"][0]["clips"]
    next_clips = timeline["tracks"][0]["clips"]
    clip_id = str(structural_edit["clip_id"])
    index = next(
        (i for i, clip in enumerate(prior_clips) if clip["clip_id"] == clip_id),
        None,
    )
    effect_valid = index is not None
    op = structural_edit.get("op")
    if effect_valid and op == "split_clip":
        source_split = structural_edit.get("source_split_ms")
        parent_clip = prior_clips[index]
        prior_ids = {str(clip["clip_id"]) for clip in prior_clips}
        effect_valid = (
            type(source_split) is int
            and len(next_clips) == len(prior_clips) + 1
        )
        if effect_valid:
            left, right = next_clips[index : index + 2]
            expected_left = deepcopy(parent_clip)
            expected_right = deepcopy(parent_clip)
            expected_left.update(
                {
                    "clip_id": left["clip_id"],
                    "source_out_ms": source_split,
                    "end_ms": int(parent_clip["start_ms"])
                    + source_split
                    - int(parent_clip["source_in_ms"]),
                }
            )
            expected_right.update(
                {
                    "clip_id": right["clip_id"],
                    "source_in_ms": source_split,
                    "start_ms": expected_left["end_ms"],
                }
            )
            expected_clips = [
                *deepcopy(prior_clips[:index]),
                expected_left,
                expected_right,
                *deepcopy(prior_clips[index + 1 :]),
            ]
            cursor = 0
            for ordinal, expected in enumerate(expected_clips):
                duration = int(expected["end_ms"]) - int(expected["start_ms"])
                expected["ordinal"] = ordinal
                expected["start_ms"] = cursor
                expected["end_ms"] = cursor + duration
                cursor += duration
            effect_valid = (
                left["clip_id"] not in prior_ids
                and right["clip_id"] not in prior_ids
                and left["clip_id"] != right["clip_id"]
                and next_clips == expected_clips
            )
    elif effect_valid and op == "delete_clip":
        expected_clips = deepcopy(prior_clips)
        expected_clips.pop(index)
        cursor = 0
        for ordinal, expected in enumerate(expected_clips):
            duration = int(expected["end_ms"]) - int(expected["start_ms"])
            expected["ordinal"] = ordinal
            expected["start_ms"] = cursor
            expected["end_ms"] = cursor + duration
            cursor += duration
        effect_valid = next_clips == expected_clips
    else:
        effect_valid = False
    if not effect_valid:
        _fail(
            "canonical_editor_save_reread_mismatch",
            "Structural save did not produce the requested canonical clip topology.",
            status=502,
        )
    return {
        "status": "saved",
        "command_type": TIMELINE_STRUCTURAL_EDIT_ACTION,
        "revision": result_head["revision"],
        "operation_id": result_head["operation_id"],
        "result_head": result_head,
    }


def validate_saved_restore_result(
    *,
    project_id: str,
    prior: CanonicalEditorSnapshot,
    historical: CanonicalEditorHistoricalSnapshot,
    result: object,
    reread: CanonicalEditorSnapshot,
) -> dict[str, Any]:
    row = _mapping(result, "restore_result")
    operation = _mapping(row.get("result"), "restore_result.result")
    result_head = _timeline_head(
        operation.get("result_head"), "restore_result.result_head"
    )
    restore_from = _timeline_head(
        operation.get("restore_from"), "restore_result.restore_from"
    )
    expected_timeline = deepcopy(historical.public["timeline"])
    expected_timeline["revision"] = prior.expected_timeline_head["revision"] + 1
    expected_timeline["parent"] = {
        "revision": prior.expected_timeline_head["revision"],
        "content_sha256": prior.expected_timeline_head[
            "timeline_content_sha256"
        ],
    }
    if (
        row.get("object") != "canonical_timeline.command_result"
        or row.get("schema_version") != "1"
        or row.get("project_id") != project_id
        or row.get("command_type") != TIMELINE_RESTORE_ACTION
        or row.get("operation_id") != result_head["operation_id"]
        or operation.get("kind") != "revision_restored"
        or restore_from != historical.restore_from
        or result_head["revision"] != prior.expected_timeline_head["revision"] + 1
        or result_head["timeline_id"]
        != prior.expected_timeline_head["timeline_id"]
        or result_head["blueprint_binding"] != prior.expected_blueprint
        or result_head["coverage_binding"] != prior.expected_coverage
        or result_head != reread.expected_timeline_head
        or reread.public["timeline"]["revision"] != result_head["revision"]
        or reread.public["timeline_head"] != result_head
        or reread.public["timeline"] != expected_timeline
        or canonical_sha256(expected_timeline)
        != result_head["timeline_content_sha256"]
    ):
        _fail(
            "canonical_editor_save_reread_mismatch",
            "Restored result did not match the exact canonical reread.",
            status=502,
        )
    return {
        "status": "saved",
        "command_type": TIMELINE_RESTORE_ACTION,
        "revision": result_head["revision"],
        "operation_id": result_head["operation_id"],
        "restore_from_revision": restore_from["revision"],
        "result_head": result_head,
    }


__all__ = [
    "AgentApiError",
    "CANONICAL_EDITOR_OBJECT",
    "CANONICAL_EDITOR_ORIGIN",
    "CANONICAL_EDITOR_SCHEMA_VERSION",
    "TIMELINE_EDIT_ACTION",
    "TIMELINE_PREVIEW_ACTION",
    "TIMELINE_RESTORE_ACTION",
    "TIMELINE_STRUCTURAL_EDIT_ACTION",
    "CanonicalEditorError",
    "CanonicalEditorHistoricalSnapshot",
    "CanonicalEditorSnapshot",
    "PairedCanonicalEditorBackend",
    "canonical_editor_historical_snapshot",
    "canonical_editor_snapshot",
    "closed_canonical_edit",
    "closed_canonical_structural_edit",
    "paired_canonical_editor_backend",
    "validate_saved_restore_result",
    "validate_saved_result",
    "validate_saved_structural_result",
]
