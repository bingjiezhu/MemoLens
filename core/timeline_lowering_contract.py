"""Pure, deterministic Coverage Plan to canonical Timeline lowering.

This B2B contract deliberately compiles only a closed image/span first cut.
The canonical Timeline contains durable media identity, never a filesystem or
database-local source locator.  Execution-only ``asset_source_id`` values live
in a separate closed manifest so relocating an identical library does not
change Timeline bytes or its content digest.

Current-head admission remains a repository transaction responsibility.  The
compiler treats the supplied Blueprint and Coverage bindings as current-head
attestations and proves that they exactly identify the supplied documents.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .blueprint_contract import (
    BlueprintContractError,
    canonical_sha256 as blueprint_canonical_sha256,
    require_valid_blueprint_document,
)
from .coverage_contract import (
    CoverageContractError,
    canonical_sha256 as coverage_canonical_sha256,
    coverage_plan_content_sha256,
    require_current_coverage_plan,
    require_exact_baseline_derivation,
)
from .residual_identity_contract import (
    ResidualIdentityContractError,
    require_valid_residual_binding,
    residual_binding_sha256,
)


CANONICAL_TIMELINE_OBJECT = "memolens.canonical_timeline"
CANONICAL_TIMELINE_SCHEMA_VERSION = "1"
CANONICAL_TIMELINE_STRUCTURAL_SCHEMA_VERSION = "2"
CANONICAL_TIMELINE_SCHEMA_VERSIONS = frozenset(
    {
        CANONICAL_TIMELINE_SCHEMA_VERSION,
        CANONICAL_TIMELINE_STRUCTURAL_SCHEMA_VERSION,
    }
)
TIMELINE_LOWERER_COMPILER = "memolens.timeline-lowerer/v1"
TIMELINE_MAX_CANONICAL_BYTES = 1_048_576
TIMELINE_MAX_CLIPS = 256

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^(?:asset|img)_[0-9a-f]{24}$")
_ASSET_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_ASSET_EVIDENCE_REF = re.compile(r"^memolens://evidence/asset/(?P<asset_id>(?:asset|img)_[0-9a-f]{24})$")
_SPAN_ID = re.compile(
    r"^seg_(?P<asset_digest>[0-9a-f]{24})_"
    r"(?P<analysis_revision>[1-9][0-9]{0,6})_"
    r"(?P<ordinal>0|[1-9][0-9]{0,6})$"
)
_SPAN_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<span_id>seg_[0-9a-f]{24}_"
    r"[1-9][0-9]{0,6}_(?:0|[1-9][0-9]{0,6}))$"
)
_RESIDUAL_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<residual_id>rseg_[0-9a-f]{64})$"
)
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:5"}

_BLUEPRINT_BINDING_FIELDS = {
    "revision",
    "content_sha256",
    "semantic_sha256",
    "operation_id",
}
_COVERAGE_BINDING_FIELDS = {
    "revision",
    "content_sha256",
    "evidence_manifest_sha256",
    "operation_id",
}
_RESOLVED_SOURCE_COMMON_FIELDS = {
    "evidence_ref",
    "asset_id",
    "asset_sha256",
    "asset_source_id",
}
_IMAGE_ANALYSIS_FIELDS = {
    "analysis_run_id",
    "analysis_revision",
    "analysis_content_sha256",
    "source_binding_sha256",
}
_RESOLVED_SOURCE_IMAGE_FIELDS = _RESOLVED_SOURCE_COMMON_FIELDS | _IMAGE_ANALYSIS_FIELDS
_RESOLVED_SOURCE_RESIDUAL_FIELDS = _RESOLVED_SOURCE_COMMON_FIELDS | {
    "residual_binding",
}
_CANONICAL_SOURCE_COMMON_FIELDS = _RESOLVED_SOURCE_COMMON_FIELDS | {
    "clip_id",
    "coverage_proof_sha256",
    "media_kind",
}
_CANONICAL_SOURCE_IMAGE_FIELDS = _CANONICAL_SOURCE_COMMON_FIELDS | _IMAGE_ANALYSIS_FIELDS
_CANONICAL_SOURCE_VIDEO_FIELDS = _CANONICAL_SOURCE_COMMON_FIELDS | {
    "span_id",
    "analysis_run_id",
    "analysis_revision",
    "input_asset_sha256",
    "source_in_ms",
    "source_out_ms",
}
_RESIDUAL_VIDEO_FIELDS = {
    "residual_id",
    "parent_segment_id",
    "residual_binding",
}
_CANONICAL_SOURCE_RESIDUAL_FIELDS = (
    _CANONICAL_SOURCE_VIDEO_FIELDS | _RESIDUAL_VIDEO_FIELDS
)


def canonical_json(value: object) -> str:
    """Return the single canonical JSON encoding used by this contract."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TimelineLoweringContractError(ValueError):
    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]):
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_canonical_timeline"),
                "path": str(item.get("path") or ""),
                "message": str(item.get("message") or "Canonical Timeline is invalid."),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"] if normalized else "Canonical Timeline is invalid.",
        )


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) < 64:
            self.items.append({"code": code, "path": path, "message": message})


def _closed(
    value: object,
    *,
    path: str,
    fields: set[str],
    issues: _Issues,
) -> dict[str, Any] | None:
    if type(value) is not dict:
        issues.add("schema_type", path, "Value must be an object.")
        return None
    row = value
    for field in sorted(fields - set(row)):
        issues.add("required_field_missing", f"{path}/{field}", "A required field is missing.")
    if set(row) - fields:
        issues.add("unknown_field", f"{path}/*", "Object contains an unsupported field.")
    return row


def _identifier(value: object, path: str, issues: _Issues) -> bool:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        issues.add("invalid_identifier", path, "Identifier is invalid.")
        return False
    return True


def _sha256(value: object, path: str, issues: _Issues) -> bool:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        issues.add("invalid_sha256", path, "SHA-256 digest is invalid.")
        return False
    return True


def _positive_int(value: object, path: str, issues: _Issues) -> bool:
    if type(value) is not int or value < 1 or value > 1_800_000:
        issues.add("invalid_positive_integer", path, "Value must be a bounded positive integer.")
        return False
    return True


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _content_address_matches(asset_id: str, asset_sha256: str) -> bool:
    namespace = asset_id.split("_", 1)[0]
    return asset_id == f"{namespace}_{asset_sha256[:24]}"


def _expected_timeline_id(project_id: str) -> str:
    return _stable_id("timeline", project_id)


def _expected_track_id(timeline_id: str) -> str:
    return _stable_id("track", timeline_id, "primary_visual")


def _expected_clip_id(
    timeline_id: str,
    beat_id: str,
    assignment_id: str,
    evidence_ref: str,
    asset_id: str,
    asset_sha256: str,
    media_kind: str,
    span_identity: Sequence[object] = (),
) -> str:
    return _stable_id(
        "clip",
        timeline_id,
        beat_id,
        assignment_id,
        evidence_ref,
        asset_id,
        asset_sha256,
        media_kind,
        *span_identity,
    )


def canonical_timeline_clip_id(
    *,
    timeline_id: str,
    beat_id: str,
    assignment_id: str,
    evidence_ref: str,
    asset_id: str,
    asset_sha256: str,
    media_kind: str,
    span_identity: Sequence[object] = (),
) -> str:
    """Return the stable clip identity used by the canonical Timeline schema.

    Callers must still validate the enclosing Timeline.  This helper exposes
    the lowerer's existing identity algorithm so deterministic manual edits do
    not fork a second clip-ID scheme.
    """

    return _expected_clip_id(
        timeline_id,
        beat_id,
        assignment_id,
        evidence_ref,
        asset_id,
        asset_sha256,
        media_kind,
        span_identity,
    )


def _raise(code: str, path: str, message: str) -> None:
    raise TimelineLoweringContractError([{"code": code, "path": path, "message": message}])


def _exact_blueprint_binding(
    blueprint: Mapping[str, object],
    binding: Mapping[str, object],
) -> dict[str, object]:
    if type(binding) is not dict or set(binding) != _BLUEPRINT_BINDING_FIELDS:
        _raise(
            "invalid_blueprint_binding",
            "/blueprint_binding",
            "Blueprint binding fields are not closed.",
        )
    document = blueprint
    expected = {
        "revision": document["revision"],
        "content_sha256": blueprint_canonical_sha256(document),
        "semantic_sha256": blueprint_canonical_sha256(document["semantic"]),
        "operation_id": document["created_by_operation_id"],
    }
    if binding != expected:
        _raise(
            "blueprint_binding_mismatch",
            "/blueprint_binding",
            "Blueprint binding does not exactly identify the supplied current revision.",
        )
    return deepcopy(expected)


def _exact_coverage_binding(
    plan: Mapping[str, object],
    binding: Mapping[str, object],
) -> dict[str, object]:
    if type(binding) is not dict or set(binding) != _COVERAGE_BINDING_FIELDS:
        _raise(
            "invalid_coverage_binding",
            "/coverage_binding",
            "Coverage binding fields are not closed.",
        )
    operation_id = binding.get("operation_id")
    if type(operation_id) is not str or _IDENTIFIER.fullmatch(operation_id) is None:
        _raise(
            "invalid_coverage_binding",
            "/coverage_binding/operation_id",
            "Coverage operation identity is invalid.",
        )
    expected = {
        "revision": plan["revision"],
        "content_sha256": coverage_plan_content_sha256(plan),
        "evidence_manifest_sha256": coverage_canonical_sha256(plan["evidence_manifest"]),
        "operation_id": operation_id,
    }
    if binding != expected:
        _raise(
            "coverage_binding_mismatch",
            "/coverage_binding",
            "Coverage binding does not exactly identify the supplied current revision.",
        )
    return deepcopy(expected)


def _normalize_resolved_sources(
    value: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    if type(value) not in {list, tuple}:
        _raise(
            "invalid_source_bindings",
            "/source_bindings",
            "Resolved source bindings must be an array.",
        )
    normalized: dict[str, dict[str, object]] = {}
    source_identities: dict[str, tuple[str, str]] = {}
    for index, raw in enumerate(value):
        path = f"/source_bindings/{index}"
        if type(raw) is not dict:
            _raise(
                "invalid_source_binding",
                path,
                "Resolved source binding fields are not closed.",
            )
        evidence_ref = raw.get("evidence_ref")
        asset_id = raw.get("asset_id")
        asset_sha256 = raw.get("asset_sha256")
        asset_source_id = raw.get("asset_source_id")
        asset_reference_match = _ASSET_EVIDENCE_REF.fullmatch(evidence_ref) if type(evidence_ref) is str else None
        span_reference_match = _SPAN_EVIDENCE_REF.fullmatch(evidence_ref) if type(evidence_ref) is str else None
        residual_reference_match = (
            _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
            if type(evidence_ref) is str
            else None
        )
        if (
            asset_reference_match is None
            and span_reference_match is None
            and residual_reference_match is None
        ):
            _raise(
                "unsupported_evidence",
                f"{path}/evidence_ref",
                "B2B accepts only opaque asset or verified span evidence references.",
            )
        expected_fields = (
            _RESOLVED_SOURCE_IMAGE_FIELDS
            if asset_reference_match is not None
            else _RESOLVED_SOURCE_RESIDUAL_FIELDS
            if residual_reference_match is not None
            else _RESOLVED_SOURCE_COMMON_FIELDS
        )
        if set(raw) != expected_fields:
            _raise(
                "invalid_source_binding",
                path,
                "Resolved source binding fields are not closed for its evidence kind.",
            )
        if (
            type(asset_id) is not str
            or _ASSET_ID.fullmatch(asset_id) is None
            or (asset_reference_match is not None and asset_id != asset_reference_match.group("asset_id"))
        ):
            _raise(
                "source_asset_mismatch",
                f"{path}/asset_id",
                "Resolved source asset identity does not match its evidence reference.",
            )
        if (
            type(asset_sha256) is not str
            or _SHA256.fullmatch(asset_sha256) is None
            or not _content_address_matches(asset_id, asset_sha256)
        ):
            _raise(
                "source_asset_mismatch",
                f"{path}/asset_sha256",
                "Resolved source content digest does not match its asset identity.",
            )
        if type(asset_source_id) is not str or _ASSET_SOURCE_ID.fullmatch(asset_source_id) is None:
            _raise(
                "invalid_asset_source_id",
                f"{path}/asset_source_id",
                "Asset source identity must be an opaque Core source identifier.",
            )
        if asset_reference_match is not None:
            analysis_run_id = raw.get("analysis_run_id")
            analysis_revision = raw.get("analysis_revision")
            analysis_content_sha256 = raw.get("analysis_content_sha256")
            source_binding_sha256 = raw.get("source_binding_sha256")
            if (
                type(analysis_run_id) is not str
                or _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None
                or type(analysis_revision) is not int
                or analysis_revision < 1
                or analysis_revision > 1_800_000
                or type(analysis_content_sha256) is not str
                or _SHA256.fullmatch(analysis_content_sha256) is None
                or type(source_binding_sha256) is not str
                or _SHA256.fullmatch(source_binding_sha256) is None
            ):
                _raise(
                    "invalid_source_binding",
                    path,
                    "Resolved image source must carry one exact canonical analysis and source binding.",
                )
        elif residual_reference_match is not None:
            try:
                residual_binding = require_valid_residual_binding(
                    raw.get("residual_binding")  # type: ignore[arg-type]
                )
            except (ResidualIdentityContractError, TypeError) as exc:
                raise TimelineLoweringContractError(
                    [
                        {
                            "code": "invalid_residual_binding",
                            "path": f"{path}/residual_binding",
                            "message": "Resolved residual source proof is invalid.",
                        }
                    ]
                ) from exc
            if (
                residual_binding["residual_id"]
                != residual_reference_match.group("residual_id")
                or residual_binding["asset_id"] != asset_id
                or residual_binding["asset_sha256"] != asset_sha256
                or residual_binding["asset_source_id"] != asset_source_id
            ):
                _raise(
                    "source_proof_mismatch",
                    path,
                    "Resolved source does not match the exact residual proof.",
                )
        if evidence_ref in normalized:
            _raise(
                "duplicate_source_binding",
                f"{path}/evidence_ref",
                "Each evidence reference must have exactly one resolved source.",
            )
        source_identity = (asset_id, asset_sha256)
        existing_identity = source_identities.get(asset_source_id)
        if existing_identity is not None and existing_identity != source_identity:
            _raise(
                "source_asset_mismatch",
                f"{path}/asset_source_id",
                "One source identity cannot resolve different asset content.",
            )
        source_identities.setdefault(asset_source_id, source_identity)
        normalized_row: dict[str, object] = {
            "evidence_ref": evidence_ref,
            "asset_id": asset_id,
            "asset_sha256": asset_sha256,
            "asset_source_id": asset_source_id,
        }
        if asset_reference_match is not None:
            for field in _IMAGE_ANALYSIS_FIELDS:
                normalized_row[field] = raw[field]
        elif residual_reference_match is not None:
            normalized_row["residual_binding"] = residual_binding
        normalized[evidence_ref] = normalized_row
    return normalized


def _compile_clips(
    *,
    timeline_id: str,
    plan: Mapping[str, object],
    resolved_sources: Mapping[str, Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    manifest_by_ref = {
        str(item["evidence_ref"]): item for item in plan["evidence_manifest"] if isinstance(item, Mapping)
    }
    clips: list[dict[str, object]] = []
    canonical_sources: list[dict[str, object]] = []
    selected_refs: set[str] = set()
    for index, beat in enumerate(plan["beats"]):
        if not isinstance(beat, Mapping):
            _raise("invalid_coverage_plan", f"/coverage_plan/beats/{index}", "Coverage Beat is invalid.")
        selected = beat.get("selected_assignments")
        if beat.get("gap") is not None or type(selected) is not list or len(selected) != 1:
            _raise(
                "coverage_gap",
                f"/coverage_plan/beats/{index}",
                "B2B requires exactly one selected assignment and no Gap for every Beat.",
            )
        assignment = selected[0]
        timing = beat.get("timing")
        if not isinstance(assignment, Mapping) or not isinstance(timing, Mapping):
            _raise("invalid_coverage_plan", f"/coverage_plan/beats/{index}", "Coverage Beat is invalid.")
        evidence_ref = str(assignment["evidence_ref"])
        evidence = manifest_by_ref.get(evidence_ref)
        proof = evidence.get("proof") if isinstance(evidence, Mapping) else None
        if type(proof) is not dict or proof.get("kind") not in {
            "asset",
            "span",
            "residual_span",
        }:
            _raise(
                "unsupported_evidence",
                f"/coverage_plan/beats/{index}/selected_assignments/0/evidence_ref",
                "Selected evidence must be a verified image asset or video span proof.",
            )
        residual_binding: dict[str, object] | None = None
        if proof["kind"] == "asset":
            reference_match = _ASSET_EVIDENCE_REF.fullmatch(evidence_ref)
            valid_reference = reference_match is not None and proof.get("asset_id") == reference_match.group("asset_id")
        elif proof["kind"] == "span":
            reference_match = _SPAN_EVIDENCE_REF.fullmatch(evidence_ref)
            valid_reference = reference_match is not None and proof.get("span_id") == reference_match.group("span_id")
        else:
            reference_match = _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
            try:
                residual_binding = require_valid_residual_binding(
                    proof.get("residual_binding")  # type: ignore[arg-type]
                )
            except (ResidualIdentityContractError, TypeError) as exc:
                raise TimelineLoweringContractError(
                    [
                        {
                            "code": "invalid_residual_binding",
                            "path": f"/coverage_plan/evidence_manifest/{evidence_ref}",
                            "message": "Coverage residual proof is invalid.",
                        }
                    ]
                ) from exc
            valid_reference = (
                reference_match is not None
                and residual_binding["residual_id"]
                == reference_match.group("residual_id")
            )
        if not valid_reference:
            _raise(
                "unsupported_evidence",
                f"/coverage_plan/beats/{index}/selected_assignments/0/evidence_ref",
                "Evidence proof kind must match its opaque evidence reference.",
            )
        source = resolved_sources.get(evidence_ref)
        if source is None:
            _raise(
                "source_binding_missing",
                f"/source_bindings/{evidence_ref}",
                "Every selected clip requires one transaction-resolved source binding.",
            )
        asset_id = str(
            residual_binding["asset_id"]
            if residual_binding is not None
            else proof["asset_id"]
        )
        asset_sha256 = str(
            residual_binding["asset_sha256"]
            if residual_binding is not None
            else proof["asset_sha256"]
        )
        if source["asset_id"] != asset_id or source["asset_sha256"] != asset_sha256:
            _raise(
                "source_proof_mismatch",
                f"/source_bindings/{evidence_ref}",
                "Resolved source binding does not match the pinned Coverage proof.",
            )
        beat_id = str(beat["beat_id"])
        assignment_id = str(assignment["assignment_id"])
        duration_ms = int(timing["end_ms"]) - int(timing["start_ms"])
        if proof["kind"] == "asset":
            media_kind = "image"
            media_fields = {field: proof[field] for field in _IMAGE_ANALYSIS_FIELDS}
            if any(source.get(field) != proof.get(field) for field in _IMAGE_ANALYSIS_FIELDS):
                _raise(
                    "source_proof_mismatch",
                    f"/source_bindings/{evidence_ref}",
                    "Resolved image source does not match the exact canonical analysis proof.",
                )
            span_identity = tuple(
                proof[field]
                for field in (
                    "analysis_run_id",
                    "analysis_revision",
                    "analysis_content_sha256",
                    "source_binding_sha256",
                )
            )
        elif proof["kind"] == "span":
            source_in_ms = int(proof["start_ms"])
            source_out_ms = source_in_ms + duration_ms
            if source_out_ms > int(proof["end_ms"]):
                _raise(
                    "video_span_too_short",
                    f"/coverage_plan/beats/{index}/selected_assignments/0/evidence_ref",
                    "Selected video span is too short for its Timeline slot.",
                )
            media_kind = "video"
            span_identity = (
                proof["span_id"],
                proof["analysis_run_id"],
                proof["analysis_revision"],
                proof["input_asset_sha256"],
                source_in_ms,
                source_out_ms,
            )
            media_fields = {
                "span_id": proof["span_id"],
                "analysis_run_id": proof["analysis_run_id"],
                "analysis_revision": proof["analysis_revision"],
                "input_asset_sha256": proof["input_asset_sha256"],
                "source_in_ms": source_in_ms,
                "source_out_ms": source_out_ms,
            }
        else:
            assert residual_binding is not None
            if source.get("residual_binding") != residual_binding:
                _raise(
                    "source_proof_mismatch",
                    f"/source_bindings/{evidence_ref}",
                    "Resolved source does not match the exact residual proof.",
                )
            source_in_ms = int(residual_binding["source_in_ms"])
            source_out_ms = source_in_ms + duration_ms
            if source_out_ms > int(residual_binding["source_out_ms"]):
                _raise(
                    "video_span_too_short",
                    f"/coverage_plan/beats/{index}/selected_assignments/0/evidence_ref",
                    "Selected residual span is too short for its Timeline slot.",
                )
            media_kind = "video"
            residual_digest = residual_binding_sha256(residual_binding)
            span_identity = (
                residual_binding["parent_segment_id"],
                residual_binding["analysis_run_id"],
                residual_binding["analysis_revision"],
                residual_binding["input_asset_sha256"],
                source_in_ms,
                source_out_ms,
                residual_binding["residual_id"],
                residual_digest,
            )
            media_fields = {
                "span_id": residual_binding["parent_segment_id"],
                "analysis_run_id": residual_binding["analysis_run_id"],
                "analysis_revision": residual_binding["analysis_revision"],
                "input_asset_sha256": residual_binding["input_asset_sha256"],
                "source_in_ms": source_in_ms,
                "source_out_ms": source_out_ms,
                "residual_id": residual_binding["residual_id"],
                "parent_segment_id": residual_binding["parent_segment_id"],
                "residual_binding": residual_binding,
            }
        clip_id = _expected_clip_id(
            timeline_id,
            beat_id,
            assignment_id,
            evidence_ref,
            asset_id,
            asset_sha256,
            media_kind,
            span_identity,
        )
        clips.append(
            {
                "clip_id": clip_id,
                "ordinal": index,
                "beat_id": beat_id,
                "assignment_id": assignment_id,
                "evidence_ref": evidence_ref,
                "asset_id": asset_id,
                "asset_sha256": asset_sha256,
                "media_kind": media_kind,
                "start_ms": timing["start_ms"],
                "end_ms": timing["end_ms"],
                "fit": "cover",
                "audio_enabled": False,
                **media_fields,
            }
        )
        canonical_sources.append(
            {
                "clip_id": clip_id,
                "evidence_ref": evidence_ref,
                "asset_id": asset_id,
                "asset_sha256": asset_sha256,
                "asset_source_id": source["asset_source_id"],
                "coverage_proof_sha256": evidence["proof_sha256"],
                "media_kind": media_kind,
                **media_fields,
            }
        )
        selected_refs.add(evidence_ref)
    if set(resolved_sources) != selected_refs:
        _raise(
            "source_binding_set_mismatch",
            "/source_bindings",
            "Resolved source bindings must exactly match selected Coverage evidence.",
        )
    return clips, canonical_sources


def compile_canonical_timeline(
    *,
    project_id: str,
    revision: int,
    parent: Mapping[str, object] | None,
    blueprint: Mapping[str, object],
    blueprint_binding: Mapping[str, object],
    coverage_plan: Mapping[str, object],
    coverage_binding: Mapping[str, object],
    source_bindings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Compile one exact image/span first cut and its execution manifest.

    The caller must resolve all inputs in one current-head transaction.  This
    pure function then proves exact document bindings and refuses to guess,
    retrieve, fill gaps, re-plan selected evidence, or expose local paths.
    """

    if type(project_id) is not str or _IDENTIFIER.fullmatch(project_id) is None:
        _raise("invalid_project_id", "/project_id", "Project identity is invalid.")
    if type(revision) is not int or revision < 1 or revision > 1_800_000:
        _raise("invalid_revision", "/revision", "Timeline revision is invalid.")
    try:
        blueprint_document = require_valid_blueprint_document(blueprint)
        plan = require_current_coverage_plan(coverage_plan)
    except (BlueprintContractError, CoverageContractError) as exc:
        raise TimelineLoweringContractError(
            [
                {
                    "code": "invalid_pinned_input",
                    "path": "",
                    "message": "Pinned Blueprint or Coverage input is invalid.",
                }
            ]
        ) from exc
    if blueprint_document["project_id"] != project_id or plan["project_id"] != project_id:
        _raise(
            "project_binding_mismatch",
            "/project_id",
            "Blueprint, Coverage, and Timeline must belong to the same project.",
        )
    exact_blueprint = _exact_blueprint_binding(blueprint_document, blueprint_binding)
    if plan["blueprint_binding"] != {
        field: exact_blueprint[field] for field in ("revision", "content_sha256", "semantic_sha256")
    }:
        _raise(
            "coverage_blueprint_binding_mismatch",
            "/coverage_plan/blueprint_binding",
            "Coverage Plan is not pinned to the exact supplied Blueprint revision.",
        )
    try:
        require_exact_baseline_derivation(plan, blueprint=blueprint_document)
    except CoverageContractError as exc:
        # Keep diagnostics at the B2B boundary stable and non-reflective.
        raise TimelineLoweringContractError(
            [
                {
                    "code": "coverage_derivation_mismatch",
                    "path": "/coverage_plan",
                    "message": "Coverage Plan is not the exact baseline for its pinned Blueprint.",
                }
            ]
        ) from exc
    exact_coverage = _exact_coverage_binding(plan, coverage_binding)
    resolved_sources = _normalize_resolved_sources(source_bindings)
    timeline_id = _expected_timeline_id(project_id)
    clips, canonical_sources = _compile_clips(
        timeline_id=timeline_id,
        plan=plan,
        resolved_sources=resolved_sources,
    )
    output_binding = plan["output_binding"]
    aspect_ratio = output_binding["aspect_ratio"]
    if aspect_ratio not in _ASPECT_RATIOS:
        _raise(
            "unsupported_aspect_ratio",
            "/coverage_plan/output_binding/aspect_ratio",
            "B2B requires one explicit supported aspect ratio.",
        )
    parent_copy = None if parent is None else deepcopy(dict(parent))
    timeline: dict[str, object] = {
        "object": CANONICAL_TIMELINE_OBJECT,
        "schema_version": CANONICAL_TIMELINE_SCHEMA_VERSION,
        "timeline_id": timeline_id,
        "project_id": project_id,
        "revision": revision,
        "parent": parent_copy,
        "blueprint_binding": exact_blueprint,
        "coverage_binding": exact_coverage,
        "compiler": {
            "id": TIMELINE_LOWERER_COMPILER,
            "media": "image_and_video_span",
            "transitions": "hard_cut",
            "audio": "silent",
            "subtitles": "none",
        },
        "output": {
            "duration_ms": output_binding["duration_target_ms"],
            "aspect_ratio": aspect_ratio,
        },
        "tracks": [
            {
                "track_id": _expected_track_id(timeline_id),
                "kind": "primary_visual",
                "clips": clips,
            }
        ],
    }
    require_current_canonical_timeline(timeline)
    require_current_timeline_source_bindings(canonical_sources, timeline=timeline)
    return {"timeline": timeline, "source_bindings": canonical_sources}


def _validate_parent(root: Mapping[str, object], issues: _Issues) -> None:
    revision = root.get("revision")
    parent = root.get("parent")
    if parent is None:
        if revision != 1:
            issues.add("invalid_parent", "/parent", "Only revision one may omit a parent.")
        return
    row = _closed(
        parent,
        path="/parent",
        fields={"revision", "content_sha256"},
        issues=issues,
    )
    if row is None:
        return
    _positive_int(row.get("revision"), "/parent/revision", issues)
    _sha256(row.get("content_sha256"), "/parent/content_sha256", issues)
    if type(revision) is int and row.get("revision") != revision - 1:
        issues.add("invalid_parent", "/parent/revision", "Parent must be the preceding revision.")


def _validate_binding(
    value: object,
    *,
    path: str,
    fields: set[str],
    issues: _Issues,
) -> None:
    row = _closed(value, path=path, fields=fields, issues=issues)
    if row is None:
        return
    _positive_int(row.get("revision"), f"{path}/revision", issues)
    _sha256(row.get("content_sha256"), f"{path}/content_sha256", issues)
    if "semantic_sha256" in fields:
        _sha256(row.get("semantic_sha256"), f"{path}/semantic_sha256", issues)
    if "evidence_manifest_sha256" in fields:
        _sha256(
            row.get("evidence_manifest_sha256"),
            f"{path}/evidence_manifest_sha256",
            issues,
        )
    _identifier(row.get("operation_id"), f"{path}/operation_id", issues)


def _validate_residual_video_clip(
    row: Mapping[str, object],
    *,
    path: str,
    residual_id_from_ref: str | None,
    source_in: object,
    source_out: object,
    issues: _Issues,
) -> tuple[object, ...]:
    try:
        binding = require_valid_residual_binding(
            row.get("residual_binding")  # type: ignore[arg-type]
        )
    except (ResidualIdentityContractError, TypeError):
        issues.add(
            "invalid_residual_binding",
            f"{path}/residual_binding",
            "Residual Timeline proof is invalid.",
        )
        return ()
    expected_residual = {
        "residual_id": binding["residual_id"],
        "parent_segment_id": binding["parent_segment_id"],
        "span_id": binding["parent_segment_id"],
        "asset_id": binding["asset_id"],
        "asset_sha256": binding["asset_sha256"],
        "analysis_run_id": binding["analysis_run_id"],
        "analysis_revision": binding["analysis_revision"],
        "input_asset_sha256": binding["input_asset_sha256"],
    }
    if (
        binding["residual_id"] != residual_id_from_ref
        or any(row.get(field) != expected for field, expected in expected_residual.items())
        or type(source_in) is not int
        or type(source_out) is not int
        or source_in < int(binding["source_in_ms"])
        or source_out > int(binding["source_out_ms"])
    ):
        issues.add(
            "residual_proof_mismatch",
            path,
            "Timeline residual identity, parent, or read range does not match its proof.",
        )
    return (
        binding["parent_segment_id"],
        binding["analysis_run_id"],
        binding["analysis_revision"],
        binding["input_asset_sha256"],
        source_in,
        source_out,
        binding["residual_id"],
        residual_binding_sha256(binding),
    )


def _clip_shape(value: object) -> tuple[set[str], bool, bool]:
    common_fields = {
        "clip_id",
        "ordinal",
        "beat_id",
        "assignment_id",
        "evidence_ref",
        "asset_id",
        "asset_sha256",
        "media_kind",
        "start_ms",
        "end_ms",
        "fit",
        "audio_enabled",
    }
    video_fields = {
        "span_id",
        "analysis_run_id",
        "analysis_revision",
        "input_asset_sha256",
        "source_in_ms",
        "source_out_ms",
    }
    image_has_analysis = (
        isinstance(value, dict)
        and value.get("media_kind") == "image"
        and bool(set(value) & _IMAGE_ANALYSIS_FIELDS)
    )
    residual_video = (
        isinstance(value, dict)
        and value.get("media_kind") == "video"
        and bool(set(value) & _RESIDUAL_VIDEO_FIELDS)
    )
    if residual_video:
        return common_fields | video_fields | _RESIDUAL_VIDEO_FIELDS, True, False
    if isinstance(value, dict) and value.get("media_kind") == "video":
        return common_fields | video_fields, False, False
    if image_has_analysis:
        return common_fields | _IMAGE_ANALYSIS_FIELDS, False, True
    return common_fields, False, False


def _validate_clip(
    value: object,
    *,
    index: int,
    timeline_id: str | None,
    expected_start: int,
    identities: tuple[set[str], set[str], set[str]],
    allow_repeated_lineage: bool,
    issues: _Issues,
) -> int:
    path = f"/tracks/0/clips/{index}"
    fields, residual_video, image_has_analysis = _clip_shape(value)
    row = _closed(
        value,
        path=path,
        fields=fields,
        issues=issues,
    )
    if row is None:
        return expected_start
    clip_ids, assignment_ids, evidence_refs = identities
    identity_rules = [("clip_id", clip_ids, "duplicate_clip")]
    if not allow_repeated_lineage:
        identity_rules.extend(
            [
                ("assignment_id", assignment_ids, "duplicate_assignment"),
                ("evidence_ref", evidence_refs, "duplicate_evidence"),
            ]
        )
    for field, seen, code in identity_rules:
        value_id = row.get(field)
        if type(value_id) is str:
            if value_id in seen:
                issues.add(code, f"{path}/{field}", "Timeline identity must be unique.")
            seen.add(value_id)
    _identifier(row.get("clip_id"), f"{path}/clip_id", issues)
    _identifier(row.get("beat_id"), f"{path}/beat_id", issues)
    _identifier(row.get("assignment_id"), f"{path}/assignment_id", issues)
    if row.get("ordinal") != index:
        issues.add("invalid_clip_order", f"{path}/ordinal", "Clip ordinal must match canonical order.")
    evidence_ref = row.get("evidence_ref")
    asset_id = row.get("asset_id")
    asset_sha256 = row.get("asset_sha256")
    asset_reference_match = _ASSET_EVIDENCE_REF.fullmatch(evidence_ref) if type(evidence_ref) is str else None
    span_reference_match = _SPAN_EVIDENCE_REF.fullmatch(evidence_ref) if type(evidence_ref) is str else None
    residual_reference_match = (
        _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
        if type(evidence_ref) is str
        else None
    )
    media_kind = row.get("media_kind")
    reference_valid = (media_kind == "image" and asset_reference_match is not None) or (
        media_kind == "video"
        and (
            span_reference_match is not None
            or residual_reference_match is not None
        )
    )
    if not reference_valid:
        issues.add(
            "unsupported_evidence",
            f"{path}/evidence_ref",
            "Image clips require asset evidence and video clips require span evidence.",
        )
    if (
        type(asset_id) is not str
        or _ASSET_ID.fullmatch(asset_id) is None
        or (asset_reference_match is not None and asset_id != asset_reference_match.group("asset_id"))
    ):
        issues.add("asset_identity_mismatch", f"{path}/asset_id", "Asset identity is invalid.")
    if (
        type(asset_sha256) is not str
        or _SHA256.fullmatch(asset_sha256) is None
        or (type(asset_id) is str and not _content_address_matches(asset_id, asset_sha256))
    ):
        issues.add("asset_identity_mismatch", f"{path}/asset_sha256", "Asset digest is invalid.")
    start = row.get("start_ms")
    end = row.get("end_ms")
    if type(start) is not int or type(end) is not int or start != expected_start or end <= start:
        issues.add("invalid_clip_timing", path, "Clips must be positive, contiguous, and non-overlapping.")
        next_start = expected_start
    else:
        next_start = end
    span_identity: tuple[object, ...] = ()
    if media_kind not in {"image", "video"}:
        issues.add(
            "unsupported_media_kind",
            f"{path}/media_kind",
            "B2B supports image and bounded video-span clips only.",
        )
    elif media_kind == "image" and image_has_analysis:
        analysis_run_id = row.get("analysis_run_id")
        if type(analysis_run_id) is not str or _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None:
            issues.add("invalid_analysis_run", f"{path}/analysis_run_id", "Analysis run identity is invalid.")
        analysis_revision = row.get("analysis_revision")
        if (
            type(analysis_revision) is not int
            or analysis_revision < 1
            or analysis_revision > 1_800_000
        ):
            issues.add("invalid_analysis_revision", f"{path}/analysis_revision", "Analysis revision is invalid.")
        analysis_content_sha256 = row.get("analysis_content_sha256")
        if type(analysis_content_sha256) is not str or _SHA256.fullmatch(analysis_content_sha256) is None:
            issues.add(
                "invalid_analysis_content_sha256",
                f"{path}/analysis_content_sha256",
                "Analysis content digest is invalid.",
            )
        source_binding_sha256 = row.get("source_binding_sha256")
        if type(source_binding_sha256) is not str or _SHA256.fullmatch(source_binding_sha256) is None:
            issues.add(
                "invalid_source_binding_sha256",
                f"{path}/source_binding_sha256",
                "Source binding digest is invalid.",
            )
        span_identity = (
            analysis_run_id,
            analysis_revision,
            analysis_content_sha256,
            source_binding_sha256,
        )
    elif media_kind == "video":
        span_id = row.get("span_id")
        span_match = _SPAN_ID.fullmatch(span_id) if type(span_id) is str else None
        analysis_run_id = row.get("analysis_run_id")
        if type(analysis_run_id) is not str or _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None:
            issues.add("invalid_analysis_run", f"{path}/analysis_run_id", "Analysis run identity is invalid.")
        analysis_revision = row.get("analysis_revision")
        if (
            type(analysis_revision) is not int
            or analysis_revision < 1
            or analysis_revision > 1_800_000
            or (span_match is not None and int(span_match.group("analysis_revision")) != analysis_revision)
        ):
            issues.add("invalid_analysis_revision", f"{path}/analysis_revision", "Analysis revision is invalid.")
        input_digest = row.get("input_asset_sha256")
        if type(input_digest) is not str or _SHA256.fullmatch(input_digest) is None or input_digest != asset_sha256:
            issues.add(
                "input_asset_mismatch",
                f"{path}/input_asset_sha256",
                "Analysis input digest must match the video asset digest.",
            )
        source_in = row.get("source_in_ms")
        source_out = row.get("source_out_ms")
        if (
            type(source_in) is not int
            or source_in < 0
            or type(source_out) is not int
            or source_out <= source_in
            or type(start) is not int
            or type(end) is not int
            or source_out - source_in != end - start
        ):
            issues.add(
                "invalid_source_range",
                f"{path}/source_in_ms",
                "Video source range must exactly match the Timeline slot duration.",
            )
        if residual_video:
            span_identity = _validate_residual_video_clip(
                row,
                path=path,
                residual_id_from_ref=(
                    residual_reference_match.group("residual_id")
                    if residual_reference_match is not None
                    else None
                ),
                source_in=source_in,
                source_out=source_out,
                issues=issues,
            )
        else:
            if (
                span_match is None
                or span_reference_match is None
                or span_id != span_reference_match.group("span_id")
                or type(asset_id) is not str
                or span_match.group("asset_digest") != asset_id.removeprefix("asset_")
            ):
                issues.add("span_identity_mismatch", f"{path}/span_id", "Video span identity is invalid.")
            span_identity = (
                span_id,
                analysis_run_id,
                analysis_revision,
                input_digest,
                source_in,
                source_out,
            )
    if row.get("fit") != "cover":
        issues.add("unsupported_fit", f"{path}/fit", "B2B uses deterministic cover fitting only.")
    if row.get("audio_enabled") is not False:
        issues.add("audio_not_silent", f"{path}/audio_enabled", "B2B Timeline must be silent.")
    if (
        timeline_id is not None
        and media_kind in {"image", "video"}
        and all(
            type(row.get(field)) is str
            for field in ("beat_id", "assignment_id", "evidence_ref", "asset_id", "asset_sha256")
        )
    ):
        expected_clip_id = _expected_clip_id(
            timeline_id,
            str(row["beat_id"]),
            str(row["assignment_id"]),
            str(row["evidence_ref"]),
            str(row["asset_id"]),
            str(row["asset_sha256"]),
            str(media_kind),
            span_identity,
        )
        if row.get("clip_id") != expected_clip_id:
            issues.add("unstable_clip_id", f"{path}/clip_id", "Clip identity is not deterministic.")
    return next_start


def _validate_document_size(value: object, issues: _Issues) -> None:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        issues.add("invalid_json", "", "Timeline is not canonical JSON.")
        return
    if len(encoded) > TIMELINE_MAX_CANONICAL_BYTES:
        issues.add("document_too_large", "", "Timeline exceeds the size limit.")


def validate_canonical_timeline(value: object) -> list[dict[str, str]]:
    """Validate one closed canonical Timeline without repairing it."""

    issues = _Issues()
    root = _closed(
        value,
        path="",
        fields={
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
        },
        issues=issues,
    )
    if root is None:
        return issues.items
    if root.get("object") != CANONICAL_TIMELINE_OBJECT:
        issues.add("invalid_object", "/object", "Canonical Timeline object is invalid.")
    schema_version = root.get("schema_version")
    if schema_version not in CANONICAL_TIMELINE_SCHEMA_VERSIONS:
        issues.add("unsupported_schema_version", "/schema_version", "Timeline schema is unsupported.")
    project_valid = _identifier(root.get("project_id"), "/project_id", issues)
    timeline_valid = _identifier(root.get("timeline_id"), "/timeline_id", issues)
    if project_valid and timeline_valid and root["timeline_id"] != _expected_timeline_id(str(root["project_id"])):
        issues.add("unstable_timeline_id", "/timeline_id", "Timeline identity is not deterministic.")
    _positive_int(root.get("revision"), "/revision", issues)
    _validate_parent(root, issues)
    _validate_binding(
        root.get("blueprint_binding"),
        path="/blueprint_binding",
        fields=_BLUEPRINT_BINDING_FIELDS,
        issues=issues,
    )
    _validate_binding(
        root.get("coverage_binding"),
        path="/coverage_binding",
        fields=_COVERAGE_BINDING_FIELDS,
        issues=issues,
    )
    compiler = _closed(
        root.get("compiler"),
        path="/compiler",
        fields={"id", "media", "transitions", "audio", "subtitles"},
        issues=issues,
    )
    expected_compiler = {
        "id": TIMELINE_LOWERER_COMPILER,
        "media": "image_and_video_span",
        "transitions": "hard_cut",
        "audio": "silent",
        "subtitles": "none",
    }
    if compiler is not None and compiler != expected_compiler:
        issues.add("invalid_compiler", "/compiler", "Timeline compiler capability claim is invalid.")
    output = _closed(
        root.get("output"),
        path="/output",
        fields={"duration_ms", "aspect_ratio"},
        issues=issues,
    )
    duration_ms: int | None = None
    if output is not None:
        if _positive_int(output.get("duration_ms"), "/output/duration_ms", issues):
            duration_ms = int(output["duration_ms"])
        if output.get("aspect_ratio") not in _ASPECT_RATIOS:
            issues.add("unsupported_aspect_ratio", "/output/aspect_ratio", "Aspect ratio is unsupported.")
    tracks = root.get("tracks")
    final_end = 0
    if type(tracks) is not list or len(tracks) != 1:
        issues.add("invalid_tracks", "/tracks", "Timeline requires one primary visual track.")
    else:
        track = _closed(
            tracks[0],
            path="/tracks/0",
            fields={"track_id", "kind", "clips"},
            issues=issues,
        )
        if track is not None:
            track_id = track.get("track_id")
            _identifier(track_id, "/tracks/0/track_id", issues)
            if type(root.get("timeline_id")) is str and track_id != _expected_track_id(str(root["timeline_id"])):
                issues.add("unstable_track_id", "/tracks/0/track_id", "Track identity is not deterministic.")
            if track.get("kind") != "primary_visual":
                issues.add("invalid_track_kind", "/tracks/0/kind", "Track must be the primary visual track.")
            clips = track.get("clips")
            if type(clips) is not list or not clips or len(clips) > TIMELINE_MAX_CLIPS:
                issues.add("invalid_clips", "/tracks/0/clips", "Timeline requires one to 256 clips.")
            else:
                identities: tuple[set[str], set[str], set[str]] = (set(), set(), set())
                timeline_id = str(root["timeline_id"]) if timeline_valid else None
                for index, clip in enumerate(clips):
                    final_end = _validate_clip(
                        clip,
                        index=index,
                        timeline_id=timeline_id,
                        expected_start=final_end,
                        identities=identities,
                        allow_repeated_lineage=(
                            schema_version == CANONICAL_TIMELINE_STRUCTURAL_SCHEMA_VERSION
                        ),
                        issues=issues,
                    )
    if duration_ms is not None and final_end != duration_ms:
        issues.add("duration_mismatch", "/tracks/0/clips", "Clips must cover the declared duration exactly.")
    _validate_document_size(value, issues)
    return issues.items[:64]


def require_valid_canonical_timeline(value: object) -> dict[str, Any]:
    """Parse a structurally valid current or historical Timeline document."""

    errors = validate_canonical_timeline(value)
    if errors:
        raise TimelineLoweringContractError(errors)
    assert isinstance(value, dict)
    return value


def _legacy_image_clip_path(value: Mapping[str, object]) -> str | None:
    tracks = value.get("tracks")
    if type(tracks) is not list or not tracks or not isinstance(tracks[0], Mapping):
        return None
    clips = tracks[0].get("clips")
    if type(clips) is not list:
        return None
    for index, clip in enumerate(clips):
        if (
            isinstance(clip, Mapping)
            and clip.get("media_kind") == "image"
            and not (set(clip) & _IMAGE_ANALYSIS_FIELDS)
        ):
            return f"/tracks/0/clips/{index}"
    return None


def inspect_canonical_timeline_for_read_only(value: object) -> dict[str, object]:
    """Parse Timeline history without promoting identity-only image evidence."""

    document = require_valid_canonical_timeline(value)
    legacy_path = _legacy_image_clip_path(document)
    return {
        "document": document,
        "evidence_currency": "legacy_non_current" if legacy_path else "current",
        "eligible_for_current_use": legacy_path is None,
        "reason_code": "legacy_image_identity_only" if legacy_path else None,
        "reason_path": legacy_path,
    }


def require_current_canonical_timeline(value: object) -> dict[str, Any]:
    """Require exact canonical image analysis evidence for mutable consumers."""

    document = require_valid_canonical_timeline(value)
    legacy_path = _legacy_image_clip_path(document)
    if legacy_path is not None:
        _raise(
            "legacy_image_evidence_non_current",
            legacy_path,
            "Identity-only image evidence is historical and cannot be edited, restored, or saved.",
        )
    return document


def canonical_timeline_content_sha256(value: Mapping[str, object]) -> str:
    require_valid_canonical_timeline(value)
    return canonical_sha256(value)


def validate_timeline_source_bindings(
    value: object,
    *,
    timeline: Mapping[str, object],
) -> list[dict[str, str]]:
    """Validate the closed, execution-local source manifest for a Timeline."""

    issues = _Issues()
    timeline_errors = validate_canonical_timeline(timeline)
    if timeline_errors:
        issues.add("invalid_canonical_timeline", "/timeline", "Source bindings require a valid Timeline.")
        return issues.items
    clips = timeline["tracks"][0]["clips"]
    if type(value) is not list or len(value) != len(clips):
        issues.add(
            "source_binding_set_mismatch",
            "/source_bindings",
            "Source manifest must contain exactly one entry per Timeline clip.",
        )
        return issues.items
    source_identities: dict[str, tuple[object, object]] = {}
    for index, (raw, clip) in enumerate(zip(value, clips, strict=True)):
        path = f"/source_bindings/{index}"
        residual_video = (
            clip.get("media_kind") == "video"
            and bool(set(clip) & _RESIDUAL_VIDEO_FIELDS)
        )
        if residual_video:
            fields = _CANONICAL_SOURCE_RESIDUAL_FIELDS
        elif clip.get("media_kind") == "video":
            fields = _CANONICAL_SOURCE_VIDEO_FIELDS
        elif clip.get("media_kind") == "image" and bool(set(clip) & _IMAGE_ANALYSIS_FIELDS):
            fields = _CANONICAL_SOURCE_IMAGE_FIELDS
        else:
            fields = _CANONICAL_SOURCE_COMMON_FIELDS
        row = _closed(raw, path=path, fields=fields, issues=issues)
        if row is None:
            continue
        comparison_fields = [
            "clip_id",
            "evidence_ref",
            "asset_id",
            "asset_sha256",
            "media_kind",
        ]
        if clip.get("media_kind") == "video":
            comparison_fields.extend(
                [
                    "span_id",
                    "analysis_run_id",
                    "analysis_revision",
                    "input_asset_sha256",
                    "source_in_ms",
                    "source_out_ms",
                ]
            )
            if residual_video:
                comparison_fields.extend(
                    [
                        "residual_id",
                        "parent_segment_id",
                        "residual_binding",
                    ]
                )
        elif clip.get("media_kind") == "image" and bool(set(clip) & _IMAGE_ANALYSIS_FIELDS):
            comparison_fields.extend(
                [
                    "analysis_run_id",
                    "analysis_revision",
                    "analysis_content_sha256",
                    "source_binding_sha256",
                ]
            )
        for field in comparison_fields:
            if row.get(field) != clip.get(field):
                issues.add(
                    "source_binding_clip_mismatch",
                    f"{path}/{field}",
                    "Source binding does not exactly identify its Timeline clip.",
                )
        source_id = row.get("asset_source_id")
        if type(source_id) is not str or _ASSET_SOURCE_ID.fullmatch(source_id) is None:
            issues.add("invalid_asset_source_id", f"{path}/asset_source_id", "Source identity is invalid.")
        else:
            source_identity = (row.get("asset_id"), row.get("asset_sha256"))
            existing_identity = source_identities.get(source_id)
            if existing_identity is not None and existing_identity != source_identity:
                issues.add(
                    "source_asset_mismatch",
                    f"{path}/asset_source_id",
                    "One source identity cannot identify different asset content.",
                )
            source_identities.setdefault(source_id, source_identity)
        _sha256(row.get("coverage_proof_sha256"), f"{path}/coverage_proof_sha256", issues)
    return issues.items[:64]


def require_valid_timeline_source_bindings(
    value: object,
    *,
    timeline: Mapping[str, object],
) -> list[dict[str, Any]]:
    errors = validate_timeline_source_bindings(value, timeline=timeline)
    if errors:
        raise TimelineLoweringContractError(errors)
    assert isinstance(value, list)
    return value


def inspect_timeline_source_bindings_for_read_only(
    value: object,
    *,
    timeline: Mapping[str, object],
) -> dict[str, object]:
    """Parse the historical source manifest and expose its Timeline currency."""

    document = require_valid_canonical_timeline(timeline)
    bindings = require_valid_timeline_source_bindings(value, timeline=document)
    legacy_path = _legacy_image_clip_path(document)
    return {
        "source_bindings": bindings,
        "evidence_currency": "legacy_non_current" if legacy_path else "current",
        "eligible_for_current_use": legacy_path is None,
        "reason_code": "legacy_image_identity_only" if legacy_path else None,
        "reason_path": legacy_path,
    }


def require_current_timeline_source_bindings(
    value: object,
    *,
    timeline: Mapping[str, object],
) -> list[dict[str, Any]]:
    """Require a source manifest for one current-evidence Timeline."""

    document = require_current_canonical_timeline(timeline)
    return require_valid_timeline_source_bindings(value, timeline=document)


def timeline_source_bindings_sha256(
    value: object,
    *,
    timeline: Mapping[str, object],
) -> str:
    manifest = require_valid_timeline_source_bindings(value, timeline=timeline)
    return canonical_sha256(manifest)


def require_exact_canonical_timeline_derivation(
    timeline: object,
    *,
    source_manifest: object,
    blueprint: Mapping[str, object],
    blueprint_binding: Mapping[str, object],
    coverage_plan: Mapping[str, object],
    coverage_binding: Mapping[str, object],
) -> dict[str, Any]:
    """Cold-audit a persisted Timeline and source manifest by full replay."""

    persisted = require_current_canonical_timeline(timeline)
    persisted_sources = require_current_timeline_source_bindings(
        source_manifest,
        timeline=persisted,
    )
    resolver_rows = []
    for row in persisted_sources:
        fields = (
            _RESOLVED_SOURCE_IMAGE_FIELDS
            if row.get("media_kind") == "image"
            else _RESOLVED_SOURCE_RESIDUAL_FIELDS
            if row.get("residual_binding") is not None
            else _RESOLVED_SOURCE_COMMON_FIELDS
        )
        resolver_rows.append({field: row[field] for field in fields})
    try:
        replay = compile_canonical_timeline(
            project_id=str(persisted["project_id"]),
            revision=int(persisted["revision"]),
            parent=persisted["parent"] if isinstance(persisted["parent"], Mapping) else None,
            blueprint=blueprint,
            blueprint_binding=blueprint_binding,
            coverage_plan=coverage_plan,
            coverage_binding=coverage_binding,
            source_bindings=resolver_rows,
        )
    except (TimelineLoweringContractError, ValueError, TypeError) as exc:
        raise TimelineLoweringContractError(
            [
                {
                    "code": "timeline_derivation_mismatch",
                    "path": "",
                    "message": "Persisted Timeline inputs cannot reproduce the exact first cut.",
                }
            ]
        ) from exc
    if replay["timeline"] != persisted or replay["source_bindings"] != persisted_sources:
        _raise(
            "timeline_derivation_mismatch",
            "",
            "Persisted Timeline is not the exact first cut for its pinned inputs.",
        )
    return persisted


__all__ = [
    "CANONICAL_TIMELINE_OBJECT",
    "CANONICAL_TIMELINE_SCHEMA_VERSION",
    "CANONICAL_TIMELINE_SCHEMA_VERSIONS",
    "CANONICAL_TIMELINE_STRUCTURAL_SCHEMA_VERSION",
    "TIMELINE_LOWERER_COMPILER",
    "TimelineLoweringContractError",
    "canonical_json",
    "canonical_sha256",
    "canonical_timeline_clip_id",
    "canonical_timeline_content_sha256",
    "compile_canonical_timeline",
    "inspect_canonical_timeline_for_read_only",
    "inspect_timeline_source_bindings_for_read_only",
    "require_current_canonical_timeline",
    "require_current_timeline_source_bindings",
    "require_exact_canonical_timeline_derivation",
    "require_valid_canonical_timeline",
    "require_valid_timeline_source_bindings",
    "timeline_source_bindings_sha256",
    "validate_canonical_timeline",
    "validate_timeline_source_bindings",
]
