from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from core.media_db import canonical_json

from .mixed_ranking import RankedCandidate
from .mixed_search_contract import MixedSearchRequest


def _present_candidate(
    item: dict[str, object],
    *,
    matched_terms: list[str],
    score: float,
    lexical_score: float | None,
    recency_score: float | None,
    provenance: list[str],
) -> dict[str, object]:
    is_video = item["result_type"] == "video_segment"
    return {
        "object": "creative_asset_match",
        "result_type": item["result_type"],
        "id": item["id"],
        "asset_id": item["asset_id"],
        "asset_source_id": item["asset_source_id"],
        "filename": item["filename"],
        "start_ms": item["start_ms"],
        "end_ms": item["end_ms"],
        "thumbnail_url": (
            f"/v1/video-segments/{item['id']}/thumbnail" if is_video else f"/v1/assets/{item['asset_id']}/thumbnail"
        ),
        "media_url": f"/v1/assets/{item['asset_id']}/media" if is_video else None,
        "summary": item.get("summary") or f"Local image file named {item['filename']}.",
        "matched_terms": matched_terms,
        "score": score,
        "grounded": True,
        "confidence": None,
        "analysis_run_id": item.get("analysis_run_id"),
        "analysis_revision": item.get("analysis_revision"),
        "score_components": {
            "lexical": lexical_score,
            "semantic": None,
            "recency": recency_score,
        },
        "source_availability": item["source_availability"],
        "provenance": provenance,
    }


def present_ranked_candidate(candidate: RankedCandidate) -> dict[str, object]:
    item = candidate.item
    provenance = (
        ["local_keyframe"] + (["sidecar_transcript"] if "subtitle" in str(item.get("combined_text")) else [])
        if item["result_type"] == "video_segment"
        else ["image_index"]
    )
    return _present_candidate(
        item,
        matched_terms=list(candidate.matched_terms),
        score=round(candidate.score, 6),
        lexical_score=round(candidate.lexical_score, 6),
        recency_score=0.0,
        provenance=provenance,
    )


def present_explicit_match(item: dict[str, object]) -> dict[str, object]:
    return _present_candidate(
        item,
        matched_terms=[],
        score=1.0,
        lexical_score=None,
        recency_score=None,
        provenance=["explicit_user_selection"],
    )


def present_explicit_matches(
    candidates: Sequence[dict[str, object]],
    match_ids: list[str],
) -> list[dict[str, object]]:
    by_id = {str(item["id"]): item for item in candidates}
    return [present_explicit_match(by_id[match_id]) for match_id in match_ids if match_id in by_id]


def present_search_response(
    request: MixedSearchRequest,
    *,
    analysis_heads: dict[str, str],
    ranked: Sequence[RankedCandidate],
    grounded: Sequence[RankedCandidate],
    selected: Sequence[RankedCandidate],
) -> dict[str, object]:
    results = [present_ranked_candidate(candidate) for candidate in selected]
    revision_json = canonical_json(request.revision_material(analysis_heads))
    return {
        "object": "mixed.search",
        "schema_version": "1",
        "id": f"search_{uuid.uuid4().hex}",
        "status": "succeeded",
        "query": request.query,
        "search_revision": hashlib.sha256(revision_json.encode()).hexdigest(),
        "analysis_heads": analysis_heads,
        "results": results,
        "data": results,
        "candidate_count": len(grounded),
        "considered_count": len(ranked),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retrieval_mode": "lexical_local_fallback",
        "semantic_available": False,
        "external_analysis": False,
    }
