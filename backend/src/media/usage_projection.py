"""Deterministic read projections for immutable canonical Usage occurrences.

Usage occurrences are the only persisted fact.  This module deliberately does
not create a writable ``used`` flag: it merges video source intervals and
subtracts them from the asset source domain every time a projection is built.
All intervals are integer, half-open ``[start_ms, end_ms)`` ranges.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import TypeAlias

from core.residual_identity_contract import (
    build_residual_binding,
    canonical_usage_projection_sha256,
    require_unique_residual_bindings,
)


Interval: TypeAlias = tuple[int, int]
MAX_RESIDUALS_PER_PARENT = 256
MAX_RESIDUAL_CANDIDATES = 4_096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class UsageSelection:
    """Optional ledger scope used by ``used_in`` search semantics."""

    project_id: str
    export_revision: int | None = None


@dataclass(frozen=True)
class UsageQueryPolicy:
    """Closed search behavior derived from the public usage filter fields."""

    requested: bool = False
    unused_only: bool = False
    prefer_unused: bool = False
    allow_reuse: bool = True
    used_in: UsageSelection | None = None
    residual_of: str | None = None


def _interval(value: object) -> Interval:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
        or value[0] < 0
        or value[1] <= value[0]
    ):
        raise ValueError("Usage intervals must be positive integer half-open ranges.")
    return int(value[0]), int(value[1])


def union_half_open_intervals(
    intervals: Iterable[Sequence[int]],
    *,
    domain: Sequence[int] | None = None,
) -> list[Interval]:
    """Return a stable union, merging overlap and exact adjacency.

    If a domain is supplied, ranges are clipped to it and ranges with no
    intersection are ignored.  Clipping is appropriate for candidate-level
    projections; persisted occurrence validation remains Core's responsibility.
    """

    bounded_domain = None if domain is None else _interval(domain)
    normalized: list[Interval] = []
    for raw in intervals:
        start, end = _interval(raw)
        if bounded_domain is not None:
            start = max(start, bounded_domain[0])
            end = min(end, bounded_domain[1])
            if end <= start:
                continue
        normalized.append((start, end))
    normalized.sort()
    merged: list[Interval] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        prior_start, prior_end = merged[-1]
        merged[-1] = (prior_start, max(prior_end, end))
    return merged


def residual_half_open_intervals(
    domain: Sequence[int],
    used_intervals: Iterable[Sequence[int]],
) -> list[Interval]:
    """Subtract the normalized used union from one exact source domain."""

    start, end = _interval(domain)
    used = union_half_open_intervals(used_intervals, domain=(start, end))
    cursor = start
    residual: list[Interval] = []
    for used_start, used_end in used:
        if cursor < used_start:
            residual.append((cursor, used_start))
        cursor = max(cursor, used_end)
    if cursor < end:
        residual.append((cursor, end))
    return residual


def _positive_identifier(value: object, label: str) -> str:
    if type(value) is not str or not value or len(value) > 256 or "\x00" in value:
        raise ValueError(f"{label} is invalid.")
    return value


def _selected_occurrence(
    occurrence: Mapping[str, object],
    selection: UsageSelection | None,
) -> bool:
    if selection is None:
        return True
    if occurrence.get("project_id") != selection.project_id:
        return False
    return (
        selection.export_revision is None
        or occurrence.get("export_revision") == selection.export_revision
    )


def project_asset_usage(
    *,
    asset_id: str,
    media_kind: str,
    duration_ms: int | None,
    occurrences: Iterable[Mapping[str, object]],
    selection: UsageSelection | None = None,
) -> dict[str, object]:
    """Project one asset's used union, residual, and occurrence provenance.

    Callers must pass occurrences read from already validated successful export
    ledgers.  Shape checks here prevent a presentation bug from silently turning
    malformed input into a false unused claim.
    """

    normalized_asset = _positive_identifier(asset_id, "Usage asset identity")
    if media_kind not in {"image", "video"}:
        raise ValueError("Usage media kind is invalid.")
    if media_kind == "video" and (
        type(duration_ms) is not int or duration_ms <= 0
    ):
        raise ValueError("Video usage projection requires a positive source duration.")
    if media_kind == "image" and duration_ms is not None:
        raise ValueError("Image usage projection cannot have a time domain.")

    selected: list[Mapping[str, object]] = []
    used_in: set[tuple[str, int]] = set()
    raw_video_intervals: list[Interval] = []
    for occurrence in occurrences:
        if occurrence.get("asset_id") != normalized_asset:
            continue
        if not _selected_occurrence(occurrence, selection):
            continue
        if occurrence.get("media_kind") != media_kind:
            raise ValueError("Usage occurrence media kind does not match its asset.")
        project_id = _positive_identifier(
            occurrence.get("project_id"),
            "Usage project identity",
        )
        export_revision = occurrence.get("export_revision")
        if type(export_revision) is not int or not 1 <= export_revision <= 1_000_000:
            raise ValueError("Usage export revision is invalid.")
        selected.append(occurrence)
        used_in.add((project_id, export_revision))
        if media_kind == "video":
            occurrence_interval = _interval(
                (
                    occurrence.get("source_start_ms"),
                    occurrence.get("source_end_ms"),
                )
            )
            assert isinstance(duration_ms, int)
            if occurrence_interval[1] > duration_ms:
                raise ValueError("Usage occurrence exceeds its source domain.")
            raw_video_intervals.append(occurrence_interval)
        elif (
            occurrence.get("source_start_ms") is not None
            or occurrence.get("source_end_ms") is not None
        ):
            raise ValueError("Image usage occurrence cannot have a source interval.")

    used_ranges: list[Interval] = []
    residual_ranges: list[Interval] = []
    if media_kind == "video":
        assert isinstance(duration_ms, int)
        domain = (0, duration_ms)
        used_ranges = union_half_open_intervals(raw_video_intervals, domain=domain)
        residual_ranges = residual_half_open_intervals(domain, used_ranges)

    return {
        "asset_id": normalized_asset,
        "media_kind": media_kind,
        "occurrence_count": len(selected),
        "used": bool(selected),
        "used_in": [
            {"project_id": project_id, "export_revision": revision}
            for project_id, revision in sorted(used_in)
        ],
        "source_domain": (
            None if media_kind == "image" else [0, int(duration_ms)]
        ),
        "used_intervals": [[start, end] for start, end in used_ranges],
        "residual_intervals": [[start, end] for start, end in residual_ranges],
    }


def project_candidate_usage(
    candidate: Mapping[str, object],
    asset_projection: Mapping[str, object],
) -> dict[str, object]:
    """Clip an asset projection to one search candidate's exact source range."""

    if candidate.get("asset_id") != asset_projection.get("asset_id"):
        raise ValueError("Usage projection belongs to a different asset.")
    result_type = candidate.get("result_type")
    if result_type == "image_asset":
        if asset_projection.get("media_kind") != "image":
            raise ValueError("Image candidate has a non-image usage projection.")
        return {
            **dict(asset_projection),
            "candidate_domain": None,
            "fully_used": bool(asset_projection.get("used")),
            "has_residual": not bool(asset_projection.get("used")),
        }
    if result_type != "video_segment" or asset_projection.get("media_kind") != "video":
        raise ValueError("Video candidate has an invalid usage projection.")
    domain = _interval((candidate.get("start_ms"), candidate.get("end_ms")))
    source_domain = _interval(asset_projection.get("source_domain"))
    if domain[0] < source_domain[0] or domain[1] > source_domain[1]:
        raise ValueError("Usage candidate exceeds its source domain.")
    raw_used = asset_projection.get("used_intervals")
    if not isinstance(raw_used, list):
        raise ValueError("Video usage projection has no used interval list.")
    used = union_half_open_intervals(raw_used, domain=domain)
    residual = residual_half_open_intervals(domain, used)
    return {
        **dict(asset_projection),
        "candidate_domain": [domain[0], domain[1]],
        "used_intervals": [[start, end] for start, end in used],
        "residual_intervals": [[start, end] for start, end in residual],
        "fully_used": bool(used) and not residual,
        "has_residual": bool(residual),
    }


def annotate_usage_candidates(
    candidates: Sequence[Mapping[str, object]],
    occurrences: Sequence[Mapping[str, object]],
    policy: UsageQueryPolicy,
) -> list[dict[str, object]]:
    """Apply exact Usage query semantics without changing candidate identity.

    Video candidates retain their analyzed segment identity and carry exact
    candidate-clipped used/residual intervals.  This avoids inventing synthetic
    segment IDs that later creative commands could not resolve.
    """

    if not policy.requested:
        return [dict(candidate) for candidate in candidates]
    occurrences_by_asset: dict[str, list[Mapping[str, object]]] = {}
    for occurrence in occurrences:
        asset_id = _positive_identifier(
            occurrence.get("asset_id"),
            "Usage occurrence asset identity",
        )
        occurrences_by_asset.setdefault(asset_id, []).append(occurrence)

    assets: dict[str, dict[str, object]] = {}
    projected: list[dict[str, object]] = []
    for candidate_value in candidates:
        candidate = dict(candidate_value)
        asset_id = _positive_identifier(
            candidate.get("asset_id"),
            "Usage candidate asset identity",
        )
        result_type = candidate.get("result_type")
        media_kind = (
            "image" if result_type == "image_asset"
            else "video" if result_type == "video_segment"
            else None
        )
        if media_kind is None:
            raise ValueError("Usage candidate media kind is invalid.")
        duration_ms = candidate.get("duration_ms") if media_kind == "video" else None
        cached = assets.get(asset_id)
        if cached is None:
            cached = project_asset_usage(
                asset_id=asset_id,
                media_kind=media_kind,
                duration_ms=duration_ms if type(duration_ms) is int else None,
                occurrences=occurrences_by_asset.get(asset_id, []),
                selection=policy.used_in,
            )
            assets[asset_id] = cached
        elif cached.get("media_kind") != media_kind:
            raise ValueError("Usage candidate asset kind is inconsistent.")
        elif media_kind == "video" and (
            type(duration_ms) is not int
            or cached.get("source_domain") != [0, duration_ms]
        ):
            raise ValueError("Usage candidate asset duration is inconsistent.")
        usage = project_candidate_usage(candidate, cached)

        if policy.used_in is not None:
            if result_type == "image_asset" and not usage["used"]:
                continue
            if result_type == "video_segment" and not usage["used_intervals"]:
                continue
        if policy.residual_of is not None:
            if asset_id != policy.residual_of or result_type != "video_segment":
                continue
            if not usage["has_residual"]:
                continue
        exclude_used = policy.unused_only or not policy.allow_reuse
        if exclude_used:
            if result_type == "image_asset" and usage["used"]:
                continue
            if result_type == "video_segment" and not usage["has_residual"]:
                continue

        candidate["canonical_usage"] = usage
        projected.append(candidate)
    return projected


def materialize_residual_candidates(
    candidates: Sequence[Mapping[str, object]],
    *,
    usage_revision: str,
    policy: UsageQueryPolicy,
) -> list[dict[str, object]]:
    """Replace a partial parent with exact, identity-bearing residual children.

    Ordinary/default and explicit allow-reuse searches retain the persisted
    parent segment.  ``unused_only``, ``allow_reuse=false``, and
    ``residual_of`` return only exact children for a genuinely partial parent.
    ``prefer_unused`` returns the children plus their reusable parent so
    ranking can prefer the fresh ranges.
    """

    if type(usage_revision) is not str or _SHA256.fullmatch(usage_revision) is None:
        raise ValueError("Residual projection requires a canonical Usage revision.")
    explode = (
        policy.unused_only
        or not policy.allow_reuse
        or policy.residual_of is not None
        or policy.prefer_unused
    )
    if policy.used_in is not None and explode:
        raise ValueError(
            "Scoped Usage cannot authorize a globally reusable residual identity."
        )
    if not explode:
        return [dict(candidate) for candidate in candidates]

    result: list[dict[str, object]] = []
    all_bindings: list[dict[str, object]] = []
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        usage = candidate.get("canonical_usage")
        if candidate.get("result_type") != "video_segment" or type(usage) is not dict:
            result.append(candidate)
            continue
        used_intervals = usage.get("used_intervals")
        residual_intervals = usage.get("residual_intervals")
        if (
            type(used_intervals) is not list
            or not used_intervals
            or type(residual_intervals) is not list
            or not residual_intervals
        ):
            # A wholly unused analyzed parent remains its persisted ``seg_*``
            # identity.  A fully used parent was already removed by A2.
            result.append(candidate)
            continue
        if len(residual_intervals) > MAX_RESIDUALS_PER_PARENT:
            raise ValueError("Residual projection exceeds the per-parent limit.")
        parent_usage_sha256 = canonical_usage_projection_sha256(usage)
        parent_start_ms, parent_end_ms = _interval(
            (candidate.get("start_ms"), candidate.get("end_ms"))
        )
        children: list[dict[str, object]] = []
        for raw_interval in residual_intervals:
            source_in_ms, source_out_ms = _interval(raw_interval)
            binding = build_residual_binding(
                parent_segment_id=_positive_identifier(
                    candidate.get("id"),
                    "Residual parent segment identity",
                ),
                asset_id=_positive_identifier(
                    candidate.get("asset_id"),
                    "Residual asset identity",
                ),
                asset_sha256=_positive_identifier(
                    candidate.get("asset_sha256"),
                    "Residual asset digest",
                ),
                asset_source_id=_positive_identifier(
                    candidate.get("asset_source_id"),
                    "Residual source identity",
                ),
                source_binding_sha256=_positive_identifier(
                    candidate.get("source_binding_sha256"),
                    "Residual source binding digest",
                ),
                analysis_run_id=_positive_identifier(
                    candidate.get("analysis_run_id"),
                    "Residual analysis run identity",
                ),
                analysis_revision=(
                    candidate["analysis_revision"]
                    if type(candidate.get("analysis_revision")) is int
                    else 0
                ),
                input_asset_sha256=_positive_identifier(
                    candidate.get("input_asset_sha256"),
                    "Residual input asset digest",
                ),
                parent_start_ms=parent_start_ms,
                parent_end_ms=parent_end_ms,
                source_in_ms=source_in_ms,
                source_out_ms=source_out_ms,
                usage_revision=usage_revision,
                parent_usage_projection_sha256=parent_usage_sha256,
            )
            child_usage = deepcopy(usage)
            child_usage["candidate_domain"] = [source_in_ms, source_out_ms]
            child_usage["used_intervals"] = []
            child_usage["residual_intervals"] = [[source_in_ms, source_out_ms]]
            child_usage["fully_used"] = False
            child_usage["has_residual"] = True
            canonical_usage_projection_sha256(child_usage)
            child = {
                **candidate,
                "id": binding["residual_id"],
                "parent_segment_id": binding["parent_segment_id"],
                "start_ms": source_in_ms,
                "end_ms": source_out_ms,
                "canonical_usage": child_usage,
                "residual_binding": binding,
            }
            children.append(child)
            all_bindings.append(binding)
        result.extend(children)
        if policy.prefer_unused:
            result.append(candidate)
        if len(all_bindings) > MAX_RESIDUAL_CANDIDATES:
            raise ValueError("Residual projection exceeds the search result limit.")
    require_unique_residual_bindings(all_bindings)
    return result


__all__ = [
    "Interval",
    "UsageQueryPolicy",
    "UsageSelection",
    "annotate_usage_candidates",
    "materialize_residual_candidates",
    "project_asset_usage",
    "project_candidate_usage",
    "residual_half_open_intervals",
    "union_half_open_intervals",
]
