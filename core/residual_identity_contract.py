"""Closed, deterministic identity contract for exact residual video material.

Residual identities are read projections over a current persisted parent
segment.  They never authorize a synthetic ``video_segments`` row and never
contain a path, display name, host, score, or wall-clock value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import re


RESIDUAL_BINDING_OBJECT = "memolens.residual_binding"
RESIDUAL_CONTRACT_VERSION = "1"
MAX_SOURCE_MS = 7 * 24 * 60 * 60 * 1000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESIDUAL_ID = re.compile(r"^rseg_[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_ASSET_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_PARENT_SEGMENT_ID = re.compile(
    r"^seg_(?P<asset_digest>[0-9a-f]{24})_"
    r"(?P<analysis_revision>[1-9][0-9]{0,6})_"
    r"(?:0|[1-9][0-9]{0,6})$"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

_RESIDUAL_BINDING_FIELDS = frozenset(
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
_SOURCE_BINDING_FIELDS = frozenset(
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
_USAGE_PROJECTION_FIELDS = frozenset(
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


class ResidualIdentityContractError(ValueError):
    """Stable fail-closed error for malformed or substituted residual facts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _fail(code: str, message: str) -> None:
    raise ResidualIdentityContractError(code, message)


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("residual_binding_invalid", f"{label} must be a lowercase SHA-256 digest.")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail("residual_binding_invalid", f"{label} is invalid.")
    return value


def _interval(value: object, label: str) -> tuple[int, int]:
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
            "residual_binding_invalid",
            f"{label} must be a bounded integer half-open interval.",
        )
    return int(value[0]), int(value[1])


def source_binding_sha256(value: Mapping[str, object]) -> str:
    """Hash the exact, path-free source-row identity used by a residual."""

    if type(value) is not dict or set(value) != _SOURCE_BINDING_FIELDS:
        _fail("source_binding_invalid", "Source binding facts must use the closed contract.")
    asset_source_id = value.get("asset_source_id")
    asset_id = value.get("asset_id")
    asset_sha256 = value.get("asset_sha256")
    library_root_id = value.get("library_root_id")
    observed_size = value.get("observed_size")
    observed_mtime_ns = value.get("observed_mtime_ns")
    source_file_id = value.get("source_file_id")
    if type(asset_source_id) is not str or _ASSET_SOURCE_ID.fullmatch(asset_source_id) is None:
        _fail("source_binding_invalid", "Source identity is invalid.")
    if type(asset_id) is not str or _ASSET_ID.fullmatch(asset_id) is None:
        _fail("source_binding_invalid", "Source asset identity is invalid.")
    digest = _sha(asset_sha256, "Source asset digest")
    if asset_id != f"asset_{digest[:24]}":
        _fail("source_binding_invalid", "Source asset identity does not match its digest.")
    _identifier(library_root_id, "Library root identity")
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
    preimage = {
        "object": "memolens.residual_source_binding",
        "contract_version": RESIDUAL_CONTRACT_VERSION,
        **dict(value),
    }
    return canonical_sha256(preimage)


def _usage_intervals(value: object, label: str) -> list[tuple[int, int]]:
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
    """Validate and hash the full 11-field candidate Usage projection."""

    if type(value) is not dict or set(value) != _USAGE_PROJECTION_FIELDS:
        _fail("usage_projection_invalid", "Usage projection must use the closed contract.")
    asset_id = value.get("asset_id")
    if type(asset_id) is not str or _ASSET_ID.fullmatch(asset_id) is None:
        _fail("usage_projection_invalid", "Usage asset identity is invalid.")
    if value.get("media_kind") != "video":
        _fail("usage_projection_invalid", "Residual Usage projection must be video.")
    occurrence_count = value.get("occurrence_count")
    if type(occurrence_count) is not int or not 0 <= occurrence_count <= 1_000_000:
        _fail("usage_projection_invalid", "Usage occurrence count is invalid.")
    for field in ("used", "fully_used", "has_residual"):
        if type(value.get(field)) is not bool:
            _fail("usage_projection_invalid", f"Usage {field} must be boolean.")
    used_in = value.get("used_in")
    if type(used_in) is not list or len(used_in) > 1_000_000:
        _fail("usage_projection_invalid", "Usage provenance is invalid.")
    normalized_used_in: list[tuple[str, int]] = []
    for row in used_in:
        if type(row) is not dict or set(row) != {"project_id", "export_revision"}:
            _fail("usage_projection_invalid", "Usage provenance row is invalid.")
        project_id = _identifier(row.get("project_id"), "Usage project identity")
        revision = row.get("export_revision")
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            _fail("usage_projection_invalid", "Usage export revision is invalid.")
        normalized_used_in.append((project_id, revision))
    if normalized_used_in != sorted(set(normalized_used_in)):
        _fail("usage_projection_invalid", "Usage provenance must be unique and sorted.")
    if value["used"] != (occurrence_count > 0) or bool(normalized_used_in) != value["used"]:
        _fail("usage_projection_invalid", "Usage occurrence facts are inconsistent.")

    source_domain = _interval(value.get("source_domain"), "Usage source domain")
    if source_domain[0] != 0:
        _fail("usage_projection_invalid", "Usage source domain must start at zero.")
    candidate_domain = _interval(value.get("candidate_domain"), "Usage candidate domain")
    if candidate_domain[0] < source_domain[0] or candidate_domain[1] > source_domain[1]:
        _fail("usage_projection_invalid", "Usage candidate exceeds the source domain.")
    used = _usage_intervals(value.get("used_intervals"), "Usage used intervals")
    residual = _usage_intervals(
        value.get("residual_intervals"),
        "Usage residual intervals",
    )
    for start, end in [*used, *residual]:
        if start < candidate_domain[0] or end > candidate_domain[1]:
            _fail("usage_projection_invalid", "Usage interval exceeds the candidate domain.")
    partition = [(start, end, "used") for start, end in used]
    partition.extend((start, end, "residual") for start, end in residual)
    partition.sort(key=lambda row: (row[0], row[1], row[2]))
    cursor = candidate_domain[0]
    for start, end, _kind in partition:
        if start != cursor:
            _fail("usage_projection_invalid", "Usage intervals do not exactly partition the candidate.")
        cursor = end
    if cursor != candidate_domain[1]:
        _fail("usage_projection_invalid", "Usage intervals do not cover the candidate domain.")
    if value["fully_used"] != (bool(used) and not residual):
        _fail("usage_projection_invalid", "Usage fully-used flag is inconsistent.")
    if value["has_residual"] != bool(residual):
        _fail("usage_projection_invalid", "Usage residual flag is inconsistent.")
    return canonical_sha256(dict(value))


def _binding_preimage(binding: Mapping[str, object]) -> dict[str, object]:
    return {key: binding[key] for key in sorted(_RESIDUAL_BINDING_FIELDS - {"residual_id"})}


def require_valid_residual_binding(value: Mapping[str, object]) -> dict[str, object]:
    """Return a canonical copy of one exact residual binding or fail closed."""

    if type(value) is not dict or set(value) != _RESIDUAL_BINDING_FIELDS:
        _fail("residual_binding_invalid", "Residual binding must use the closed contract.")
    if value.get("object") != RESIDUAL_BINDING_OBJECT:
        _fail("residual_binding_invalid", "Residual binding object is invalid.")
    if value.get("contract_version") != RESIDUAL_CONTRACT_VERSION:
        _fail("residual_binding_invalid", "Residual contract version is invalid.")
    residual_id = value.get("residual_id")
    if type(residual_id) is not str or _RESIDUAL_ID.fullmatch(residual_id) is None:
        _fail("residual_binding_invalid", "Residual identity is invalid.")
    parent_segment_id = value.get("parent_segment_id")
    parent_match = (
        _PARENT_SEGMENT_ID.fullmatch(parent_segment_id)
        if type(parent_segment_id) is str
        else None
    )
    if parent_match is None:
        _fail("residual_binding_invalid", "Parent segment identity is invalid.")
    asset_id = value.get("asset_id")
    if type(asset_id) is not str or _ASSET_ID.fullmatch(asset_id) is None:
        _fail("residual_binding_invalid", "Residual asset identity is invalid.")
    asset_sha256 = _sha(value.get("asset_sha256"), "Residual asset digest")
    if asset_id != f"asset_{asset_sha256[:24]}":
        _fail("residual_binding_invalid", "Residual asset identity does not match its digest.")
    if parent_match.group("asset_digest") != asset_sha256[:24]:
        _fail("residual_binding_invalid", "Parent segment belongs to another asset.")
    asset_source_id = value.get("asset_source_id")
    if type(asset_source_id) is not str or _ASSET_SOURCE_ID.fullmatch(asset_source_id) is None:
        _fail("residual_binding_invalid", "Residual source identity is invalid.")
    _sha(value.get("source_binding_sha256"), "Residual source binding digest")
    analysis_run_id = value.get("analysis_run_id")
    if type(analysis_run_id) is not str or _ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None:
        _fail("residual_binding_invalid", "Residual analysis run identity is invalid.")
    analysis_revision = value.get("analysis_revision")
    if type(analysis_revision) is not int or not 1 <= analysis_revision <= 1_000_000:
        _fail("residual_binding_invalid", "Residual analysis revision is invalid.")
    if int(parent_match.group("analysis_revision")) != analysis_revision:
        _fail("residual_binding_invalid", "Parent segment analysis revision is inconsistent.")
    input_asset_sha256 = _sha(
        value.get("input_asset_sha256"),
        "Residual analysis input digest",
    )
    if input_asset_sha256 != asset_sha256:
        _fail("residual_binding_invalid", "Residual analysis input does not match the asset.")
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
    _sha(
        value.get("parent_usage_projection_sha256"),
        "Residual parent Usage projection digest",
    )
    expected_id = f"rseg_{canonical_sha256(_binding_preimage(value))}"
    if residual_id != expected_id:
        _fail("residual_identity_mismatch", "Residual identity does not match its binding.")
    return json.loads(canonical_json(dict(value)))


def build_residual_binding(
    *,
    parent_segment_id: str,
    asset_id: str,
    asset_sha256: str,
    asset_source_id: str,
    source_binding_sha256: str,
    analysis_run_id: str,
    analysis_revision: int,
    input_asset_sha256: str,
    parent_start_ms: int,
    parent_end_ms: int,
    source_in_ms: int,
    source_out_ms: int,
    usage_revision: str,
    parent_usage_projection_sha256: str,
) -> dict[str, object]:
    preimage: dict[str, object] = {
        "object": RESIDUAL_BINDING_OBJECT,
        "contract_version": RESIDUAL_CONTRACT_VERSION,
        "parent_segment_id": parent_segment_id,
        "asset_id": asset_id,
        "asset_sha256": asset_sha256,
        "asset_source_id": asset_source_id,
        "source_binding_sha256": source_binding_sha256,
        "analysis_run_id": analysis_run_id,
        "analysis_revision": analysis_revision,
        "input_asset_sha256": input_asset_sha256,
        "parent_start_ms": parent_start_ms,
        "parent_end_ms": parent_end_ms,
        "source_in_ms": source_in_ms,
        "source_out_ms": source_out_ms,
        "usage_revision": usage_revision,
        "parent_usage_projection_sha256": parent_usage_projection_sha256,
    }
    binding = {
        **preimage,
        "residual_id": f"rseg_{canonical_sha256(preimage)}",
    }
    return require_valid_residual_binding(binding)


def residual_binding_sha256(value: Mapping[str, object]) -> str:
    return canonical_sha256(require_valid_residual_binding(value))


def require_unique_residual_bindings(
    values: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Reject a cryptographic ID collision instead of merging two preimages."""

    normalized: list[dict[str, object]] = []
    preimages: dict[str, str] = {}
    for value in values:
        binding = require_valid_residual_binding(value)
        residual_id = str(binding["residual_id"])
        preimage = canonical_json(_binding_preimage(binding))
        prior = preimages.get(residual_id)
        if prior is not None and prior != preimage:
            _fail(
                "residual_identity_collision",
                "One residual identity resolves to multiple canonical preimages.",
            )
        preimages[residual_id] = preimage
        normalized.append(binding)
    return normalized


__all__ = [
    "MAX_SOURCE_MS",
    "RESIDUAL_BINDING_OBJECT",
    "RESIDUAL_CONTRACT_VERSION",
    "ResidualIdentityContractError",
    "build_residual_binding",
    "canonical_json",
    "canonical_sha256",
    "canonical_usage_projection_sha256",
    "require_unique_residual_bindings",
    "require_valid_residual_binding",
    "residual_binding_sha256",
    "source_binding_sha256",
]
