from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from core.media_db import canonical_json

from .mixed_ranking import RankedCandidate, candidate_is_groundable
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
    residual_binding = item.get("residual_binding")
    is_residual = is_video and isinstance(residual_binding, dict)
    parent_segment_id = (
        str(residual_binding.get("parent_segment_id"))
        if is_residual
        else str(item["id"])
    )
    raw_review = item.get("review")
    review = raw_review if isinstance(raw_review, dict) else {}
    presented = {
        "object": "creative_asset_match",
        "result_type": item["result_type"],
        "id": item["id"],
        "asset_id": item["asset_id"],
        "asset_source_id": item["asset_source_id"],
        "filename": item["filename"],
        "start_ms": item["start_ms"],
        "end_ms": item["end_ms"],
        "thumbnail_url": (
            f"/v1/video-segments/{parent_segment_id}/thumbnail"
            if is_video
            else f"/v1/assets/{item['asset_id']}/thumbnail"
        ),
        "media_url": f"/v1/assets/{item['asset_id']}/media" if is_video else None,
        "summary": item.get("summary") or f"Local image file named {item['filename']}.",
        "matched_terms": matched_terms,
        "score": score,
        "grounded": candidate_is_groundable(item),
        "confidence": None,
        "analysis_run_id": item.get("analysis_run_id"),
        "analysis_revision": item.get("analysis_revision"),
        "analysis_status": item.get("analysis_status", "current" if is_video else "unknown"),
        "score_components": {
            "lexical": lexical_score,
            "semantic": None,
            "recency": recency_score,
        },
        "source_availability": item["source_availability"],
        "review": {
            "revision": int(review.get("revision") or 0),
            "inbox_state": str(review.get("inbox_state") or "inbox"),
            "favorite": bool(review.get("favorite")),
            "project_ready": bool(review.get("project_ready")),
        },
        "provenance": provenance,
    }
    if item.get("asset_sha256") is not None:
        presented["asset_sha256"] = item["asset_sha256"]
    if is_residual:
        presented["parent_segment_id"] = parent_segment_id
        presented["residual_binding"] = residual_binding
    if not is_video:
        presented["canonical_image_observation"] = item.get(
            "canonical_image_observation"
        )
    usage = item.get("canonical_usage")
    if isinstance(usage, dict):
        presented["usage"] = {
            "asset_id": usage.get("asset_id"),
            "media_kind": usage.get("media_kind"),
            "occurrence_count": usage.get("occurrence_count"),
            "used": usage.get("used"),
            "used_in": usage.get("used_in"),
            "source_domain": usage.get("source_domain"),
            "candidate_domain": usage.get("candidate_domain"),
            "used_intervals": usage.get("used_intervals"),
            "residual_intervals": usage.get("residual_intervals"),
            "fully_used": usage.get("fully_used"),
            "has_residual": usage.get("has_residual"),
        }
    return presented


def present_ranked_candidate(candidate: RankedCandidate) -> dict[str, object]:
    item = candidate.item
    if item["result_type"] == "video_segment":
        provenance = ["local_keyframe"] + (
            ["sidecar_transcript"]
            if "subtitle" in str(item.get("combined_text"))
            else []
        )
    elif item.get("analysis_status") == "current":
        provenance = ["canonical_image_analysis", "verified_image_projection"]
    else:
        provenance = ["local_file_identity"]
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
    derivative_revision: str,
    usage_revision: str | None = None,
) -> dict[str, object]:
    results = [present_ranked_candidate(candidate) for candidate in selected]
    grounded_ids = {id(candidate) for candidate in grounded}
    audit_results = [
        present_ranked_candidate(candidate)
        for candidate in ranked
        if candidate.score > 0 and id(candidate) not in grounded_ids
    ]
    revision_json = canonical_json(
        request.revision_material(
            analysis_heads,
            derivative_revision=derivative_revision,
            usage_revision=usage_revision,
        )
    )
    response = {
        "object": "mixed.search",
        "schema_version": "1",
        "id": f"search_{uuid.uuid4().hex}",
        "status": "succeeded",
        "query": request.query,
        "search_revision": hashlib.sha256(revision_json.encode()).hexdigest(),
        "derivative_revision": derivative_revision,
        "analysis_heads": analysis_heads,
        "results": results,
        "data": results,
        "audit_results": audit_results,
        "audit_count": len(audit_results),
        "candidate_count": len(grounded),
        "considered_count": len(ranked),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retrieval_mode": "lexical_local_fallback",
        "semantic_available": False,
        "external_analysis": False,
    }
    if usage_revision is not None:
        response["usage_revision"] = usage_revision
    return response
