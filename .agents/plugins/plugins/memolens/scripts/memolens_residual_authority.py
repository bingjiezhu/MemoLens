"""Standalone, deterministic Usage and residual-video read projection.

The contract is deliberately local to the plugin runtime: Codex and the
DeepSeek Harness load this same module through ``memolens_mcp.py``.  Residuals
are read projections over current ``seg_*`` parents; this module never creates
or mutates a ``video_segments`` row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
import sqlite3
from typing import TypeAlias

from memolens_creative_blueprint import canonical_sha256


Interval: TypeAlias = tuple[int, int]
MAX_SOURCE_MS = 7 * 24 * 60 * 60 * 1000
MAX_RESIDUALS_PER_PARENT = 256
MAX_RESIDUAL_CANDIDATES = 4_096

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RESIDUAL_ID = re.compile(r"rseg_[0-9a-f]{64}\Z")
_ASSET_ID = re.compile(r"asset_[0-9a-f]{24}\Z")
_ASSET_SOURCE_ID = re.compile(r"src_[0-9a-f]{24}\Z")
_ANALYSIS_RUN_ID = re.compile(r"arun_[0-9a-f]{32}\Z")
_PARENT_SEGMENT_ID = re.compile(
    r"seg_(?P<asset_digest>[0-9a-f]{24})_"
    r"(?P<analysis_revision>[1-9][0-9]{0,6})_"
    r"(?:0|[1-9][0-9]{0,6})\Z"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_BINDING_FIELDS = frozenset(
    {
        "object",
        "contract_version",
        "residual_id",
        "parent_segment_id",
        "asset_id",
        "asset_sha256",
        "asset_source_id",
        "source_binding_sha256",
        "analysis_run_id",
        "analysis_revision",
        "input_asset_sha256",
        "parent_start_ms",
        "parent_end_ms",
        "source_in_ms",
        "source_out_ms",
        "usage_revision",
        "parent_usage_projection_sha256",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "asset_source_id",
        "asset_id",
        "asset_sha256",
        "library_root_id",
        "observed_size",
        "observed_mtime_ns",
        "source_file_id",
    }
)
_USAGE_FIELDS = frozenset(
    {
        "asset_id",
        "media_kind",
        "occurrence_count",
        "used",
        "used_in",
        "source_domain",
        "candidate_domain",
        "used_intervals",
        "residual_intervals",
        "fully_used",
        "has_residual",
    }
)
_FILTER_FIELDS = frozenset(
    {"unused_only", "prefer_unused", "allow_reuse", "used_in", "residual_of"}
)
_CURRENT_CANDIDATE_ASSET_IDS_SQL = """SELECT asset.id AS asset_id
     FROM assets AS asset
     JOIN asset_sources AS source
       ON source.asset_id=asset.id AND source.is_preferred=1
     LEFT JOIN current_asset_reviews AS review
       ON review.asset_id=asset.id
    WHERE asset.kind='image'
      AND COALESCE(review.inbox_state,'inbox')!='archived'
    UNION
   SELECT segment.asset_id AS asset_id
     FROM current_video_segments AS segment
     JOIN analysis_runs AS run
       ON run.id=segment.analysis_run_id
      AND run.asset_id=segment.asset_id
      AND run.status='succeeded'
     JOIN asset_sources AS source
       ON source.asset_id=segment.asset_id
      AND source.is_preferred=1
      AND source.availability='available'
     JOIN library_roots AS root
       ON root.id=source.library_root_id
      AND root.status='active'
     LEFT JOIN current_asset_reviews AS review
       ON review.asset_id=segment.asset_id
    WHERE COALESCE(review.inbox_state,'inbox')!='archived'
    ORDER BY asset_id"""


class ResidualAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ResidualAuthorityError(code, message)


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("residual_binding_invalid", f"{label} must be a lowercase SHA-256 digest.")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail("residual_binding_invalid", f"{label} is invalid.")
    return value


def _interval(value: object, label: str = "Usage interval") -> Interval:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
        or value[0] < 0
        or value[1] <= value[0]
        or value[1] > MAX_SOURCE_MS
    ):
        _fail(
            "usage_projection_invalid",
            f"{label} must be a bounded integer half-open interval.",
        )
    return int(value[0]), int(value[1])


def union_half_open_intervals(
    intervals: Iterable[Sequence[int]],
    *,
    domain: Sequence[int] | None = None,
) -> list[Interval]:
    bounded = None if domain is None else _interval(domain)
    normalized: list[Interval] = []
    for raw in intervals:
        start, end = _interval(raw)
        if bounded is not None:
            start = max(start, bounded[0])
            end = min(end, bounded[1])
            if end <= start:
                continue
        normalized.append((start, end))
    normalized.sort()
    merged: list[Interval] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prior_start, prior_end = merged[-1]
            merged[-1] = (prior_start, max(prior_end, end))
    return merged


def residual_half_open_intervals(
    domain: Sequence[int],
    used_intervals: Iterable[Sequence[int]],
) -> list[Interval]:
    start, end = _interval(domain)
    cursor = start
    residual: list[Interval] = []
    for used_start, used_end in union_half_open_intervals(
        used_intervals,
        domain=(start, end),
    ):
        if cursor < used_start:
            residual.append((cursor, used_start))
        cursor = max(cursor, used_end)
    if cursor < end:
        residual.append((cursor, end))
    return residual


def source_binding_sha256(value: Mapping[str, object]) -> str:
    """Hash the exact path-free source-row facts pinned by a residual."""

    if type(value) is not dict or set(value) != _SOURCE_FIELDS:
        _fail("source_binding_invalid", "Source binding facts must use the closed contract.")
    source_id = value.get("asset_source_id")
    asset_id = value.get("asset_id")
    if type(source_id) is not str or _ASSET_SOURCE_ID.fullmatch(source_id) is None:
        _fail("source_binding_invalid", "Source identity is invalid.")
    if type(asset_id) is not str or _ASSET_ID.fullmatch(asset_id) is None:
        _fail("source_binding_invalid", "Source asset identity is invalid.")
    asset_sha256 = _sha(value.get("asset_sha256"), "Source asset digest")
    if asset_id != f"asset_{asset_sha256[:24]}":
        _fail("source_binding_invalid", "Source asset identity does not match its digest.")
    _identifier(value.get("library_root_id"), "Library root identity")
    observed_size = value.get("observed_size")
    observed_mtime_ns = value.get("observed_mtime_ns")
    source_file_id = value.get("source_file_id")
    if type(observed_size) is not int or observed_size < 0:
        _fail("source_binding_invalid", "Observed source size is invalid.")
    if observed_mtime_ns is not None and (
        type(observed_mtime_ns) is not int or observed_mtime_ns < 0
    ):
        _fail("source_binding_invalid", "Observed source mtime is invalid.")
    if source_file_id is not None and (
        type(source_file_id) is not str
        or not source_file_id
        or len(source_file_id) > 512
        or "\x00" in source_file_id
    ):
        _fail("source_binding_invalid", "Source file identity is invalid.")
    return canonical_sha256(
        {
            "object": "memolens.residual_source_binding",
            "contract_version": "1",
            **dict(value),
        }
    )


def _usage_intervals(value: object, label: str) -> list[Interval]:
    if type(value) is not list:
        _fail("usage_projection_invalid", f"{label} must be an interval array.")
    intervals = [_interval(item, label) for item in value]
    if intervals != sorted(intervals):
        _fail("usage_projection_invalid", f"{label} must be sorted.")
    for prior, current in zip(intervals, intervals[1:]):
        if current[0] <= prior[1]:
            _fail(
                "usage_projection_invalid",
                f"{label} must be normalized and non-adjacent.",
            )
    return intervals


def canonical_usage_projection_sha256(value: Mapping[str, object]) -> str:
    if type(value) is not dict or set(value) != _USAGE_FIELDS:
        _fail("usage_projection_invalid", "Usage projection must use the closed contract.")
    asset_id = value.get("asset_id")
    if type(asset_id) is not str or _ASSET_ID.fullmatch(asset_id) is None:
        _fail("usage_projection_invalid", "Usage asset identity is invalid.")
    if value.get("media_kind") != "video":
        _fail("usage_projection_invalid", "Residual Usage projection must be video.")
    count = value.get("occurrence_count")
    if type(count) is not int or not 0 <= count <= 1_000_000:
        _fail("usage_projection_invalid", "Usage occurrence count is invalid.")
    if any(type(value.get(field)) is not bool for field in ("used", "fully_used", "has_residual")):
        _fail("usage_projection_invalid", "Usage flags must be boolean.")
    raw_used_in = value.get("used_in")
    if type(raw_used_in) is not list or len(raw_used_in) > 1_000_000:
        _fail("usage_projection_invalid", "Usage provenance is invalid.")
    used_in: list[tuple[str, int]] = []
    for row in raw_used_in:
        if type(row) is not dict or set(row) != {"project_id", "export_revision"}:
            _fail("usage_projection_invalid", "Usage provenance row is invalid.")
        project_id = _identifier(row.get("project_id"), "Usage project identity")
        revision = row.get("export_revision")
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            _fail("usage_projection_invalid", "Usage export revision is invalid.")
        used_in.append((project_id, revision))
    if used_in != sorted(set(used_in)):
        _fail("usage_projection_invalid", "Usage provenance must be unique and sorted.")
    if value["used"] != (count > 0) or bool(used_in) != value["used"]:
        _fail("usage_projection_invalid", "Usage occurrence facts are inconsistent.")
    source_domain = _interval(value.get("source_domain"), "Usage source domain")
    candidate_domain = _interval(value.get("candidate_domain"), "Usage candidate domain")
    if source_domain[0] != 0 or not (
        source_domain[0] <= candidate_domain[0] < candidate_domain[1] <= source_domain[1]
    ):
        _fail("usage_projection_invalid", "Usage candidate exceeds its source domain.")
    used = _usage_intervals(value.get("used_intervals"), "Usage used intervals")
    residual = _usage_intervals(
        value.get("residual_intervals"),
        "Usage residual intervals",
    )
    partition = [(start, end, "used") for start, end in used]
    partition += [(start, end, "residual") for start, end in residual]
    partition.sort(key=lambda row: (row[0], row[1], row[2]))
    cursor = candidate_domain[0]
    for start, end, _kind in partition:
        if start != cursor or end > candidate_domain[1]:
            _fail("usage_projection_invalid", "Usage intervals do not partition the candidate.")
        cursor = end
    if cursor != candidate_domain[1]:
        _fail("usage_projection_invalid", "Usage intervals do not cover the candidate domain.")
    if value["fully_used"] != (bool(used) and not residual):
        _fail("usage_projection_invalid", "Usage fully-used flag is inconsistent.")
    if value["has_residual"] != bool(residual):
        _fail("usage_projection_invalid", "Usage residual flag is inconsistent.")
    return canonical_sha256(dict(value))


def _binding_preimage(value: Mapping[str, object]) -> dict[str, object]:
    return {key: value[key] for key in sorted(_BINDING_FIELDS - {"residual_id"})}


def require_valid_residual_binding(value: Mapping[str, object]) -> dict[str, object]:
    if type(value) is not dict or set(value) != _BINDING_FIELDS:
        _fail("residual_binding_invalid", "Residual binding must use the closed contract.")
    if value.get("object") != "memolens.residual_binding" or value.get("contract_version") != "1":
        _fail("residual_binding_invalid", "Residual contract identity is invalid.")
    residual_id = value.get("residual_id")
    if type(residual_id) is not str or _RESIDUAL_ID.fullmatch(residual_id) is None:
        _fail("residual_binding_invalid", "Residual identity is invalid.")
    parent_id = value.get("parent_segment_id")
    parent_match = _PARENT_SEGMENT_ID.fullmatch(parent_id) if type(parent_id) is str else None
    if parent_match is None:
        _fail("residual_binding_invalid", "Parent segment identity is invalid.")
    asset_id = value.get("asset_id")
    if type(asset_id) is not str or _ASSET_ID.fullmatch(asset_id) is None:
        _fail("residual_binding_invalid", "Residual asset identity is invalid.")
    asset_sha256 = _sha(value.get("asset_sha256"), "Residual asset digest")
    if asset_id != f"asset_{asset_sha256[:24]}" or parent_match.group("asset_digest") != asset_sha256[:24]:
        _fail("residual_binding_invalid", "Residual parent and asset identity differ.")
    source_id = value.get("asset_source_id")
    if type(source_id) is not str or _ASSET_SOURCE_ID.fullmatch(source_id) is None:
        _fail("residual_binding_invalid", "Residual source identity is invalid.")
    _sha(value.get("source_binding_sha256"), "Residual source binding digest")
    run_id = value.get("analysis_run_id")
    if type(run_id) is not str or _ANALYSIS_RUN_ID.fullmatch(run_id) is None:
        _fail("residual_binding_invalid", "Residual analysis run identity is invalid.")
    revision = value.get("analysis_revision")
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        _fail("residual_binding_invalid", "Residual analysis revision is invalid.")
    if int(parent_match.group("analysis_revision")) != revision:
        _fail("residual_binding_invalid", "Parent analysis revision is inconsistent.")
    if _sha(value.get("input_asset_sha256"), "Residual analysis input digest") != asset_sha256:
        _fail("residual_binding_invalid", "Residual analysis input differs from the asset.")
    parent = _interval(
        (value.get("parent_start_ms"), value.get("parent_end_ms")),
        "Residual parent domain",
    )
    residual = _interval(
        (value.get("source_in_ms"), value.get("source_out_ms")),
        "Residual source interval",
    )
    if residual[0] < parent[0] or residual[1] > parent[1] or residual == parent:
        _fail("residual_binding_invalid", "Residual interval is not a strict parent remainder.")
    _sha(value.get("usage_revision"), "Residual Usage revision")
    _sha(value.get("parent_usage_projection_sha256"), "Residual Usage projection digest")
    if residual_id != f"rseg_{canonical_sha256(_binding_preimage(value))}":
        _fail("residual_identity_mismatch", "Residual identity does not match its binding.")
    return deepcopy(dict(value))


def build_residual_binding(**values: object) -> dict[str, object]:
    preimage = {
        "object": "memolens.residual_binding",
        "contract_version": "1",
        **values,
    }
    return require_valid_residual_binding(
        {**preimage, "residual_id": f"rseg_{canonical_sha256(preimage)}"}
    )


def current_candidate_usage_facts(
    connection: sqlite3.Connection,
    raw_occurrences: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], str]:
    """Freeze the mixed-search Usage domain inside a caller-owned snapshot.

    Residual identity is global to every current, non-archived mixed candidate,
    including images.  It must not be weakened to the residual's one asset.
    """

    try:
        rows = connection.execute(_CURRENT_CANDIDATE_ASSET_IDS_SQL).fetchall()
    except sqlite3.Error:
        _fail(
            "usage_authority_integrity_error",
            "The current material candidate domain could not be read.",
        )
    if len(rows) > 100_000:
        _fail(
            "usage_authority_integrity_error",
            "The current material candidate domain is too large.",
        )
    asset_ids: set[str] = set()
    for row in rows:
        asset_id = row["asset_id"]
        if type(asset_id) is not str or _IDENTIFIER.fullmatch(asset_id) is None:
            _fail(
                "usage_authority_integrity_error",
                "The current material candidate domain contains an invalid identity.",
            )
        asset_ids.add(asset_id)
    occurrences = [
        deepcopy(dict(item))
        for item in raw_occurrences
        if item.get("asset_id") in asset_ids
    ]
    return occurrences, canonical_sha256(occurrences)


def revalidate_residual_proof_in_connection(
    connection: sqlite3.Connection,
    *,
    evidence_ref: object,
    presented_proof: object,
    occurrences: Sequence[Mapping[str, object]],
    usage_revision: str,
    derivative_sha256s: frozenset[str],
) -> dict[str, object] | None:
    """Revalidate one presented residual against current source and Usage facts.

    Contract-invalid input is rejected by the Blueprint schema before this
    function.  A valid historical proof whose parent/source/analysis/Usage is
    no longer current returns ``None`` and therefore cannot execute.
    """

    if (
        type(evidence_ref) is not str
        or type(presented_proof) is not dict
        or set(presented_proof) != {"kind", "residual_binding"}
        or presented_proof.get("kind") != "residual_span"
    ):
        return None
    try:
        binding = require_valid_residual_binding(
            presented_proof["residual_binding"]  # type: ignore[arg-type]
        )
    except (KeyError, ResidualAuthorityError, TypeError):
        return None
    if evidence_ref != f"memolens://evidence/span/{binding['residual_id']}":
        return None
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
                 FROM video_segments AS segment
                 JOIN asset_analysis_heads AS head
                   ON head.asset_id=segment.asset_id
                  AND head.analysis_run_id=segment.analysis_run_id
                 JOIN analysis_runs AS run
                   ON run.id=head.analysis_run_id
                  AND run.asset_id=head.asset_id
                  AND run.status='succeeded'
                 JOIN assets AS asset
                   ON asset.id=segment.asset_id AND asset.kind='video'
                 JOIN asset_sources AS source
                   ON source.id=? AND source.asset_id=asset.id
                  AND source.availability='available'
                 JOIN library_roots AS root
                   ON root.id=source.library_root_id AND root.status='active'
                WHERE segment.id=?""",
            (binding["asset_source_id"], binding["parent_segment_id"]),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ResidualAuthorityError(
            "residual_authority_integrity_error",
            "Residual parent authority could not be read.",
        ) from exc
    if row is None:
        return None
    try:
        source_digest = source_binding_sha256(
            {
                "asset_source_id": str(row["asset_source_id"]),
                "asset_id": str(row["asset_id"]),
                "asset_sha256": str(row["asset_sha256"]),
                "library_root_id": str(row["library_root_id"]),
                "observed_size": row["observed_size"],
                "observed_mtime_ns": row["observed_mtime_ns"],
                "source_file_id": row["source_file_id"],
            }
        )
    except ResidualAuthorityError:
        return None
    current = {
        "parent_segment_id": str(row["parent_segment_id"]),
        "asset_id": str(row["asset_id"]),
        "asset_sha256": str(row["asset_sha256"]),
        "asset_source_id": str(row["asset_source_id"]),
        "source_binding_sha256": source_digest,
        "analysis_run_id": str(row["analysis_run_id"]),
        "analysis_revision": row["analysis_revision"],
        "input_asset_sha256": str(row["input_asset_sha256"]),
        "parent_start_ms": row["parent_start_ms"],
        "parent_end_ms": row["parent_end_ms"],
    }
    if any(binding[field] != value for field, value in current.items()):
        return None
    duration_ms = row["duration_ms"]
    if type(duration_ms) is not int or duration_ms <= 0:
        return None
    if binding["asset_sha256"] in derivative_sha256s:
        return None
    if binding["usage_revision"] != usage_revision:
        return None
    asset_occurrences = [
        item for item in occurrences if item.get("asset_id") == binding["asset_id"]
    ]
    used_in = sorted(
        {
            (str(item["project_id"]), int(item["export_revision"]))
            for item in asset_occurrences
        }
    )
    raw_intervals = [
        (int(item["source_start_ms"]), int(item["source_end_ms"]))
        for item in asset_occurrences
        if type(item.get("source_start_ms")) is int
        and type(item.get("source_end_ms")) is int
    ]
    parent_domain = (binding["parent_start_ms"], binding["parent_end_ms"])
    used_intervals = union_half_open_intervals(
        raw_intervals,
        domain=parent_domain,  # type: ignore[arg-type]
    )
    residual_intervals = residual_half_open_intervals(
        parent_domain,  # type: ignore[arg-type]
        used_intervals,
    )
    projection = {
        "asset_id": binding["asset_id"],
        "media_kind": "video",
        "occurrence_count": len(asset_occurrences),
        "used": bool(asset_occurrences),
        "used_in": [
            {"project_id": project_id, "export_revision": export_revision}
            for project_id, export_revision in used_in
        ],
        "source_domain": [0, duration_ms],
        "candidate_domain": list(parent_domain),
        "used_intervals": [list(item) for item in used_intervals],
        "residual_intervals": [list(item) for item in residual_intervals],
        "fully_used": bool(used_intervals) and not residual_intervals,
        "has_residual": bool(residual_intervals),
    }
    try:
        projection_sha256 = canonical_usage_projection_sha256(projection)
    except ResidualAuthorityError:
        return None
    if (
        projection_sha256 != binding["parent_usage_projection_sha256"]
        or (binding["source_in_ms"], binding["source_out_ms"])
        not in residual_intervals
    ):
        return None
    return {
        "kind": "residual_span",
        "residual_binding": binding,
    }


@dataclass(frozen=True)
class UsageSelection:
    project_id: str
    export_revision: int | None = None


@dataclass(frozen=True)
class UsagePolicy:
    requested: bool = False
    unused_only: bool = False
    prefer_unused: bool = False
    allow_reuse: bool = True
    used_in: UsageSelection | None = None
    residual_of: str | None = None


def _usage_selection(value: object) -> UsageSelection:
    if type(value) is str:
        return UsageSelection(project_id=_identifier(value, "Usage project identity"))
    if (
        type(value) is not dict
        or not set(value).issubset({"project_id", "export_revision"})
        or "project_id" not in value
    ):
        _fail(
            "usage_filter_invalid",
            "filters.used_in must be a project identity or a closed project/export selector.",
        )
    revision = value.get("export_revision")
    if revision is not None and (
        type(revision) is not int or not 1 <= revision <= 1_000_000
    ):
        _fail(
            "usage_filter_invalid",
            "filters.used_in.export_revision must be a positive integer.",
        )
    return UsageSelection(
        project_id=_identifier(
            value.get("project_id"),
            "Usage project identity",
        ),
        export_revision=revision,
    )


def parse_usage_filters(value: object) -> UsagePolicy:
    if value is None:
        return UsagePolicy()
    if type(value) is not dict or not set(value).issubset(_FILTER_FIELDS):
        _fail("usage_filter_invalid", "Usage filters must use the closed contract.")
    if not value:
        return UsagePolicy()
    for field in ("unused_only", "prefer_unused", "allow_reuse"):
        if field in value and type(value[field]) is not bool:
            _fail("usage_filter_invalid", f"filters.{field} must be boolean.")
    unused_only = bool(value.get("unused_only", False))
    prefer_unused = bool(value.get("prefer_unused", False))
    allow_reuse = bool(value.get("allow_reuse", not unused_only))
    used_in = (
        None
        if value.get("used_in") is None
        else _usage_selection(value["used_in"])
    )
    residual_of = value.get("residual_of")
    if residual_of is not None:
        residual_of = _identifier(residual_of, "Residual asset identity")
    if unused_only and allow_reuse:
        _fail("usage_filter_invalid", "unused_only conflicts with allow_reuse=true.")
    if prefer_unused and not allow_reuse:
        _fail("usage_filter_invalid", "prefer_unused requires allow_reuse=true.")
    if unused_only and prefer_unused:
        _fail("usage_filter_invalid", "unused_only conflicts with prefer_unused.")
    if used_in is not None and (
        unused_only
        or prefer_unused
        or residual_of is not None
        or not allow_reuse
    ):
        _fail(
            "usage_filter_invalid",
            "filters.used_in cannot authorize a global residual or no-reuse projection.",
        )
    return UsagePolicy(
        requested=True,
        unused_only=unused_only,
        prefer_unused=prefer_unused,
        allow_reuse=allow_reuse,
        used_in=used_in,
        residual_of=residual_of,
    )


def _project_asset_usage(
    *,
    asset_id: str,
    media_kind: str,
    duration_ms: int | None,
    occurrences: Iterable[Mapping[str, object]],
    selection: UsageSelection | None = None,
) -> dict[str, object]:
    _identifier(asset_id, "Usage asset identity")
    if media_kind == "video" and (type(duration_ms) is not int or duration_ms <= 0):
        _fail("usage_projection_invalid", "Video Usage requires a positive duration.")
    selected: list[Mapping[str, object]] = []
    used_in: set[tuple[str, int]] = set()
    raw_intervals: list[Interval] = []
    for occurrence in occurrences:
        if occurrence.get("asset_id") != asset_id:
            continue
        if selection is not None and (
            occurrence.get("project_id") != selection.project_id
            or (
                selection.export_revision is not None
                and occurrence.get("export_revision") != selection.export_revision
            )
        ):
            continue
        if occurrence.get("media_kind") != media_kind:
            _fail("usage_projection_invalid", "Usage media kind differs from its asset.")
        project_id = _identifier(occurrence.get("project_id"), "Usage project identity")
        export_revision = occurrence.get("export_revision")
        if type(export_revision) is not int or not 1 <= export_revision <= 1_000_000:
            _fail("usage_projection_invalid", "Usage export revision is invalid.")
        selected.append(occurrence)
        used_in.add((project_id, export_revision))
        if media_kind == "video":
            interval = _interval(
                (occurrence.get("source_start_ms"), occurrence.get("source_end_ms"))
            )
            assert isinstance(duration_ms, int)
            if interval[1] > duration_ms:
                _fail("usage_projection_invalid", "Usage exceeds its source duration.")
            raw_intervals.append(interval)
        elif occurrence.get("source_start_ms") is not None or occurrence.get("source_end_ms") is not None:
            _fail("usage_projection_invalid", "Image Usage cannot have a source interval.")
    used: list[Interval] = []
    residual: list[Interval] = []
    if media_kind == "video":
        assert isinstance(duration_ms, int)
        used = union_half_open_intervals(raw_intervals, domain=(0, duration_ms))
        residual = residual_half_open_intervals((0, duration_ms), used)
    return {
        "asset_id": asset_id,
        "media_kind": media_kind,
        "occurrence_count": len(selected),
        "used": bool(selected),
        "used_in": [
            {"project_id": project_id, "export_revision": revision}
            for project_id, revision in sorted(used_in)
        ],
        "source_domain": None if media_kind == "image" else [0, int(duration_ms)],
        "used_intervals": [[start, end] for start, end in used],
        "residual_intervals": [[start, end] for start, end in residual],
    }


def project_candidate_usage(
    candidate: Mapping[str, object],
    occurrences: Sequence[Mapping[str, object]],
    *,
    selection: UsageSelection | None = None,
) -> dict[str, object]:
    result_type = candidate.get("result_type")
    asset_id = _identifier(candidate.get("asset_id"), "Usage asset identity")
    if result_type == "image":
        asset = _project_asset_usage(
            asset_id=asset_id,
            media_kind="image",
            duration_ms=None,
            occurrences=occurrences,
            selection=selection,
        )
        return {
            **asset,
            "candidate_domain": None,
            "fully_used": bool(asset["used"]),
            "has_residual": not bool(asset["used"]),
        }
    if result_type != "video_segment":
        _fail("usage_projection_invalid", "Usage candidate type is invalid.")
    duration = candidate.get("asset_duration_ms")
    asset = _project_asset_usage(
        asset_id=asset_id,
        media_kind="video",
        duration_ms=duration if type(duration) is int else None,
        occurrences=occurrences,
        selection=selection,
    )
    domain = _interval((candidate.get("start_ms"), candidate.get("end_ms")))
    source_domain = _interval(asset["source_domain"])
    if not source_domain[0] <= domain[0] < domain[1] <= source_domain[1]:
        _fail("usage_projection_invalid", "Usage candidate exceeds its source duration.")
    used = union_half_open_intervals(asset["used_intervals"], domain=domain)  # type: ignore[arg-type]
    residual = residual_half_open_intervals(domain, used)
    return {
        **asset,
        "candidate_domain": [domain[0], domain[1]],
        "used_intervals": [[start, end] for start, end in used],
        "residual_intervals": [[start, end] for start, end in residual],
        "fully_used": bool(used) and not residual,
        "has_residual": bool(residual),
    }


def candidate_admitted(
    candidate: Mapping[str, object],
    occurrences: Sequence[Mapping[str, object]],
    policy: UsagePolicy,
) -> bool:
    if not policy.requested:
        return True
    usage = project_candidate_usage(
        candidate,
        occurrences,
        selection=policy.used_in,
    )
    if policy.used_in is not None:
        return bool(
            usage["used"]
            if candidate.get("result_type") == "image"
            else usage["used_intervals"]
        )
    if policy.residual_of is not None:
        return bool(
            candidate.get("result_type") == "video_segment"
            and candidate.get("asset_id") == policy.residual_of
            and usage["has_residual"]
        )
    if policy.unused_only or not policy.allow_reuse:
        return bool(
            not usage["used"]
            if candidate.get("result_type") == "image"
            else usage["has_residual"]
        )
    return True


def annotate_and_materialize(
    candidates: Sequence[Mapping[str, object]],
    occurrences: Sequence[Mapping[str, object]],
    *,
    usage_revision: str,
    policy: UsagePolicy,
) -> list[dict[str, object]]:
    if not policy.requested:
        return [dict(candidate) for candidate in candidates]
    _sha(usage_revision, "Usage revision")
    explode = (
        policy.unused_only
        or policy.residual_of is not None
        or policy.prefer_unused
        or not policy.allow_reuse
    )
    if policy.used_in is not None and explode:
        _fail(
            "usage_filter_invalid",
            "Scoped Usage cannot authorize a globally reusable residual identity.",
        )
    result: list[dict[str, object]] = []
    bindings: dict[str, dict[str, object]] = {}
    for raw in candidates:
        candidate = dict(raw)
        if not candidate_admitted(candidate, occurrences, policy):
            continue
        usage = project_candidate_usage(
            candidate,
            occurrences,
            selection=policy.used_in,
        )
        candidate["usage"] = usage
        explode = (
            candidate.get("result_type") == "video_segment"
            and (
                policy.unused_only
                or policy.residual_of is not None
                or policy.prefer_unused
                or not policy.allow_reuse
            )
            and bool(usage["used_intervals"])
            and bool(usage["residual_intervals"])
        )
        if not explode:
            result.append(candidate)
            continue
        residuals = usage["residual_intervals"]
        assert isinstance(residuals, list)
        if len(residuals) > MAX_RESIDUALS_PER_PARENT:
            _fail("usage_projection_too_large", "Residual projection is too large.")
        parent_usage_sha256 = canonical_usage_projection_sha256(usage)
        for raw_interval in residuals:
            source_in_ms, source_out_ms = _interval(raw_interval)
            binding = build_residual_binding(
                parent_segment_id=candidate.get("id"),
                asset_id=candidate.get("asset_id"),
                asset_sha256=candidate.get("asset_sha256"),
                asset_source_id=candidate.get("asset_source_id"),
                source_binding_sha256=candidate.get("source_binding_sha256"),
                analysis_run_id=candidate.get("analysis_run_id"),
                analysis_revision=candidate.get("analysis_revision"),
                input_asset_sha256=candidate.get("input_asset_sha256"),
                parent_start_ms=candidate.get("start_ms"),
                parent_end_ms=candidate.get("end_ms"),
                source_in_ms=source_in_ms,
                source_out_ms=source_out_ms,
                usage_revision=usage_revision,
                parent_usage_projection_sha256=parent_usage_sha256,
            )
            prior = bindings.get(str(binding["residual_id"]))
            if prior is not None and _binding_preimage(prior) != _binding_preimage(binding):
                _fail("residual_identity_collision", "Residual identity collision detected.")
            bindings[str(binding["residual_id"])] = binding
            child_usage = deepcopy(usage)
            child_usage.update(
                {
                    "candidate_domain": [source_in_ms, source_out_ms],
                    "used_intervals": [],
                    "residual_intervals": [[source_in_ms, source_out_ms]],
                    "fully_used": False,
                    "has_residual": True,
                }
            )
            canonical_usage_projection_sha256(child_usage)
            result.append(
                {
                    **candidate,
                    "id": binding["residual_id"],
                    "segment_id": binding["residual_id"],
                    "parent_segment_id": binding["parent_segment_id"],
                    "start_ms": source_in_ms,
                    "end_ms": source_out_ms,
                    "usage": child_usage,
                    "residual_binding": binding,
                    "thumbnail_url": (
                        f"/v1/video-segments/{binding['parent_segment_id']}/thumbnail"
                    ),
                }
            )
            if len(bindings) > MAX_RESIDUAL_CANDIDATES:
                _fail("usage_projection_too_large", "Residual projection is too large.")
        if policy.prefer_unused:
            result.append(candidate)
    return result


def usage_preference(candidate: Mapping[str, object]) -> int:
    usage = candidate.get("usage")
    if not isinstance(usage, Mapping):
        return 0
    if usage.get("media_kind") == "video":
        return 0 if not usage.get("used_intervals") else 1 if usage.get("has_residual") else 2
    return 0 if not usage.get("used") else 1 if usage.get("has_residual") else 2


__all__ = [
    "ResidualAuthorityError",
    "UsagePolicy",
    "UsageSelection",
    "annotate_and_materialize",
    "build_residual_binding",
    "candidate_admitted",
    "canonical_usage_projection_sha256",
    "current_candidate_usage_facts",
    "parse_usage_filters",
    "project_candidate_usage",
    "require_valid_residual_binding",
    "revalidate_residual_proof_in_connection",
    "residual_half_open_intervals",
    "source_binding_sha256",
    "union_half_open_intervals",
    "usage_preference",
]
