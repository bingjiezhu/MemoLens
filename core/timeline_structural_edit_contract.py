"""Closed structural edits for canonical Timeline schema v2.

This authority is deliberately separate from ``timeline.apply_edit``.  It
accepts exactly one split or removal command, never a caller-supplied Timeline,
generic operation array, path, or draft-editor identity.  The functions are
pure; persistence and capability consumption remain caller responsibilities.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .coverage_contract import CoverageContractError, require_current_coverage_plan
from .timeline_edit_contract import (
    TIMELINE_EDIT_MAX_REVISION,
    TIMELINE_EDIT_MAX_SOURCE_MS,
    TIMELINE_EDIT_MIN_TRIM_MS,
    TimelineEditContractError,
    _reflow,
    _require_exact_timeline_coverage,
    _stable_clip_id,
)
from .timeline_lowering_contract import (
    CANONICAL_TIMELINE_STRUCTURAL_SCHEMA_VERSION,
    TIMELINE_MAX_CLIPS,
    TimelineLoweringContractError,
    canonical_timeline_content_sha256,
    require_current_canonical_timeline,
    require_current_timeline_source_bindings,
)


_STRUCTURAL_EDIT_FIELDS = {
    "split_clip": {"op", "clip_id", "source_split_ms"},
    "delete_clip": {"op", "clip_id"},
}


@dataclass(frozen=True)
class TimelineStructuralEditContractError(ValueError):
    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]):
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_timeline_structural_edit"),
                "path": str(item.get("path") or ""),
                "message": str(
                    item.get("message") or "Canonical Timeline structural edit is invalid."
                ),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"]
            if normalized
            else "Canonical Timeline structural edit is invalid.",
        )


def _raise(code: str, path: str, message: str) -> None:
    raise TimelineStructuralEditContractError(
        [{"code": code, "path": path, "message": message}]
    )


def _closed_structural_edit(value: object) -> dict[str, object]:
    if type(value) is not dict:
        _raise(
            "invalid_structural_edit",
            "/edit",
            "Structural edit must be one closed object.",
        )
    op = value.get("op")
    if type(op) is not str or op not in _STRUCTURAL_EDIT_FIELDS:
        _raise(
            "unsupported_structural_edit",
            "/edit/op",
            "Structural Timeline operation is unsupported.",
        )
    if set(value) != _STRUCTURAL_EDIT_FIELDS[op]:
        _raise(
            "invalid_structural_edit",
            "/edit",
            "Structural edit fields are not closed for this operation.",
        )
    clip_id = value.get("clip_id")
    if (
        type(clip_id) is not str
        or not 1 <= len(clip_id) <= 200
        or not clip_id[0].isalnum()
        or any(
            not character.isascii()
            or not (character.isalnum() or character in "._-")
            for character in clip_id
        )
    ):
        _raise(
            "invalid_clip_id",
            "/edit/clip_id",
            "Structural edit clip identity is invalid.",
        )
    if op == "split_clip":
        split_ms = value.get("source_split_ms")
        if (
            type(split_ms) is not int
            or split_ms < 0
            or split_ms > TIMELINE_EDIT_MAX_SOURCE_MS
        ):
            _raise(
                "invalid_source_split",
                "/edit/source_split_ms",
                "source_split_ms must be a bounded absolute source timestamp.",
            )
    return dict(value)


def apply_timeline_structural_edit(
    *,
    parent_timeline: Mapping[str, object],
    parent_source_bindings: Sequence[Mapping[str, object]],
    coverage_plan: Mapping[str, object],
    edit: Mapping[str, object],
) -> dict[str, object]:
    """Apply one exact split/remove operation and return schema-v2 N+1."""

    try:
        parent = require_current_canonical_timeline(parent_timeline)
        parent_sources = require_current_timeline_source_bindings(
            parent_source_bindings,
            timeline=parent,
        )
    except TimelineLoweringContractError as exc:
        _raise(
            "invalid_parent_timeline",
            "/parent_timeline",
            exc.errors[0]["message"] if exc.errors else "Parent Timeline is invalid.",
        )
    try:
        plan = require_current_coverage_plan(coverage_plan)
    except CoverageContractError as exc:
        _raise(
            "invalid_coverage_plan",
            "/coverage_plan",
            exc.errors[0]["message"] if exc.errors else "Coverage Plan is invalid.",
        )
    try:
        _require_exact_timeline_coverage(parent, parent_sources, plan)
    except TimelineEditContractError as exc:
        errors = getattr(exc, "errors", ())
        _raise(
            "coverage_binding_mismatch",
            "/coverage_plan",
            errors[0]["message"] if errors else "Timeline Coverage binding is invalid.",
        )

    operation = _closed_structural_edit(edit)
    clips = parent["tracks"][0]["clips"]
    assert isinstance(clips, list)
    pairs = [
        (deepcopy(clip), deepcopy(source))
        for clip, source in zip(clips, parent_sources, strict=True)
        if isinstance(clip, dict) and isinstance(source, dict)
    ]
    target_index = next(
        (
            index
            for index, (clip, _source) in enumerate(pairs)
            if clip["clip_id"] == operation["clip_id"]
        ),
        None,
    )
    if target_index is None:
        _raise(
            "unknown_clip",
            "/edit/clip_id",
            "Structural edit references a clip absent from the exact parent.",
        )

    target, target_source = pairs[target_index]
    op = str(operation["op"])
    if op == "split_clip":
        if target.get("media_kind") != "video":
            _raise(
                "invalid_split_target",
                "/edit/clip_id",
                "Only a video clip may be split.",
            )
        if len(pairs) >= TIMELINE_MAX_CLIPS:
            _raise(
                "timeline_clip_limit_exceeded",
                "/tracks/0/clips",
                f"Structural Timeline cannot exceed {TIMELINE_MAX_CLIPS} clips.",
            )
        split_ms = int(operation["source_split_ms"])
        source_in_ms = int(target["source_in_ms"])
        source_out_ms = int(target["source_out_ms"])
        if (
            split_ms - source_in_ms < TIMELINE_EDIT_MIN_TRIM_MS
            or source_out_ms - split_ms < TIMELINE_EDIT_MIN_TRIM_MS
        ):
            _raise(
                "split_child_too_short",
                "/edit/source_split_ms",
                f"Both split children must be at least {TIMELINE_EDIT_MIN_TRIM_MS} ms.",
            )
        left = deepcopy(target)
        right = deepcopy(target)
        left["source_out_ms"] = split_ms
        right["source_in_ms"] = split_ms
        left["end_ms"] = int(left["start_ms"]) + split_ms - source_in_ms
        right_duration_ms = source_out_ms - split_ms
        right["end_ms"] = int(right["start_ms"]) + right_duration_ms
        timeline_id = str(parent["timeline_id"])
        left["clip_id"] = _stable_clip_id(timeline_id, left)
        right["clip_id"] = _stable_clip_id(timeline_id, right)
        parent_clip_ids = {
            str(clip["clip_id"])
            for clip, _source in pairs
        }
        if (
            left["clip_id"] == right["clip_id"]
            or left["clip_id"] in parent_clip_ids
            or right["clip_id"] in parent_clip_ids
        ):
            _raise(
                "split_identity_collision",
                "/edit/source_split_ms",
                "Split children must have distinct stable identities absent from the parent.",
            )
        pairs[target_index : target_index + 1] = [
            (left, deepcopy(target_source)),
            (right, deepcopy(target_source)),
        ]
    else:
        assert op == "delete_clip"
        if len(pairs) <= 1:
            _raise(
                "cannot_delete_last_clip",
                "/edit/clip_id",
                "The final Timeline clip cannot be removed.",
            )
        pairs.pop(target_index)

    reflowed_clips, reflowed_sources, output_duration_ms = _reflow(pairs)
    next_revision = int(parent["revision"]) + 1
    if next_revision > TIMELINE_EDIT_MAX_REVISION:
        _raise(
            "revision_limit_exceeded",
            "/revision",
            "Timeline revision exceeds the canonical limit.",
        )
    edited = deepcopy(parent)
    edited["schema_version"] = CANONICAL_TIMELINE_STRUCTURAL_SCHEMA_VERSION
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
        _require_exact_timeline_coverage(edited, reflowed_sources, plan)
    except (TimelineLoweringContractError, TimelineEditContractError) as exc:
        errors = getattr(exc, "errors", ())
        _raise(
            "invalid_structural_edit_result",
            "",
            errors[0]["message"] if errors else "Structural edit result is invalid.",
        )
    return {"timeline": edited, "source_bindings": reflowed_sources}


def require_exact_timeline_structural_edit_replay(
    timeline: object,
    *,
    source_bindings: object,
    parent_timeline: Mapping[str, object],
    parent_source_bindings: Sequence[Mapping[str, object]],
    coverage_plan: Mapping[str, object],
    edit: Mapping[str, object],
) -> dict[str, Any]:
    """Cold-audit one persisted structural successor by exact replay."""

    try:
        persisted = require_current_canonical_timeline(timeline)
        persisted_sources = require_current_timeline_source_bindings(
            source_bindings,
            timeline=persisted,
        )
        replay = apply_timeline_structural_edit(
            parent_timeline=parent_timeline,
            parent_source_bindings=parent_source_bindings,
            coverage_plan=coverage_plan,
            edit=edit,
        )
    except (TimelineLoweringContractError, TimelineStructuralEditContractError) as exc:
        raise TimelineStructuralEditContractError(
            [
                {
                    "code": "timeline_structural_edit_replay_mismatch",
                    "path": "",
                    "message": "Persisted Timeline cannot be reproduced from the exact structural edit inputs.",
                }
            ]
        ) from exc
    if replay["timeline"] != persisted or replay["source_bindings"] != persisted_sources:
        _raise(
            "timeline_structural_edit_replay_mismatch",
            "",
            "Persisted Timeline is not the exact result of the supplied structural edit.",
        )
    return persisted


__all__ = [
    "TimelineStructuralEditContractError",
    "apply_timeline_structural_edit",
    "require_exact_timeline_structural_edit_replay",
]
