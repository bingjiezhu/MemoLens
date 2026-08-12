"""Deterministic bounded lexical ranking shared by photo and video readers."""

from __future__ import annotations

import heapq
from typing import Any


RankedRow = tuple[float, str, int, dict[str, Any]]


def lexical_score(
    fields: dict[str, str],
    *,
    query: str,
    phrase: str,
    terms: list[str],
    weights: dict[str, float],
) -> tuple[float, list[str]]:
    blob = " ".join(fields.values())
    score = 8.0 if phrase in blob else 0.0
    matched = [query] if score else []
    for term in terms:
        term_score = 0.0
        for names, weight in weights.items():
            if any(term in fields[name] for name in names.split("|")):
                term_score += weight
        if term_score:
            score += term_score
            matched.append(term)
    return score, matched


def keep_best(heap: list[RankedRow], candidate: RankedRow, limit: int) -> None:
    """Keep at most ``limit`` candidates without materializing the full index."""

    if len(heap) < limit:
        heapq.heappush(heap, candidate)
    elif candidate[:3] > heap[0][:3]:
        heapq.heapreplace(heap, candidate)
