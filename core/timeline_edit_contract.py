"""Closed deterministic v1 edits for a canonical Timeline revision.

This module edits an already-canonical Timeline; it is not another Coverage
lowerer and its output must not be claimed as the exact baseline first cut.
The existing compiler object remains the closed media/cut/audio capability
envelope.  A persistence layer is responsible for recording edit provenance
in its command ledger.

All functions are pure.  They accept an exact parent Timeline, its exact
execution-local source manifest, the Timeline-pinned Coverage Plan, and one
closed edit.  No path, lookup, clock, database handle, or hidden source
selection enters the contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from .coverage_contract import (
    CoverageContractError,
    canonical_sha256 as coverage_canonical_sha256,
    coverage_plan_content_sha256,
    require_current_coverage_plan,
)
from .timeline_lowering_contract import (
    TimelineLoweringContractError,
    canonical_timeline_clip_id,
    canonical_timeline_content_sha256,
    require_current_canonical_timeline,
    require_current_timeline_source_bindings,
)


TIMELINE_EDIT_MIN_TRIM_MS = 100
TIMELINE_EDIT_MAX_DURATION_MS = 1_800_000
TIMELINE_EDIT_MAX_REVISION = 1_800_000
TIMELINE_EDIT_MAX_SOURCE_MS = 9_007_199_254_740_991

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^(?:asset|img)_[0-9a-f]{24}$")
_ASSET_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_ASSET_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/asset/(?P<asset_id>(?:asset|img)_[0-9a-f]{24})$"
)
_SPAN_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<span_id>seg_[0-9a-f]{24}_"
    r"[1-9][0-9]{0,6}_(?:0|[1-9][0-9]{0,6}))$"
)

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
_EDIT_FIELDS = {
    "move_clip": {"op", "clip_id", "to_index"},
    "trim_clip": {"op", "clip_id", "source_in_ms", "source_out_ms"},
    "set_clip_duration": {"op", "clip_id", "duration_ms"},
    "replace_clip": {"op", "clip_id", "assignment_id"},
}


@dataclass(frozen=True)
class TimelineEditContractError(ValueError):
    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]):
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_timeline_edit"),
                "path": str(item.get("path") or ""),
                "message": str(item.get("message") or "Canonical Timeline edit is invalid."),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"] if normalized else "Canonical Timeline edit is invalid.",
        )


def _raise(code: str, path: str, message: str) -> None:
    raise TimelineEditContractError([{"code": code, "path": path, "message": message}])


def _raise_from_lowering(exc: TimelineLoweringContractError, *, code: str, path: str) -> None:
    message = exc.errors[0]["message"] if exc.errors else "Canonical Timeline input is invalid."
    _raise(code, path, message)


def _content_address_matches(asset_id: str, asset_sha256: str) -> bool:
    namespace = asset_id.split("_", 1)[0]
    return asset_id == f"{namespace}_{asset_sha256[:24]}"


def _bounded_int(
    value: object,
    *,
    path: str,
    minimum: int,
    maximum: int = TIMELINE_EDIT_MAX_DURATION_MS,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _raise(
            "invalid_edit_integer",
            path,
            f"Value must be an integer from {minimum} through {maximum}.",
        )
    return value


def _closed_edit(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _raise("invalid_edit", "/edit", "Edit must be one closed object.")
    op = value.get("op")
    if type(op) is not str or op not in _EDIT_FIELDS:
        _raise("unsupported_edit", "/edit/op", "Timeline edit operation is unsupported.")
    if set(value) != _EDIT_FIELDS[op]:
        _raise("invalid_edit", "/edit", "Edit fields are not closed for this operation.")
    clip_id = value.get("clip_id")
    if type(clip_id) is not str or _IDENTIFIER.fullmatch(clip_id) is None:
        _raise("invalid_clip_id", "/edit/clip_id", "Edit clip identity is invalid.")
    if op == "replace_clip":
        assignment_id = value.get("assignment_id")
        if type(assignment_id) is not str or _IDENTIFIER.fullmatch(assignment_id) is None:
            _raise(
                "invalid_replacement_candidate",
                "/edit/assignment_id",
                "Replacement assignment identity is invalid.",
            )
    return value


def _closed_replacement_source(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _raise(
            "invalid_replacement_source",
            "/replacement_source",
            "Replacement source fields are not closed.",
        )
    evidence_ref = value.get("evidence_ref")
    asset_id = value.get("asset_id")
    asset_sha256 = value.get("asset_sha256")
    asset_source_id = value.get("asset_source_id")
    asset_match = (
        _ASSET_EVIDENCE_REF.fullmatch(evidence_ref) if type(evidence_ref) is str else None
    )
    span_match = (
        _SPAN_EVIDENCE_REF.fullmatch(evidence_ref) if type(evidence_ref) is str else None
    )
    if asset_match is None and span_match is None:
        _raise(
            "invalid_replacement_source",
            "/replacement_source/evidence_ref",
            "Replacement evidence reference is invalid.",
        )
    expected_fields = (
        _RESOLVED_SOURCE_IMAGE_FIELDS
        if asset_match is not None
        else _RESOLVED_SOURCE_COMMON_FIELDS
    )
    if set(value) != expected_fields:
        _raise(
            "invalid_replacement_source",
            "/replacement_source",
            "Replacement source fields are not closed for its evidence kind.",
        )
    if (
        type(asset_id) is not str
        or _ASSET_ID.fullmatch(asset_id) is None
        or (asset_match is not None and asset_id != asset_match.group("asset_id"))
    ):
        _raise(
            "replacement_source_mismatch",
            "/replacement_source/asset_id",
            "Replacement source asset identity is invalid.",
        )
    if (
        type(asset_sha256) is not str
        or _SHA256.fullmatch(asset_sha256) is None
        or not _content_address_matches(asset_id, asset_sha256)
    ):
        _raise(
            "replacement_source_mismatch",
            "/replacement_source/asset_sha256",
            "Replacement source asset digest is invalid.",
        )
    if type(asset_source_id) is not str or _ASSET_SOURCE_ID.fullmatch(asset_source_id) is None:
        _raise(
            "invalid_replacement_source",
            "/replacement_source/asset_source_id",
            "Replacement source must use an opaque Core source identity.",
        )
    if asset_match is not None:
        analysis_run_id = value.get("analysis_run_id")
        analysis_revision = value.get("analysis_revision")
        analysis_content_sha256 = value.get("analysis_content_sha256")
        source_binding_sha256 = value.get("source_binding_sha256")
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
                "invalid_replacement_source",
                "/replacement_source",
                "Replacement image source must carry exact canonical analysis evidence.",
            )
    return deepcopy(value)


def _coverage_indexes(
    plan: Mapping[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    beats: dict[str, dict[str, object]] = {}
    for raw in plan["beats"]:
        assert isinstance(raw, dict)
        beats[str(raw["beat_id"])] = raw
    manifest: dict[str, dict[str, object]] = {}
    for raw in plan["evidence_manifest"]:
        assert isinstance(raw, dict)
        manifest[str(raw["evidence_ref"])] = raw
    return beats, manifest


def _assignment_for_clip(
    beat: Mapping[str, object],
    *,
    assignment_id: object,
) -> Mapping[str, object] | None:
    selected = beat["selected_assignments"]
    alternatives = beat["alternatives"]
    assert isinstance(selected, list) and isinstance(alternatives, list)
    for raw in [*selected, *alternatives]:
        if isinstance(raw, Mapping) and raw.get("assignment_id") == assignment_id:
            return raw
    return None


def _require_clip_proof_match(
    clip: Mapping[str, object],
    source: Mapping[str, object],
    evidence: Mapping[str, object],
    *,
    path: str,
) -> None:
    proof = evidence.get("proof")
    if evidence.get("status") != "verified" or type(proof) is not dict:
        _raise(
            "unverified_clip_evidence",
            f"{path}/evidence_ref",
            "Every Timeline clip must retain a verified Coverage proof.",
        )
    if source.get("coverage_proof_sha256") != evidence.get("proof_sha256"):
        _raise(
            "coverage_proof_mismatch",
            f"{path}/evidence_ref",
            "Source binding does not carry the exact pinned Coverage proof digest.",
        )
    if clip.get("asset_id") != proof.get("asset_id") or clip.get("asset_sha256") != proof.get(
        "asset_sha256"
    ):
        _raise(
            "coverage_proof_mismatch",
            f"{path}/asset_id",
            "Timeline clip asset does not match its pinned Coverage proof.",
        )
    if proof.get("kind") == "asset":
        if clip.get("media_kind") != "image":
            _raise(
                "coverage_proof_mismatch",
                f"{path}/media_kind",
                "Asset proof requires an image Timeline clip.",
            )
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            if clip.get(field) != proof.get(field) or source.get(field) != proof.get(field):
                _raise(
                    "coverage_proof_mismatch",
                    f"{path}/{field}",
                    "Image clip and source do not match the exact pinned canonical analysis proof.",
                )
        return
    if proof.get("kind") != "span" or clip.get("media_kind") != "video":
        _raise(
            "coverage_proof_mismatch",
            f"{path}/media_kind",
            "Span proof requires a video Timeline clip.",
        )
    for field in (
        "span_id",
        "analysis_run_id",
        "analysis_revision",
        "input_asset_sha256",
    ):
        if clip.get(field) != proof.get(field):
            _raise(
                "coverage_proof_mismatch",
                f"{path}/{field}",
                "Video clip lineage does not match its pinned Coverage proof.",
            )
    source_in = clip.get("source_in_ms")
    source_out = clip.get("source_out_ms")
    if (
        type(source_in) is not int
        or type(source_out) is not int
        or source_in < proof["start_ms"]
        or source_out > proof["end_ms"]
    ):
        _raise(
            "coverage_proof_bounds_exceeded",
            f"{path}/source_in_ms",
            "Video clip range exceeds its pinned Coverage proof bounds.",
        )


def _require_exact_timeline_coverage(
    timeline: Mapping[str, object],
    source_bindings: Sequence[Mapping[str, object]],
    coverage_plan: Mapping[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    if timeline["project_id"] != coverage_plan["project_id"]:
        _raise(
            "coverage_binding_mismatch",
            "/coverage_plan/project_id",
            "Timeline and Coverage Plan belong to different projects.",
        )
    coverage_binding = timeline["coverage_binding"]
    assert isinstance(coverage_binding, Mapping)
    expected_coverage_binding = {
        "revision": coverage_plan["revision"],
        "content_sha256": coverage_plan_content_sha256(coverage_plan),
        "evidence_manifest_sha256": coverage_canonical_sha256(
            coverage_plan["evidence_manifest"]
        ),
        "operation_id": coverage_binding["operation_id"],
    }
    if coverage_binding != expected_coverage_binding:
        _raise(
            "coverage_binding_mismatch",
            "/coverage_binding",
            "Timeline does not identify the exact supplied Coverage revision.",
        )
    blueprint_binding = timeline["blueprint_binding"]
    assert isinstance(blueprint_binding, Mapping)
    if {
        field: blueprint_binding[field]
        for field in ("revision", "content_sha256", "semantic_sha256")
    } != coverage_plan["blueprint_binding"]:
        _raise(
            "blueprint_binding_mismatch",
            "/blueprint_binding",
            "Timeline and Coverage Plan do not share one exact Blueprint binding.",
        )
    output = timeline["output"]
    plan_output = coverage_plan["output_binding"]
    assert isinstance(output, Mapping) and isinstance(plan_output, Mapping)
    if output["aspect_ratio"] != plan_output["aspect_ratio"]:
        _raise(
            "output_binding_mismatch",
            "/output/aspect_ratio",
            "Manual edits cannot change the pinned output aspect ratio.",
        )

    beats, manifest = _coverage_indexes(coverage_plan)
    clips = timeline["tracks"][0]["clips"]
    assert isinstance(clips, list)
    clip_beat_ids = [str(clip["beat_id"]) for clip in clips if isinstance(clip, Mapping)]
    is_structural_timeline = timeline.get("schema_version") == "2"
    if not is_structural_timeline and (
        len(clip_beat_ids) != len(beats)
        or len(set(clip_beat_ids)) != len(beats)
        or set(clip_beat_ids) != set(beats)
    ):
        _raise(
            "coverage_beat_set_mismatch",
            "/tracks/0/clips",
            "Timeline must retain exactly one clip for every pinned Coverage Beat.",
        )
    if is_structural_timeline and any(beat_id not in beats for beat_id in clip_beat_ids):
        _raise(
            "coverage_beat_set_mismatch",
            "/tracks/0/clips",
            "Every structural Timeline occurrence must retain a pinned Coverage Beat.",
        )
    for index, (clip, source) in enumerate(zip(clips, source_bindings, strict=True)):
        assert isinstance(clip, Mapping) and isinstance(source, Mapping)
        beat = beats[str(clip["beat_id"])]
        assignment = _assignment_for_clip(beat, assignment_id=clip["assignment_id"])
        path = f"/tracks/0/clips/{index}"
        if assignment is None or assignment.get("evidence_ref") != clip.get("evidence_ref"):
            _raise(
                "coverage_assignment_mismatch",
                f"{path}/assignment_id",
                "Timeline clip is not an assignment from its pinned Coverage Beat.",
            )
        evidence = manifest.get(str(clip["evidence_ref"]))
        if evidence is None:
            _raise(
                "coverage_proof_mismatch",
                f"{path}/evidence_ref",
                "Timeline evidence is absent from the pinned Coverage manifest.",
            )
        _require_clip_proof_match(clip, source, evidence, path=path)
    return beats, manifest


def _stable_clip_id(timeline_id: str, clip: Mapping[str, object]) -> str:
    span_identity: tuple[object, ...] = ()
    if clip["media_kind"] == "image":
        span_identity = tuple(
            clip[field]
            for field in (
                "analysis_run_id",
                "analysis_revision",
                "analysis_content_sha256",
                "source_binding_sha256",
            )
        )
    elif clip["media_kind"] == "video":
        span_identity = tuple(
            clip[field]
            for field in (
                "span_id",
                "analysis_run_id",
                "analysis_revision",
                "input_asset_sha256",
                "source_in_ms",
                "source_out_ms",
            )
        )
    return canonical_timeline_clip_id(
        timeline_id=timeline_id,
        beat_id=str(clip["beat_id"]),
        assignment_id=str(clip["assignment_id"]),
        evidence_ref=str(clip["evidence_ref"]),
        asset_id=str(clip["asset_id"]),
        asset_sha256=str(clip["asset_sha256"]),
        media_kind=str(clip["media_kind"]),
        span_identity=span_identity,
    )


def _canonical_source_for_clip(
    clip: Mapping[str, object],
    source: Mapping[str, object],
) -> dict[str, object]:
    result: dict[str, object] = {
        "clip_id": clip["clip_id"],
        "evidence_ref": clip["evidence_ref"],
        "asset_id": clip["asset_id"],
        "asset_sha256": clip["asset_sha256"],
        "asset_source_id": source["asset_source_id"],
        "coverage_proof_sha256": source["coverage_proof_sha256"],
        "media_kind": clip["media_kind"],
    }
    if clip["media_kind"] == "image":
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            result[field] = clip[field]
    elif clip["media_kind"] == "video":
        for field in (
            "span_id",
            "analysis_run_id",
            "analysis_revision",
            "input_asset_sha256",
            "source_in_ms",
            "source_out_ms",
        ):
            result[field] = clip[field]
    return result


def _reflow(
    pairs: list[tuple[dict[str, object], dict[str, object]]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    cursor = 0
    clips: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for ordinal, (clip, source) in enumerate(pairs):
        duration_ms = int(clip["end_ms"]) - int(clip["start_ms"])
        if duration_ms < 1:
            _raise("invalid_clip_duration", f"/tracks/0/clips/{ordinal}", "Clip duration is invalid.")
        clip["ordinal"] = ordinal
        clip["start_ms"] = cursor
        clip["end_ms"] = cursor + duration_ms
        cursor += duration_ms
        clips.append(clip)
        sources.append(_canonical_source_for_clip(clip, source))
    if cursor < 1 or cursor > TIMELINE_EDIT_MAX_DURATION_MS:
        _raise(
            "timeline_duration_limit_exceeded",
            "/output/duration_ms",
            "Edited Timeline duration exceeds the canonical limit.",
        )
    return clips, sources, cursor


def _replacement_clip(
    *,
    timeline_id: str,
    target: Mapping[str, object],
    assignment: Mapping[str, object],
    evidence: Mapping[str, object],
    replacement_source: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    evidence_ref = str(assignment["evidence_ref"])
    proof = evidence.get("proof")
    if evidence.get("status") != "verified" or type(proof) is not dict:
        _raise(
            "unverified_replacement",
            "/edit/assignment_id",
            "Replacement assignment must carry a verified Coverage proof.",
        )
    if replacement_source["evidence_ref"] != evidence_ref:
        _raise(
            "replacement_source_mismatch",
            "/replacement_source/evidence_ref",
            "Replacement source does not identify the selected alternative evidence.",
        )
    if (
        replacement_source["asset_id"] != proof.get("asset_id")
        or replacement_source["asset_sha256"] != proof.get("asset_sha256")
    ):
        _raise(
            "replacement_source_mismatch",
            "/replacement_source/asset_id",
            "Replacement source does not match the pinned alternative proof.",
        )
    duration_ms = int(target["end_ms"]) - int(target["start_ms"])
    common: dict[str, object] = {
        "ordinal": target["ordinal"],
        "beat_id": target["beat_id"],
        "assignment_id": assignment["assignment_id"],
        "evidence_ref": evidence_ref,
        "asset_id": proof["asset_id"],
        "asset_sha256": proof["asset_sha256"],
        "start_ms": target["start_ms"],
        "end_ms": int(target["start_ms"]) + duration_ms,
        "fit": "cover",
        "audio_enabled": False,
    }
    if proof["kind"] == "asset":
        if any(
            replacement_source.get(field) != proof.get(field)
            for field in _IMAGE_ANALYSIS_FIELDS
        ):
            _raise(
                "replacement_source_mismatch",
                "/replacement_source/analysis_revision",
                "Replacement image source does not match the exact pinned analysis proof.",
            )
        clip = {
            **common,
            "media_kind": "image",
            **{field: proof[field] for field in _IMAGE_ANALYSIS_FIELDS},
        }
    elif proof["kind"] == "span":
        source_in_ms = int(proof["start_ms"])
        source_out_ms = source_in_ms + duration_ms
        if source_out_ms > int(proof["end_ms"]):
            _raise(
                "replacement_slot_unsatisfied",
                "/edit/assignment_id",
                "Replacement video proof cannot satisfy the existing clip slot.",
            )
        clip = {
            **common,
            "media_kind": "video",
            "span_id": proof["span_id"],
            "analysis_run_id": proof["analysis_run_id"],
            "analysis_revision": proof["analysis_revision"],
            "input_asset_sha256": proof["input_asset_sha256"],
            "source_in_ms": source_in_ms,
            "source_out_ms": source_out_ms,
        }
    else:
        _raise(
            "unverified_replacement",
            "/edit/assignment_id",
            "Replacement proof kind is unsupported.",
        )
    clip["clip_id"] = _stable_clip_id(timeline_id, clip)
    source = {
        "asset_source_id": replacement_source["asset_source_id"],
        "coverage_proof_sha256": evidence["proof_sha256"],
    }
    return clip, source


def apply_timeline_edit(
    *,
    parent_timeline: Mapping[str, object],
    parent_source_bindings: Sequence[Mapping[str, object]],
    coverage_plan: Mapping[str, object],
    edit: Mapping[str, object],
    replacement_source: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Apply one closed edit and return canonical revision N+1 plus sources."""

    try:
        parent = require_current_canonical_timeline(parent_timeline)
        parent_sources = require_current_timeline_source_bindings(
            parent_source_bindings,
            timeline=parent,
        )
    except TimelineLoweringContractError as exc:
        _raise_from_lowering(exc, code="invalid_parent_timeline", path="/parent_timeline")
    try:
        plan = require_current_coverage_plan(coverage_plan)
    except CoverageContractError as exc:
        message = exc.errors[0]["message"] if exc.errors else "Coverage Plan is invalid."
        _raise("invalid_coverage_plan", "/coverage_plan", message)
    beats, manifest = _require_exact_timeline_coverage(parent, parent_sources, plan)
    operation = _closed_edit(edit)
    op = str(operation["op"])
    if op != "replace_clip" and replacement_source is not None:
        _raise(
            "unexpected_replacement_source",
            "/replacement_source",
            "Only replace_clip accepts a replacement source.",
        )
    normalized_replacement_source = (
        _closed_replacement_source(replacement_source) if op == "replace_clip" else None
    )

    clips = parent["tracks"][0]["clips"]
    assert isinstance(clips, list)
    pairs = [
        (deepcopy(clip), deepcopy(source))
        for clip, source in zip(clips, parent_sources, strict=True)
        if isinstance(clip, dict) and isinstance(source, dict)
    ]
    target_index = next(
        (index for index, (clip, _source) in enumerate(pairs) if clip["clip_id"] == operation["clip_id"]),
        None,
    )
    if target_index is None:
        _raise("unknown_clip", "/edit/clip_id", "Edit references a clip absent from the exact parent.")
    target, target_source = pairs[target_index]
    timeline_id = str(parent["timeline_id"])

    if op == "move_clip":
        to_index = _bounded_int(
            operation["to_index"],
            path="/edit/to_index",
            minimum=0,
            maximum=len(pairs) - 1,
        )
        pairs.insert(to_index, pairs.pop(target_index))
    elif op == "trim_clip":
        if target["media_kind"] != "video":
            _raise("invalid_trim_target", "/edit/clip_id", "Only a video clip may be trimmed.")
        source_in_ms = _bounded_int(
            operation["source_in_ms"],
            path="/edit/source_in_ms",
            minimum=0,
            maximum=TIMELINE_EDIT_MAX_SOURCE_MS,
        )
        source_out_ms = _bounded_int(
            operation["source_out_ms"],
            path="/edit/source_out_ms",
            minimum=0,
            maximum=TIMELINE_EDIT_MAX_SOURCE_MS,
        )
        if source_out_ms - source_in_ms < TIMELINE_EDIT_MIN_TRIM_MS:
            _raise(
                "trim_too_short",
                "/edit/source_out_ms",
                f"Trimmed video must be at least {TIMELINE_EDIT_MIN_TRIM_MS} ms.",
            )
        evidence = manifest[str(target["evidence_ref"])]
        proof = evidence["proof"]
        assert isinstance(proof, Mapping)
        if (
            proof.get("kind") != "span"
            or source_in_ms < proof["start_ms"]
            or source_out_ms > proof["end_ms"]
        ):
            _raise(
                "trim_outside_coverage_proof",
                "/edit/source_in_ms",
                "Trim range must remain inside the pinned Coverage proof bounds.",
            )
        target["source_in_ms"] = source_in_ms
        target["source_out_ms"] = source_out_ms
        target["end_ms"] = int(target["start_ms"]) + source_out_ms - source_in_ms
        target["clip_id"] = _stable_clip_id(timeline_id, target)
    elif op == "set_clip_duration":
        if target["media_kind"] != "image":
            _raise(
                "invalid_duration_target",
                "/edit/clip_id",
                "Only an image clip accepts an independent Timeline duration.",
            )
        duration_ms = _bounded_int(
            operation["duration_ms"],
            path="/edit/duration_ms",
            minimum=1,
        )
        target["end_ms"] = int(target["start_ms"]) + duration_ms
    else:
        assert op == "replace_clip" and normalized_replacement_source is not None
        beat = beats[str(target["beat_id"])]
        alternatives = beat["alternatives"]
        assert isinstance(alternatives, list)
        assignment = next(
            (
                candidate
                for candidate in alternatives
                if isinstance(candidate, Mapping)
                and candidate.get("assignment_id") == operation["assignment_id"]
            ),
            None,
        )
        if assignment is None:
            _raise(
                "invalid_replacement_candidate",
                "/edit/assignment_id",
                "Replacement must name an alternative assignment from the target Coverage Beat.",
            )
        evidence_ref = str(assignment["evidence_ref"])
        if parent.get("schema_version") == "1" and any(
            index != target_index and clip["evidence_ref"] == evidence_ref
            for index, (clip, _source) in enumerate(pairs)
        ):
            _raise(
                "duplicate_replacement_evidence",
                "/edit/assignment_id",
                "Replacement evidence is already used by another Timeline clip.",
            )
        evidence = manifest[evidence_ref]
        replacement_clip, replacement_binding = _replacement_clip(
            timeline_id=timeline_id,
            target=target,
            assignment=assignment,
            evidence=evidence,
            replacement_source=normalized_replacement_source,
        )
        pairs[target_index] = (replacement_clip, replacement_binding)

    reflowed_clips, reflowed_sources, output_duration_ms = _reflow(pairs)
    next_revision = int(parent["revision"]) + 1
    if next_revision > TIMELINE_EDIT_MAX_REVISION:
        _raise("revision_limit_exceeded", "/revision", "Timeline revision exceeds the canonical limit.")
    edited = deepcopy(parent)
    edited["revision"] = next_revision
    edited["parent"] = {
        "revision": parent["revision"],
        "content_sha256": canonical_timeline_content_sha256(parent),
    }
    edited["output"]["duration_ms"] = output_duration_ms
    edited["tracks"][0]["clips"] = reflowed_clips
    try:
        require_current_canonical_timeline(edited)
        require_current_timeline_source_bindings(reflowed_sources, timeline=edited)
    except TimelineLoweringContractError as exc:
        _raise_from_lowering(exc, code="invalid_edit_result", path="")
    _require_exact_timeline_coverage(edited, reflowed_sources, plan)
    return {"timeline": edited, "source_bindings": reflowed_sources}


def require_exact_timeline_edit_replay(
    timeline: object,
    *,
    source_bindings: object,
    parent_timeline: Mapping[str, object],
    parent_source_bindings: Sequence[Mapping[str, object]],
    coverage_plan: Mapping[str, object],
    edit: Mapping[str, object],
    replacement_source: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Cold-audit a manual revision by reapplying its exact edit inputs."""

    try:
        persisted = require_current_canonical_timeline(timeline)
        persisted_sources = require_current_timeline_source_bindings(
            source_bindings,
            timeline=persisted,
        )
        plan = require_current_coverage_plan(coverage_plan)
        _require_exact_timeline_coverage(persisted, persisted_sources, plan)
        replay = apply_timeline_edit(
            parent_timeline=parent_timeline,
            parent_source_bindings=parent_source_bindings,
            coverage_plan=plan,
            edit=edit,
            replacement_source=replacement_source,
        )
    except (TimelineLoweringContractError, CoverageContractError, TimelineEditContractError) as exc:
        raise TimelineEditContractError(
            [
                {
                    "code": "timeline_edit_replay_mismatch",
                    "path": "",
                    "message": "Persisted Timeline cannot be reproduced from the exact manual edit inputs.",
                }
            ]
        ) from exc
    if replay["timeline"] != persisted or replay["source_bindings"] != persisted_sources:
        _raise(
            "timeline_edit_replay_mismatch",
            "",
            "Persisted Timeline is not the exact result of the supplied manual edit.",
        )
    return persisted


__all__ = [
    "TIMELINE_EDIT_MAX_DURATION_MS",
    "TIMELINE_EDIT_MAX_REVISION",
    "TIMELINE_EDIT_MAX_SOURCE_MS",
    "TIMELINE_EDIT_MIN_TRIM_MS",
    "TimelineEditContractError",
    "apply_timeline_edit",
    "require_exact_timeline_edit_replay",
]
