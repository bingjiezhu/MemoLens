from __future__ import annotations

import re


def expand_text_with_hints(
    text: str,
    semantic_hints: dict[str, list[str]],
) -> str:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return ""
    if not semantic_hints:
        return normalized_text

    haystack = _normalize_text(normalized_text)
    expanded_terms: list[str] = []

    for trigger, related_terms in semantic_hints.items():
        normalized_trigger = _normalize_text(trigger)
        if not normalized_trigger:
            continue
        if normalized_trigger not in haystack:
            continue

        for term in related_terms:
            normalized_term = str(term).strip()
            if normalized_term and normalized_term not in expanded_terms:
                expanded_terms.append(normalized_term)

    if not expanded_terms:
        return normalized_text

    return f"{normalized_text} Related concepts: {', '.join(expanded_terms)}."


def normalize_semantic_hints(raw_value: object) -> dict[str, list[str]]:
    if not isinstance(raw_value, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    for key, value in raw_value.items():
        normalized_key = str(key).strip()
        if not normalized_key:
            continue
        if not isinstance(value, list):
            continue

        normalized_values: list[str] = []
        for item in value:
            normalized_item = str(item).strip()
            if normalized_item and normalized_item not in normalized_values:
                normalized_values.append(normalized_item)

        if normalized_values:
            normalized[normalized_key] = normalized_values

    return normalized


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.strip().lower())).strip()
