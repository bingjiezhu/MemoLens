from __future__ import annotations

from dataclasses import dataclass
import re

from .usage_projection import UsageQueryPolicy, UsageSelection


TYPE_ALIASES = {
    "image": "image_asset",
    "image_asset": "image_asset",
    "video": "video_segment",
    "video_segment": "video_segment",
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_USAGE_FILTER_FIELDS = {
    "unused_only",
    "prefer_unused",
    "allow_reuse",
    "used_in",
    "residual_of",
}


@dataclass(frozen=True)
class MixedSearchRequest:
    query: str
    top_k: int
    result_types: frozenset[str]
    filters: dict[str, object]
    excluded_terms: tuple[str, ...]
    duration_min_ms: object
    duration_max_ms: object
    orientation: str | None
    usage: UsageQueryPolicy

    def revision_material(
        self,
        analysis_heads: dict[str, str],
        *,
        derivative_revision: str,
        usage_revision: str | None = None,
    ) -> dict[str, object]:
        material = {
            "query": self.query,
            "types": sorted(self.result_types),
            "filters": self.filters,
            "heads": analysis_heads,
            "derivative_revision": derivative_revision,
        }
        if usage_revision is not None:
            material["usage_revision"] = usage_revision
        return material


def _optional_bool(filters: dict[str, object], field: str, default: bool) -> bool:
    value = filters.get(field, default)
    if type(value) is not bool:
        raise ValueError(f"`filters.{field}` must be a boolean.")
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"`filters.{field}` must be a bounded asset or project identity.")
    return value


def _usage_selection(value: object) -> UsageSelection:
    if type(value) is str:
        return UsageSelection(project_id=_identifier(value, "used_in"))
    if type(value) is not dict or not set(value).issubset(
        {"project_id", "export_revision"}
    ) or "project_id" not in value:
        raise ValueError(
            "`filters.used_in` must be a project identity or a closed project/export selector."
        )
    revision = value.get("export_revision")
    if revision is not None and (
        type(revision) is not int or not 1 <= revision <= 1_000_000
    ):
        raise ValueError("`filters.used_in.export_revision` must be a positive integer.")
    return UsageSelection(
        project_id=_identifier(value.get("project_id"), "used_in.project_id"),
        export_revision=revision,
    )


def _usage_policy(filters: dict[str, object]) -> UsageQueryPolicy:
    requested = any(field in filters for field in _USAGE_FILTER_FIELDS)
    if not requested:
        return UsageQueryPolicy()
    unused_only = _optional_bool(filters, "unused_only", False)
    prefer_unused = _optional_bool(filters, "prefer_unused", False)
    allow_reuse = _optional_bool(filters, "allow_reuse", not unused_only)
    used_in = (
        None
        if filters.get("used_in") is None
        else _usage_selection(filters["used_in"])
    )
    residual_of = (
        None
        if filters.get("residual_of") is None
        else _identifier(filters["residual_of"], "residual_of")
    )
    if unused_only and allow_reuse:
        raise ValueError("`filters.unused_only` conflicts with `filters.allow_reuse=true`.")
    if prefer_unused and not allow_reuse:
        raise ValueError("`filters.prefer_unused` requires `filters.allow_reuse=true`.")
    if unused_only and prefer_unused:
        raise ValueError("`filters.unused_only` conflicts with `filters.prefer_unused`.")
    if used_in is not None and (
        unused_only
        or prefer_unused
        or residual_of is not None
        or not allow_reuse
    ):
        raise ValueError(
            "`filters.used_in` cannot authorize a global residual or no-reuse projection."
        )
    return UsageQueryPolicy(
        requested=True,
        unused_only=unused_only,
        prefer_unused=prefer_unused,
        allow_reuse=allow_reuse,
        used_in=used_in,
        residual_of=residual_of,
    )


def parse_mixed_search_request(payload: dict[str, object]) -> MixedSearchRequest:
    query = str(payload.get("query") or payload.get("text") or "").strip()
    if not query:
        raise ValueError("`query` must be a non-empty string.")

    top_k = payload.get("top_k", 24)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
        raise ValueError("`top_k` must be an integer from 1 to 100.")

    raw_types = payload.get("types")
    result_types = frozenset(
        TYPE_ALIASES[value]
        for value in raw_types or ["image_asset", "video_segment"]
        if isinstance(value, str) and value in TYPE_ALIASES
    )
    if not result_types:
        raise ValueError("`types` must include image/image_asset or video/video_segment.")

    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    excluded_terms = tuple(str(value).strip() for value in filters.get("excluded_terms", []) if str(value).strip())
    return MixedSearchRequest(
        query=query,
        top_k=top_k,
        result_types=result_types,
        filters=filters,
        excluded_terms=excluded_terms,
        duration_min_ms=filters.get("duration_min_ms"),
        duration_max_ms=filters.get("duration_max_ms"),
        orientation=str(filters.get("orientation") or "").strip().casefold() or None,
        usage=_usage_policy(filters),
    )
