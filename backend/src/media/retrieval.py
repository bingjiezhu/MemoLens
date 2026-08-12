from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone

from core.media_db import MediaRepository, canonical_json


TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _terms(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    values = TOKEN_RE.findall(normalized)
    # CJK phrases are useful whole and as overlapping bigrams for deterministic fallback.
    for token in list(values):
        if len(token) >= 3 and any("\u3400" <= char <= "\u9fff" for char in token):
            values.extend(token[index : index + 2] for index in range(len(token) - 1))
    return list(dict.fromkeys(value for value in values if value))


def _orientation(item: dict[str, object]) -> str | None:
    width = item.get("width")
    height = item.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def _candidate_text(item: dict[str, object]) -> str:
    return " ".join(
        [
            str(item.get("filename") or ""),
            str(item.get("summary") or ""),
            str(item.get("combined_text") or ""),
            *[str(tag) for tag in item.get("tags", [])],
        ]
    )


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


class MixedRetrievalService:
    """Honest lexical fallback across current images and current successful video segments."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    def search(self, payload: dict[str, object]) -> dict[str, object]:
        query = str(payload.get("query") or payload.get("text") or "").strip()
        if not query:
            raise ValueError("`query` must be a non-empty string.")
        top_k = payload.get("top_k", 24)
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 100:
            raise ValueError("`top_k` must be an integer from 1 to 100.")
        raw_types = payload.get("types")
        aliases = {
            "image": "image_asset",
            "image_asset": "image_asset",
            "video": "video_segment",
            "video_segment": "video_segment",
        }
        types = {
            aliases[value]
            for value in raw_types or ["image_asset", "video_segment"]
            if isinstance(value, str) and value in aliases
        }
        if not types:
            raise ValueError("`types` must include image/image_asset or video/video_segment.")
        filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
        excluded = [str(value).strip() for value in filters.get("excluded_terms", []) if str(value).strip()]
        duration_min = filters.get("duration_min_ms")
        duration_max = filters.get("duration_max_ms")
        orientation = str(filters.get("orientation") or "").strip().casefold() or None

        candidates, heads = self.repository.mixed_candidates()
        query_terms = _terms(query)
        ranked: list[tuple[float, dict[str, object], list[str], float]] = []
        for item in candidates:
            if item.get("result_type") not in types:
                continue
            if orientation and _orientation(item) != orientation:
                continue
            if item.get("result_type") == "video_segment":
                duration = int(item.get("end_ms") or 0) - int(item.get("start_ms") or 0)
                if isinstance(duration_min, int) and duration < duration_min:
                    continue
                if isinstance(duration_max, int) and duration > duration_max:
                    continue
            normalized = _normalized(_candidate_text(item))
            if any(term.casefold() in normalized for term in excluded):
                continue
            matched = [term for term in query_terms if term in normalized]
            exact_bonus = 0.35 if unicodedata.normalize("NFKC", query).casefold() in normalized else 0.0
            lexical = min(1.0, exact_bonus + len(matched) / max(len(query_terms), 1))
            ranked.append((lexical, item, matched, lexical))
        ranked.sort(
            key=lambda value: (-value[0], str(value[1].get("captured_at") or ""), str(value[1]["id"])), reverse=False
        )

        # A score of zero is not evidence. Returning it as a creative candidate makes an
        # unrelated library look grounded and prevents the product's honest empty state.
        grounded_ranked = [entry for entry in ranked if entry[0] > 0]
        results: list[dict[str, object]] = []
        selected_video_windows: dict[str, list[tuple[int, int]]] = {}
        for score, item, matched, lexical in grounded_ranked:
            if item["result_type"] == "video_segment":
                asset_id = str(item["asset_id"])
                start_ms = int(item["start_ms"])
                end_ms = int(item["end_ms"])
                windows = selected_video_windows.setdefault(asset_id, [])
                if any(start_ms <= prior_end + 250 and end_ms >= prior_start - 250 for prior_start, prior_end in windows):
                    continue
                windows.append((start_ms, end_ms))
            result = {
                "object": "creative_asset_match",
                "result_type": item["result_type"],
                "id": item["id"],
                "asset_id": item["asset_id"],
                "asset_source_id": item["asset_source_id"],
                "filename": item["filename"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "thumbnail_url": (
                    f"/v1/video-segments/{item['id']}/thumbnail"
                    if item["result_type"] == "video_segment"
                    else f"/v1/assets/{item['asset_id']}/thumbnail"
                ),
                "media_url": (
                    f"/v1/assets/{item['asset_id']}/media" if item["result_type"] == "video_segment" else None
                ),
                "summary": item.get("summary") or f"Local image file named {item['filename']}.",
                "matched_terms": matched,
                "score": round(score, 6),
                "grounded": True,
                "confidence": None,
                "analysis_run_id": item.get("analysis_run_id"),
                "analysis_revision": item.get("analysis_revision"),
                "score_components": {"lexical": round(lexical, 6), "semantic": None, "recency": 0.0},
                "source_availability": item["source_availability"],
                "provenance": (
                    ["local_keyframe"]
                    + (["sidecar_transcript"] if "subtitle" in str(item.get("combined_text")) else [])
                    if item["result_type"] == "video_segment"
                    else ["image_index"]
                ),
            }
            results.append(result)
            if len(results) >= top_k:
                break
        revision_material = {"query": query, "types": sorted(types), "filters": filters, "heads": heads}
        search_revision = hashlib.sha256(canonical_json(revision_material).encode()).hexdigest()
        return {
            "object": "mixed.search",
            "schema_version": "1",
            "id": f"search_{uuid.uuid4().hex}",
            "status": "succeeded",
            "query": query,
            "search_revision": search_revision,
            "analysis_heads": heads,
            "results": results,
            "data": results,
            "candidate_count": len(grounded_ranked),
            "considered_count": len(ranked),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "retrieval_mode": "lexical_local_fallback",
            "semantic_available": False,
            "external_analysis": False,
        }

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
        by_id = {str(item["id"]): item for item in candidates}
        conflicts: list[dict[str, object]] = []
        normalized_required = [(term, _normalized(term)) for term in required_terms]
        normalized_excluded = [(term, _normalized(term)) for term in excluded_terms]
        coverage = {term: False for term, _ in normalized_required}
        for match_id in match_ids:
            item = by_id.get(match_id)
            if item is None:
                continue
            text = _normalized(_candidate_text(item))
            for term, normalized in normalized_required:
                coverage[term] = coverage[term] or normalized in text
            present_excluded = [term for term, normalized in normalized_excluded if normalized in text]
            if present_excluded:
                conflicts.append(
                    {
                        "candidate_ref": match_id,
                        "missing_required_terms": [],
                        "matched_excluded_terms": present_excluded,
                    }
                )
        missing_required = [term for term, covered in coverage.items() if not covered]
        if missing_required:
            conflicts.append(
                {
                    "candidate_ref": None,
                    "missing_required_terms": missing_required,
                    "matched_excluded_terms": [],
                }
            )
        return conflicts

    def resolve_matches(self, match_ids: list[str]) -> list[dict[str, object]]:
        candidates, _ = self.repository.mixed_candidates()
        by_id = {str(item["id"]): item for item in candidates}
        resolved: list[dict[str, object]] = []
        for match_id in match_ids:
            item = by_id.get(match_id)
            if item is None:
                continue
            resolved.append(
                {
                    "object": "creative_asset_match",
                    "result_type": item["result_type"],
                    "id": item["id"],
                    "asset_id": item["asset_id"],
                    "asset_source_id": item["asset_source_id"],
                    "filename": item["filename"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "thumbnail_url": (
                        f"/v1/video-segments/{item['id']}/thumbnail"
                        if item["result_type"] == "video_segment"
                        else f"/v1/assets/{item['asset_id']}/thumbnail"
                    ),
                    "media_url": (
                        f"/v1/assets/{item['asset_id']}/media" if item["result_type"] == "video_segment" else None
                    ),
                    "summary": item.get("summary") or f"Local image file named {item['filename']}.",
                    "matched_terms": [],
                    "score": 1.0,
                    "grounded": True,
                    "confidence": None,
                    "analysis_run_id": item.get("analysis_run_id"),
                    "analysis_revision": item.get("analysis_revision"),
                    "score_components": {"lexical": None, "semantic": None, "recency": None},
                    "source_availability": item["source_availability"],
                    "provenance": ["explicit_user_selection"],
                }
            )
        return resolved
