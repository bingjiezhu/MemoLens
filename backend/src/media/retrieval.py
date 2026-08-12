from __future__ import annotations

from core.media_db import MediaRepository

from .mixed_presenter import present_explicit_matches, present_search_response
from .mixed_ranking import (
    find_constraint_conflicts,
    grounded_candidates,
    rank_candidates,
    select_non_overlapping,
)
from .mixed_search_contract import parse_mixed_search_request


class MixedRetrievalService:
    """Honest lexical fallback across current images and current successful video segments."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    def search(self, payload: dict[str, object]) -> dict[str, object]:
        search_request = parse_mixed_search_request(payload)
        candidates, heads = self.repository.mixed_candidates()
        ranked = rank_candidates(candidates, search_request)
        grounded = grounded_candidates(ranked)
        selected = select_non_overlapping(grounded, top_k=search_request.top_k)
        return present_search_response(
            search_request,
            analysis_heads=heads,
            ranked=ranked,
            grounded=grounded,
            selected=selected,
        )

    def matches_for_required_terms(
        self,
        required_terms: list[str],
        *,
        excluded_terms: list[str],
        top_k: int = 24,
    ) -> list[dict[str, object]]:
        """Return a stable union whose candidates collectively cover required terms."""
        union: list[dict[str, object]] = []
        seen: set[str] = set()
        for term in required_terms:
            search = self.search(
                {
                    "query": term,
                    "types": ["image_asset", "video_segment"],
                    "top_k": top_k,
                    "filters": {"excluded_terms": excluded_terms},
                }
            )
            for item in search["results"]:
                identifier = str(item["id"])
                if identifier not in seen:
                    seen.add(identifier)
                    union.append(item)
                    if len(union) >= top_k:
                        return union
        return union

    def constraint_conflicts(
        self,
        match_ids: list[str],
        *,
        required_terms: list[str],
        excluded_terms: list[str],
    ) -> list[dict[str, object]]:
        """Evaluate authoritative brief constraints against explicit user selections."""
        candidates, _ = self.repository.mixed_candidates()
        return find_constraint_conflicts(
            candidates,
            match_ids,
            required_terms=required_terms,
            excluded_terms=excluded_terms,
        )

    def resolve_matches(self, match_ids: list[str]) -> list[dict[str, object]]:
        candidates, _ = self.repository.mixed_candidates()
        return present_explicit_matches(candidates, match_ids)
