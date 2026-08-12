from __future__ import annotations

from dataclasses import dataclass


TYPE_ALIASES = {
    "image": "image_asset",
    "image_asset": "image_asset",
    "video": "video_segment",
    "video_segment": "video_segment",
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

    def revision_material(self, analysis_heads: dict[str, str]) -> dict[str, object]:
        return {
            "query": self.query,
            "types": sorted(self.result_types),
            "filters": self.filters,
            "heads": analysis_heads,
        }


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
    )
