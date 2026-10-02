from __future__ import annotations

from collections.abc import Mapping
import sqlite3

from core.media_db import (
    CanonicalExportIntegrityError,
    MediaRepository,
    content_sha256,
)
from core.residual_identity_contract import (
    ResidualIdentityContractError,
    canonical_usage_projection_sha256,
    require_valid_residual_binding,
    source_binding_sha256,
)

from .usage_projection import project_asset_usage, project_candidate_usage


class ResidualParentResolutionError(ValueError):
    """Stable failure for a stale, substituted, or unavailable residual parent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ResidualParentResolutionError(code, message)


def _validate_presented_candidate(
    candidate: Mapping[str, object],
    binding: Mapping[str, object],
    *,
    allow_subrange: bool,
) -> None:
    expected = {
        "id": binding["residual_id"],
        "parent_segment_id": binding["parent_segment_id"],
        "asset_id": binding["asset_id"],
        "asset_source_id": binding["asset_source_id"],
        "result_type": "video_segment",
    }
    if any(candidate.get(field) != value for field, value in expected.items()):
        _fail(
            "residual_candidate_mismatch",
            "Residual candidate fields do not match the enclosed binding.",
        )
    start_ms = candidate.get("start_ms")
    end_ms = candidate.get("end_ms")
    exact_range = (
        start_ms == binding["source_in_ms"]
        and end_ms == binding["source_out_ms"]
    )
    bounded_subrange = (
        type(start_ms) is int
        and type(end_ms) is int
        and binding["source_in_ms"] <= start_ms < end_ms <= binding["source_out_ms"]
    )
    if not (bounded_subrange if allow_subrange else exact_range):
        _fail(
            "residual_candidate_mismatch",
            "Residual source range leaves its exact selected remainder.",
        )
    presented_sha256 = candidate.get("asset_sha256")
    if presented_sha256 is not None and presented_sha256 != binding["asset_sha256"]:
        _fail(
            "residual_candidate_mismatch",
            "Residual candidate content digest does not match its binding.",
        )


def resolve_current_residual_parent(
    repository: MediaRepository,
    residual_binding: Mapping[str, object],
    *,
    candidate: Mapping[str, object] | None = None,
    connection: sqlite3.Connection | None = None,
    allow_subrange: bool = False,
) -> dict[str, object]:
    """Resolve one residual to its exact current parent and pinned source.

    The lookup never asks for a preferred source and never treats ``rseg_*`` as
    a persisted segment identity.  A current analysis-head join and the
    path-free source-binding digest make source replacement and reanalysis
    explicit conflicts.
    """

    try:
        binding = require_valid_residual_binding(residual_binding)
    except ResidualIdentityContractError as exc:
        raise ResidualParentResolutionError(exc.code, str(exc)) from exc
    if candidate is not None:
        _validate_presented_candidate(
            candidate,
            binding,
            allow_subrange=allow_subrange,
        )
    if connection is None:
        with repository.transaction() as read_connection:
            return resolve_current_residual_parent(
                repository,
                binding,
                candidate=candidate,
                connection=read_connection,
                allow_subrange=allow_subrange,
            )
    try:
        row = connection.execute(
            """SELECT segment.id AS parent_segment_id,
                      segment.asset_id,segment.analysis_run_id,
                      segment.start_ms AS parent_start_ms,
                      segment.end_ms AS parent_end_ms,
                      run.revision AS analysis_revision,
                      run.input_asset_sha256,
                      asset.sha256 AS asset_sha256,
                      asset.duration_ms,
                      source.id AS asset_source_id,
                      source.library_root_id,source.observed_size,
                      source.observed_mtime_ns,source.source_file_id
                 FROM main.video_segments segment
                 JOIN main.asset_analysis_heads head
                   ON head.asset_id=segment.asset_id
                  AND head.analysis_run_id=segment.analysis_run_id
                 JOIN main.analysis_runs run
                   ON run.id=segment.analysis_run_id
                  AND run.asset_id=segment.asset_id
                  AND run.status='succeeded'
                 JOIN main.assets asset
                   ON asset.id=segment.asset_id AND asset.kind='video'
                 JOIN main.asset_sources source
                   ON source.id=? AND source.asset_id=asset.id
                  AND source.availability='available'
                 JOIN main.library_roots root
                   ON root.id=source.library_root_id AND root.status='active'
                WHERE segment.id=?""",
            (binding["asset_source_id"], binding["parent_segment_id"]),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ResidualParentResolutionError(
            "residual_parent_query_failed",
            "Residual parent facts could not be read.",
        ) from exc
    if row is None:
        _fail(
            "residual_parent_stale",
            "Residual parent analysis or pinned source is no longer current and available.",
        )
    source_facts = {
        "asset_source_id": str(row["asset_source_id"]),
        "asset_id": str(row["asset_id"]),
        "asset_sha256": str(row["asset_sha256"]),
        "library_root_id": str(row["library_root_id"]),
        "observed_size": row["observed_size"],
        "observed_mtime_ns": row["observed_mtime_ns"],
        "source_file_id": row["source_file_id"],
    }
    try:
        current_source_binding_sha256 = source_binding_sha256(source_facts)
    except ResidualIdentityContractError as exc:
        raise ResidualParentResolutionError(
            "residual_source_binding_invalid",
            "Residual source facts do not satisfy the current binding contract.",
        ) from exc
    expected = {
        "parent_segment_id": str(row["parent_segment_id"]),
        "asset_id": str(row["asset_id"]),
        "asset_sha256": str(row["asset_sha256"]),
        "asset_source_id": str(row["asset_source_id"]),
        "source_binding_sha256": current_source_binding_sha256,
        "analysis_run_id": str(row["analysis_run_id"]),
        "analysis_revision": int(row["analysis_revision"]),
        "input_asset_sha256": str(row["input_asset_sha256"]),
        "parent_start_ms": int(row["parent_start_ms"]),
        "parent_end_ms": int(row["parent_end_ms"]),
    }
    if any(binding[field] != value for field, value in expected.items()):
        _fail(
            "residual_parent_stale",
            "Residual parent, analysis, content, or source binding has changed.",
        )
    try:
        current_candidates, _analysis_heads = repository.mixed_candidates_in_transaction(
            connection,
        )
        current_asset_ids = sorted(
            {
                str(current["asset_id"])
                for current in current_candidates
                if current.get("asset_id") is not None
            }
        )
        facts = repository.canonical_material_search_facts_in_transaction(
            connection,
            current_asset_ids,
        )
        occurrences = facts.get("usage_occurrences")
        derivative_facts = facts.get("derivative_facts")
        if type(occurrences) is not list or type(derivative_facts) is not list:
            raise ValueError("residual_usage_projection_invalid")
        if any(
            type(item) is dict
            and item.get("video_sha256") == binding["asset_sha256"]
            for item in derivative_facts
        ):
            _fail(
                "residual_export_derivative",
                "Residual source bytes are a successful canonical Export output.",
            )
        if content_sha256(occurrences) != binding["usage_revision"]:
            _fail(
                "residual_usage_stale",
                "Residual Usage authority has changed since selection.",
            )
        asset_usage = project_asset_usage(
            asset_id=str(row["asset_id"]),
            media_kind="video",
            duration_ms=row["duration_ms"],
            occurrences=occurrences,
        )
        parent_usage = project_candidate_usage(
            {
                "result_type": "video_segment",
                "asset_id": str(row["asset_id"]),
                "start_ms": int(row["parent_start_ms"]),
                "end_ms": int(row["parent_end_ms"]),
            },
            asset_usage,
        )
        if (
            canonical_usage_projection_sha256(parent_usage)
            != binding["parent_usage_projection_sha256"]
        ):
            _fail(
                "residual_usage_stale",
                "Residual interval is no longer present in current Usage authority.",
            )
        residual_intervals = parent_usage.get("residual_intervals")
        if (
            type(residual_intervals) is not list
            or [binding["source_in_ms"], binding["source_out_ms"]]
            not in residual_intervals
        ):
            _fail(
                "residual_usage_stale",
                "Residual interval is no longer present in current Usage authority.",
            )
    except ResidualParentResolutionError:
        raise
    except (CanonicalExportIntegrityError, KeyError, sqlite3.Error, TypeError, ValueError) as exc:
        raise ResidualParentResolutionError(
            "residual_usage_authority_unavailable",
            "Residual Usage and derivative authority could not be validated.",
        ) from exc
    return {
        **expected,
        "residual_id": binding["residual_id"],
        "source_in_ms": binding["source_in_ms"],
        "source_out_ms": binding["source_out_ms"],
        "duration_ms": row["duration_ms"],
        "residual_binding": binding,
    }


__all__ = [
    "ResidualParentResolutionError",
    "resolve_current_residual_parent",
]
