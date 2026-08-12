"""Validate and compact read-only Atlas API responses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memolens_contracts import MemoLensError, compact_asset, safety_summary


def present_memories(
    payload: dict[str, Any],
    *,
    library_dir: Path | None,
    query: str | None,
    limit: int,
    local_api: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("object") != "atlas.workbench":
        raise MemoLensError(
            "MemoLens Atlas returned an unexpected object type.",
            code="invalid_response",
        )
    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list):
        raise MemoLensError(
            "MemoLens Atlas did not return a memory list.",
            code="invalid_response",
        )
    memories = [
        _memory_card(raw, library_dir)
        for raw in raw_memories[:limit]
        if isinstance(raw, dict)
    ]
    return {
        "object": "memolens.memories",
        "status": "completed",
        "source": "local_api",
        "mode": "opt_in_local_api",
        "local_api": local_api,
        "query": query.strip() if query and query.strip() else None,
        "memory_count": len(memories),
        "memories": memories,
        "suggested_queries": payload.get("suggested_queries", []),
        "storylines": payload.get("storylines", []),
        "safety": safety_summary(),
    }


def _memory_card(raw: dict[str, Any], library_dir: Path | None) -> dict[str, Any]:
    card = {
        key: raw.get(key)
        for key in (
            "id",
            "kind",
            "label",
            "asset_count",
            "top_concepts",
            "place_label",
            "time_label",
            "score",
            "duplicate_count",
            "chapter_count",
        )
        if raw.get(key) is not None
    }
    representatives = raw.get("representative_assets")
    if not isinstance(representatives, list):
        representatives = raw.get("best_assets")
    card["representative_assets"] = [
        compact_asset(asset, library_dir)
        for asset in (representatives or [])[:5]
        if isinstance(asset, dict)
    ]
    return card


def present_cleanup(
    payload: dict[str, Any],
    *,
    library_dir: Path | None,
    local_api: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("object") != "atlas.cleanup":
        raise MemoLensError(
            "MemoLens cleanup report returned an unexpected object type.",
            code="invalid_response",
        )
    stacks = [
        _cleanup_stack(raw, library_dir)
        for raw in payload.get("stacks", [])[:48]
        if isinstance(raw, dict)
    ]
    return {
        "object": "memolens.cleanup_report",
        "status": "completed",
        "source": "local_api",
        "mode": "opt_in_local_api",
        "local_api": local_api,
        "read_only": True,
        "counts": {
            key: payload.get(key, 0)
            for key in (
                "duplicate_stack_count",
                "similar_stack_count",
                "low_quality_count",
                "missing_time_count",
                "missing_place_count",
                "people_review_count",
            )
        },
        "stacks": stacks,
        "low_quality_assets": _asset_list(payload, "low_quality_assets", library_dir),
        "missing_time_assets": _asset_list(payload, "missing_time_assets", library_dir),
        "missing_place_assets": _asset_list(
            payload, "missing_place_assets", library_dir
        ),
        "people_review_assets": _asset_list(
            payload, "people_review_assets", library_dir
        ),
        "warning": "Review candidates only. No file was deleted, moved, or modified.",
        "safety": safety_summary(),
    }


def _cleanup_stack(raw: dict[str, Any], library_dir: Path | None) -> dict[str, Any]:
    stack = {
        key: raw.get(key)
        for key in (
            "id",
            "kind",
            "count",
            "asset_ids",
            "representative_asset_id",
            "best_asset_id",
            "score",
            "reason",
        )
        if raw.get(key) is not None
    }
    stack["assets"] = [
        compact_asset(asset, library_dir)
        for asset in raw.get("assets", [])[:8]
        if isinstance(asset, dict)
    ]
    if isinstance(raw.get("best_asset"), dict):
        stack["best_asset"] = compact_asset(raw["best_asset"], library_dir)
    return stack


def _asset_list(
    payload: dict[str, Any], key: str, library_dir: Path | None
) -> list[dict[str, Any]]:
    raw_items = payload.get(key, [])
    if not isinstance(raw_items, list):
        return []
    return [
        compact_asset(item, library_dir)
        for item in raw_items[:24]
        if isinstance(item, dict)
    ]
