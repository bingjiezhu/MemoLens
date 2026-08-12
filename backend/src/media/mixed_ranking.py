from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from collections.abc import Sequence

from .mixed_search_contract import MixedSearchRequest


TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class RankedCandidate:
    score: float
    item: dict[str, object]
    matched_terms: tuple[str, ...]
    lexical_score: float


def terms(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    values = TOKEN_RE.findall(normalized)
    # CJK phrases are useful whole and as overlapping bigrams for deterministic fallback.
    for token in list(values):
        if len(token) >= 3 and any("\u3400" <= char <= "\u9fff" for char in token):
            values.extend(token[index : index + 2] for index in range(len(token) - 1))
    return list(dict.fromkeys(value for value in values if value))


def orientation(item: dict[str, object]) -> str | None:
    width = item.get("width")
    height = item.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        return None
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def candidate_text(item: dict[str, object]) -> str:
    return " ".join(
        [
            str(item.get("filename") or ""),
            str(item.get("summary") or ""),
            str(item.get("combined_text") or ""),
            *[str(tag) for tag in item.get("tags", [])],
        ]
    )


def normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def rank_candidates(
    candidates: Sequence[dict[str, object]],
    request: MixedSearchRequest,
) -> list[RankedCandidate]:
    query_terms = terms(request.query)
    normalized_query = unicodedata.normalize("NFKC", request.query).casefold()
    ranked: list[RankedCandidate] = []
    for item in candidates:
        if item.get("result_type") not in request.result_types:
            continue
        if request.orientation and orientation(item) != request.orientation:
            continue
        if item.get("result_type") == "video_segment":
            duration = int(item.get("end_ms") or 0) - int(item.get("start_ms") or 0)
            if isinstance(request.duration_min_ms, int) and duration < request.duration_min_ms:
                continue
            if isinstance(request.duration_max_ms, int) and duration > request.duration_max_ms:
                continue
        normalized_text = normalized(candidate_text(item))
        if any(term.casefold() in normalized_text for term in request.excluded_terms):
            continue
        matched_terms = tuple(term for term in query_terms if term in normalized_text)
        exact_bonus = 0.35 if normalized_query in normalized_text else 0.0
        lexical_score = min(1.0, exact_bonus + len(matched_terms) / max(len(query_terms), 1))
        ranked.append(
            RankedCandidate(
                score=lexical_score,
                item=item,
                matched_terms=matched_terms,
                lexical_score=lexical_score,
            )
        )
    ranked.sort(
        key=lambda value: (
            -value.score,
            str(value.item.get("captured_at") or ""),
            str(value.item["id"]),
        )
    )
    return ranked


def grounded_candidates(ranked: Sequence[RankedCandidate]) -> list[RankedCandidate]:
    # A score of zero is not evidence. Returning it as a creative candidate makes an
    # unrelated library look grounded and prevents the product's honest empty state.
    return [candidate for candidate in ranked if candidate.score > 0]


def select_non_overlapping(
    ranked: Sequence[RankedCandidate],
    *,
    top_k: int,
) -> list[RankedCandidate]:
    selected: list[RankedCandidate] = []
    selected_video_windows: dict[str, list[tuple[int, int]]] = {}
    for candidate in ranked:
        item = candidate.item
        if item["result_type"] == "video_segment":
            asset_id = str(item["asset_id"])
            start_ms = int(item["start_ms"])
            end_ms = int(item["end_ms"])
            windows = selected_video_windows.setdefault(asset_id, [])
            if any(start_ms <= prior_end + 250 and end_ms >= prior_start - 250 for prior_start, prior_end in windows):
                continue
            windows.append((start_ms, end_ms))
        selected.append(candidate)
        if len(selected) >= top_k:
            break
    return selected


def find_constraint_conflicts(
    candidates: Sequence[dict[str, object]],
    match_ids: list[str],
    *,
    required_terms: list[str],
    excluded_terms: list[str],
) -> list[dict[str, object]]:
    by_id = {str(item["id"]): item for item in candidates}
    conflicts: list[dict[str, object]] = []
    normalized_required = [(term, normalized(term)) for term in required_terms]
    normalized_excluded = [(term, normalized(term)) for term in excluded_terms]
    coverage = {term: False for term, _ in normalized_required}
    for match_id in match_ids:
        item = by_id.get(match_id)
        if item is None:
            continue
        text = normalized(candidate_text(item))
        for term, normalized_term in normalized_required:
            coverage[term] = coverage[term] or normalized_term in text
        present_excluded = [term for term, normalized_term in normalized_excluded if normalized_term in text]
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
