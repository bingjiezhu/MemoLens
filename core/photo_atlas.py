from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

import numpy as np

from .db import ImageIndexRepository
from .image_quality import metadata_quality_score


ATLAS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS atlas_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    layout_version TEXT NOT NULL,
    image_count INTEGER NOT NULL,
    cluster_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    message TEXT
);

CREATE TABLE IF NOT EXISTS atlas_assets (
    image_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    taken_at TEXT,
    place_name TEXT,
    country TEXT,
    description TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    combined_text TEXT NOT NULL,
    embedding_backend TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    cluster_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    duplicate_group_id TEXT,
    neighbor_ids_json TEXT NOT NULL,
    quality_score REAL NOT NULL,
    technical_quality_score REAL,
    people_risk REAL NOT NULL,
    lat REAL,
    lon REAL,
    layout_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_clusters (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    label TEXT NOT NULL,
    count INTEGER NOT NULL,
    representative_image_id TEXT,
    top_concepts_json TEXT NOT NULL,
    place_label TEXT,
    time_label TEXT,
    x REAL NOT NULL,
    y REAL NOT NULL,
    bounds_json TEXT NOT NULL,
    layout_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_edges (
    id TEXT PRIMARY KEY,
    source_image_id TEXT NOT NULL,
    target_image_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    weight REAL NOT NULL,
    layout_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    asset_ids_json TEXT NOT NULL,
    representative_ids_json TEXT NOT NULL,
    top_concepts_json TEXT NOT NULL,
    place_label TEXT,
    time_label TEXT,
    x REAL NOT NULL,
    y REAL NOT NULL,
    score REAL NOT NULL,
    people_risk REAL NOT NULL,
    duplicate_count INTEGER NOT NULL,
    chapter_count INTEGER NOT NULL,
    layout_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_stacks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    asset_ids_json TEXT NOT NULL,
    representative_image_id TEXT,
    best_image_id TEXT,
    score REAL NOT NULL,
    reason TEXT NOT NULL,
    layout_version TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS atlas_roles (
    image_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    role TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (image_id, memory_id, role)
);

CREATE TABLE IF NOT EXISTS atlas_baskets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    asset_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_atlas_assets_cluster
    ON atlas_assets(cluster_id);

CREATE INDEX IF NOT EXISTS idx_atlas_assets_duplicate
    ON atlas_assets(duplicate_group_id);

CREATE INDEX IF NOT EXISTS idx_atlas_clusters_mode
    ON atlas_clusters(mode);

CREATE INDEX IF NOT EXISTS idx_atlas_edges_source
    ON atlas_edges(source_image_id);

CREATE INDEX IF NOT EXISTS idx_atlas_feedback_target
    ON atlas_feedback(target_kind, target_id);

CREATE INDEX IF NOT EXISTS idx_atlas_memories_kind
    ON atlas_memories(kind);

CREATE INDEX IF NOT EXISTS idx_atlas_stacks_kind
    ON atlas_stacks(kind);

CREATE INDEX IF NOT EXISTS idx_atlas_roles_memory
    ON atlas_roles(memory_id);
"""

SUPPORTED_MODES = {
    "semantic",
    "time",
    "place",
    "event",
    "people",
    "quality",
    "duplicates",
}
SUPPORTED_LENSES = {
    "explore",
    "story",
    "map",
    "people",
    "cleanup",
    "similar",
}
DEFAULT_MODE = "semantic"
DEFAULT_LENS = "explore"
DEFAULT_LAYOUT_VERSION = "atlas_pca_kmeans_v1"
NEIGHBOR_COUNT = 8
NEAR_DUPLICATE_THRESHOLD = 0.88
EDGE_SIMILARITY_FLOOR = 0.62
DEFAULT_VISIBLE_LIMIT = 900
MAX_VISIBLE_LIMIT = 1800

HUMAN_TERMS = {
    "adult",
    "baby",
    "barista",
    "boy",
    "child",
    "children",
    "couple",
    "crowd",
    "customer",
    "face",
    "family",
    "girl",
    "human",
    "kid",
    "kids",
    "man",
    "men",
    "people",
    "person",
    "portrait",
    "selfie",
    "staff",
    "student",
    "tourist",
    "visitor",
    "woman",
    "women",
    "worker",
    "\u4eba",
    "\u4eba\u7269",
    "\u4eba\u50cf",
    "\u6709\u4eba",
    "\u8138",
}
ABSENCE_PHRASES = {
    "no person",
    "no people",
    "without person",
    "without people",
    "\u65e0\u4eba",
    "\u6ca1\u6709\u4eba",
    "\u4e0d\u8981\u4eba",
    "\u4e0d\u8981\u6709\u4eba",
}
DIVERSITY_PHRASES = {
    "diverse",
    "low similarity",
    "not similar",
    "no duplicates",
    "avoid duplicates",
    "less repetitive",
    "\u76f8\u4f3c\u5ea6\u4f4e",
    "\u4e0d\u8981\u91cd\u590d",
    "\u4e0d\u91cd\u590d",
    "\u4e0d\u8981\u592a\u50cf",
    "\u522b\u592a\u50cf",
    "\u5dee\u5f02\u5927",
    "\u591a\u6837",
}
SOCIAL_POST_PHRASES = {
    "post",
    "publish",
    "social",
    "instagram",
    "\u670b\u53cb\u5708",
    "\u5c0f\u7ea2\u4e66",
    "\u53d1\u56fe",
    "\u53d1\u7167\u7247",
    "\u53d1\u5e03",
}
GENERIC_TERMS = {
    "a",
    "and",
    "area",
    "background",
    "capture",
    "captures",
    "day",
    "image",
    "images",
    "local",
    "moment",
    "moments",
    "no",
    "outdoor",
    "photo",
    "photos",
    "picture",
    "scene",
    "shot",
    "the",
    "this",
    "view",
    "without",
    "with",
    "\u7167\u7247",
    "\u56fe\u7247",
    "\u573a\u666f",
    "\u98ce\u666f",
}
CONCEPT_ALIASES = {
    "\u5c71": "mountain",
    "\u5c71\u666f": "mountain",
    "\u5c71\u5cf0": "mountain",
    "\u5c71\u8109": "mountain",
    "\u7fa4\u5c71": "mountain",
    "\u96ea\u5c71": "mountain",
    "\u5ca9\u5c71": "mountain",
    "mountains": "mountain",
    "peak": "mountain",
    "summit": "mountain",
    "valley": "mountain",
    "\u6d77\u8fb9": "beach",
    "\u6d77": "beach",
    "\u6cb3": "river",
    "\u6e56": "lake",
    "\u68ee\u6797": "forest",
    "\u82b1\u56ed": "garden",
    "\u81ea\u7136": "nature",
    "\u5927\u81ea\u7136": "nature",
    "\u81ea\u7136\u98ce\u5149": "landscape",
    "\u81ea\u7136\u666f\u8272": "landscape",
    "\u98ce\u666f\u7167": "landscape",
    "\u666f\u8272": "scenery",
    "\u9633\u5149\u660e\u5a9a": "sunny",
    "\u9633\u5149": "sunlight",
    "\u767d\u5929": "daytime",
    "\u57ce\u5e02": "city",
    "\u8857\u5934": "street",
    "\u5efa\u7b51\u7269": "architecture",
    "\u5efa\u7b51": "architecture",
    "\u6865": "bridge",
    "\u5927\u6865": "bridge",
    "\u96fe": "fog",
    "\u98df\u7269": "food",
    "\u7f8e\u98df": "food",
    "\u5403\u996d": "food",
    "\u9910\u5385": "restaurant",
    "\u996d\u5e97": "restaurant",
    "\u5496\u5561": "coffee",
    "\u65e5\u843d": "sunset",
    "\u508d\u665a": "sunset",
    "coast": "beach",
    "ocean": "beach",
    "street": "city",
    "building": "city",
    "skyline": "city",
    "forest": "forest",
    "tree": "forest",
    "trees": "forest",
    "food": "food",
    "coffee": "food",
    "restaurant": "food",
    "portrait": "people",
    "person": "people",
    "people": "people",
}
HAN_TEXT_RE = re.compile("[\u3400-\u9fff]")


@dataclass
class AtlasFilters:
    mode: str = DEFAULT_MODE
    query: str | None = None
    no_people: bool = False
    min_quality: float | None = None
    show_duplicates: bool = False
    limit: int = DEFAULT_VISIBLE_LIMIT
    cluster_id: str | None = None
    asset_ids: list[str] | None = None


class PhotoAtlasService:
    def __init__(self, repository: ImageIndexRepository):
        self.repository = repository

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(ATLAS_SCHEMA_SQL)

    def status(self) -> dict[str, object]:
        self.ensure_schema()
        with self._connect() as connection:
            image_count = self._image_count(connection)
            asset_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_assets").fetchone()[
                    "count"
                ]
            )
            cluster_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_clusters").fetchone()[
                    "count"
                ]
            )
            edge_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_edges").fetchone()[
                    "count"
                ]
            )
            run = connection.execute(
                """
                SELECT id, status, layout_version, image_count, cluster_count, edge_count,
                       started_at, completed_at, message
                FROM atlas_runs
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            newest_image_update = connection.execute(
                "SELECT MAX(updated_at) AS updated_at FROM image_index"
            ).fetchone()["updated_at"]

        completed_at = run["completed_at"] if run else None
        needs_rebuild = (
            run is None
            or run["status"] != "completed"
            or image_count != asset_count
            or (newest_image_update is not None and completed_at is not None and newest_image_update > completed_at)
        )
        if image_count == 0:
            needs_rebuild = False

        return {
            "object": "atlas.status",
            "status": run["status"] if run else "empty",
            "layout_version": run["layout_version"] if run else DEFAULT_LAYOUT_VERSION,
            "image_count": image_count,
            "asset_count": asset_count,
            "cluster_count": cluster_count,
            "edge_count": edge_count,
            "needs_rebuild": needs_rebuild,
            "last_run": dict(run) if run else None,
        }

    def rebuild(self) -> dict[str, object]:
        self.ensure_schema()
        started_at = utc_now_iso()
        run_id = f"atlas_{int(time.time())}"

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO atlas_runs (
                    id, status, layout_version, image_count, cluster_count,
                    edge_count, started_at, completed_at, message
                )
                VALUES (?, 'running', ?, 0, 0, 0, ?, NULL, NULL)
                """,
                (run_id, DEFAULT_LAYOUT_VERSION, started_at),
            )

        try:
            rows = self._fetch_image_rows()
            assets, clusters, edges = self._build_atlas(rows)
            completed_at = utc_now_iso()
            with self._connect() as connection:
                connection.execute("DELETE FROM atlas_assets")
                connection.execute("DELETE FROM atlas_clusters")
                connection.execute("DELETE FROM atlas_edges")
                connection.execute("DELETE FROM atlas_memories")
                connection.execute("DELETE FROM atlas_stacks")
                connection.execute("DELETE FROM atlas_roles")
                self._insert_assets(connection, assets, completed_at)
                self._insert_clusters(connection, clusters, completed_at)
                self._insert_edges(connection, edges, completed_at)
                self._insert_memories(connection, self._build_memories(assets), completed_at)
                self._insert_stacks(connection, self._build_stacks(assets, edges), completed_at)
                self._insert_roles(connection, self._build_roles(assets), completed_at)
                connection.execute(
                    """
                    UPDATE atlas_runs
                    SET status = 'completed',
                        image_count = ?,
                        cluster_count = ?,
                        edge_count = ?,
                        completed_at = ?,
                        message = ?
                    WHERE id = ?
                    """,
                    (
                        len(assets),
                        len(clusters),
                        len(edges),
                        completed_at,
                        "Atlas cache rebuilt locally.",
                        run_id,
                    ),
                )
            return {
                "object": "atlas.rebuild",
                "status": "completed",
                "layout_version": DEFAULT_LAYOUT_VERSION,
                "image_count": len(assets),
                "cluster_count": len(clusters),
                "edge_count": len(edges),
                "started_at": started_at,
                "completed_at": completed_at,
            }
        except Exception as exc:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE atlas_runs
                    SET status = 'failed',
                        completed_at = ?,
                        message = ?
                    WHERE id = ?
                    """,
                    (utc_now_iso(), str(exc), run_id),
                )
            raise

    def overview(self, filters: AtlasFilters | None = None) -> dict[str, object]:
        filters = filters or AtlasFilters()
        filters.mode = normalize_mode(filters.mode)
        index_health = self.status()
        self._ensure_current_cache()

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT image_id, filename, relative_path, taken_at, place_name,
                       country, description, tags_json, combined_text, embedding_backend,
                       x, y, cluster_id, event_id, duplicate_group_id,
                       neighbor_ids_json, quality_score, technical_quality_score,
                       people_risk, lat, lon, layout_version, updated_at
                FROM atlas_assets
                ORDER BY quality_score DESC, taken_at DESC, filename ASC
                """
            ).fetchall()
            hidden_ids = self._hidden_asset_ids(connection)
            forced_people_ids = self._feedback_asset_ids(connection, "never_show_people")
            cluster_labels = self._cluster_label_map(connection)

        assets = [
            self._asset_row_to_dict(
                row,
                mode=filters.mode,
                forced_people_ids=forced_people_ids,
                cluster_labels=cluster_labels,
            )
            for row in rows
            if row["image_id"] not in hidden_ids
        ]
        filtered_assets = self._apply_filters(assets, filters)
        clusters = self._build_response_clusters(filtered_assets, filters.mode)
        limited_assets = filtered_assets[: filters.limit]
        edge_assets = {asset["id"] for asset in limited_assets}
        edges = self._response_edges(edge_assets)

        return {
            "object": "atlas.overview",
            "status": "stale" if index_health["needs_rebuild"] else "completed",
            "mode": filters.mode,
            "layout_version": DEFAULT_LAYOUT_VERSION,
            "asset_count": len(assets),
            "filtered_count": len(filtered_assets),
            "visible_count": len(limited_assets),
            "clusters": clusters,
            "assets": limited_assets,
            "edges": edges,
            "stats": self._overview_stats(filtered_assets),
            "index_health": index_health,
        }

    def record_feedback(
        self,
        *,
        target_kind: str,
        target_id: str,
        action: str,
        weight: float = 1.0,
        note: str | None = None,
    ) -> dict[str, object]:
        self.ensure_schema()
        normalized_kind = target_kind.strip().lower()
        normalized_action = action.strip().lower()
        if normalized_kind not in {"asset", "cluster"}:
            raise ValueError("`target_kind` must be `asset` or `cluster`.")
        if not target_id.strip():
            raise ValueError("`target_id` must be non-empty.")
        if normalized_action not in {
            "more_like",
            "less_like",
            "hide",
            "hide_similar",
            "never_show_people",
            "rename_cluster",
        }:
            raise ValueError("Unsupported feedback action.")

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO atlas_feedback (
                    target_kind, target_id, action, weight, note, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_kind,
                    target_id.strip(),
                    normalized_action,
                    max(-5.0, min(5.0, float(weight))),
                    note.strip() if isinstance(note, str) and note.strip() else None,
                    utc_now_iso(),
                ),
            )

        return {
            "object": "atlas.feedback",
            "status": "recorded",
            "target_kind": normalized_kind,
            "target_id": target_id.strip(),
            "action": normalized_action,
        }

    def generate(
        self,
        *,
        text: str,
        top_k: int = 9,
        mode: str = DEFAULT_MODE,
        cluster_id: str | None = None,
        asset_ids: list[str] | None = None,
        no_people: bool = False,
        min_quality: float | None = None,
        show_duplicates: bool = False,
    ) -> dict[str, object]:
        effective_no_people = no_people or query_requests_no_people(text)
        context_assets: list[dict[str, object]] = []
        context_terms: list[str] = []
        if asset_ids:
            self._ensure_derived_layers()
            with self._connect() as connection:
                context_assets = self._assets_by_ids(connection, asset_ids[:24])
            context_terms = context_terms_from_assets(context_assets)
        effective_text = append_context_terms(text, context_terms)
        filters = AtlasFilters(
            mode=mode,
            query=effective_text,
            no_people=effective_no_people,
            min_quality=min_quality,
            show_duplicates=show_duplicates,
            limit=MAX_VISIBLE_LIMIT,
            cluster_id=cluster_id,
            asset_ids=None,
        )
        overview = self.overview(filters)
        candidates = list(overview["assets"])
        if context_assets:
            existing_ids = {str(asset["id"]) for asset in candidates}
            candidates.extend(
                asset
                for asset in context_assets
                if str(asset["id"]) not in existing_ids
                and (not effective_no_people or float(asset.get("people_risk") or 0.0) < 0.45)
            )
        if not candidates:
            return {
                "object": "atlas.generate",
                "id": f"atlas_gen_{int(time.time())}",
                "status": "completed",
                "query_text": text,
                "candidate_count": 0,
                "data": [],
                "atlas": overview,
            }

        selected = self._curate_assets(
            candidates,
            text=effective_text,
            top_k=top_k,
            context_assets=context_assets,
        )

        return {
            "object": "atlas.generate",
            "id": f"atlas_gen_{int(time.time())}",
            "status": "completed",
            "query_text": text,
            "candidate_count": len(candidates),
            "data": [self._asset_to_retrieval_image(asset) for asset in selected[:top_k]],
            "atlas": overview,
        }

    def assets_by_ids(self, asset_ids: list[str]) -> list[dict[str, object]]:
        self._ensure_derived_layers()
        with self._connect() as connection:
            return self._assets_by_ids(connection, asset_ids)

    def query_preview(
        self,
        *,
        text: str,
        lens: str = DEFAULT_LENS,
        no_people: bool = False,
        min_quality: float | None = None,
        show_duplicates: bool = False,
        limit: int = DEFAULT_VISIBLE_LIMIT,
        selected_memory_ids: list[str] | None = None,
    ) -> dict[str, object]:
        normalized_lens = normalize_lens(lens)
        mode = mode_for_lens(normalized_lens)
        intent = parse_atlas_intent(text)
        memory_asset_ids = self.asset_ids_for_memories(selected_memory_ids or [])
        effective_no_people = no_people or bool(intent["no_people_requested"])
        overview = self.overview(
            AtlasFilters(
                mode=mode,
                query=text,
                no_people=effective_no_people,
                min_quality=min_quality,
                show_duplicates=show_duplicates,
                limit=MAX_VISIBLE_LIMIT,
                asset_ids=memory_asset_ids or None,
            )
        )
        self._ensure_derived_layers()
        with self._connect() as connection:
            memories = self._memory_cards(connection, overview["assets"], normalized_lens)
            similarity = self._similarity_lookup(connection)

        curated = self._curate_assets(
            list(overview["assets"]),
            text=text,
            top_k=max(9, min(18, int(intent["target_count"]))),
        )
        warnings = query_preview_warnings(
            text=text,
            assets=curated,
            requested_no_people=effective_no_people,
        )
        return {
            "object": "atlas.query_preview",
            "status": overview["status"],
            "intent": intent,
            "mode": mode,
            "lens": normalized_lens,
            "candidate_count": overview["filtered_count"],
            "evidence": self._evidence_assets(
                curated[: max(9, min(18, int(intent["target_count"])))],
                text=text,
                similarity=similarity,
            ),
            "memories": memories[:8],
            "warnings": warnings,
            "suggested_queries": suggested_queries_from_memories(memories),
            "index_health": overview.get("index_health"),
        }

    def workbench(
        self,
        *,
        lens: str = DEFAULT_LENS,
        query: str | None = None,
        no_people: bool = False,
        min_quality: float | None = None,
        show_duplicates: bool = False,
        limit: int = DEFAULT_VISIBLE_LIMIT,
    ) -> dict[str, object]:
        normalized_lens = normalize_lens(lens)
        mode = mode_for_lens(normalized_lens)
        effective_no_people = no_people or query_requests_no_people(query or "")
        overview = self.overview(
            AtlasFilters(
                mode=mode,
                query=query,
                no_people=effective_no_people,
                min_quality=min_quality,
                show_duplicates=show_duplicates,
                limit=limit,
            )
        )
        self._ensure_derived_layers()

        with self._connect() as connection:
            memories = self._memory_cards(connection, overview["assets"], normalized_lens)
            cleanup = self._cleanup_summary(connection, overview["assets"])
            all_assets = self._all_cached_assets(connection)
            all_cleanup = self._cleanup_summary(connection, all_assets)
            inspiration_memories = self._memory_cards(connection, all_assets, "explore")
            basket = self._latest_basket(connection)

        featured_memory = memories[0] if memories else None
        return {
            "object": "atlas.workbench",
            "status": "completed",
            "lens": normalized_lens,
            "mode": mode,
            "lenses": build_lens_summaries(overview["assets"], cleanup),
            "overview": overview,
            "memories": memories,
            "featured_memory": featured_memory,
            "cleanup": cleanup,
            "basket": basket,
            "basket_ready": self._basket_ready_candidates(overview["assets"]),
            "library_summary": build_library_summary(
                assets=all_assets,
                memories=inspiration_memories,
                cleanup=all_cleanup,
                index_health=overview.get("index_health"),
            ),
            "inspiration_cards": build_inspiration_cards(
                memories=inspiration_memories,
                assets=all_assets,
                cleanup=all_cleanup,
            ),
            "suggested_queries": suggested_queries_from_memories(inspiration_memories),
            "storylines": build_storylines(inspiration_memories),
            "index_health": overview.get("index_health"),
        }

    def memory(self, memory_id: str) -> dict[str, object]:
        self._ensure_current_cache()
        self._ensure_derived_layers()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, kind, label, asset_ids_json, representative_ids_json,
                       top_concepts_json, place_label, time_label, x, y, score,
                       people_risk, duplicate_count, chapter_count
                FROM atlas_memories
                WHERE id = ?
                """,
                (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Atlas memory does not exist.")
            asset_ids = parse_json_list(row["asset_ids_json"])
            assets = self._assets_by_ids(connection, asset_ids)
            role_rows = connection.execute(
                """
                SELECT image_id, role, confidence, reason
                FROM atlas_roles
                WHERE memory_id = ?
                ORDER BY confidence DESC
                """,
                (memory_id,),
            ).fetchall()
            stacks = self._stacks_for_asset_ids(connection, asset_ids)

        roles: dict[str, list[dict[str, object]]] = defaultdict(list)
        for role_row in role_rows:
            roles[str(role_row["role"])].append(
                {
                    "image_id": role_row["image_id"],
                    "confidence": float(role_row["confidence"]),
                    "reason": role_row["reason"],
                }
            )
        chapters = build_memory_chapters(assets)
        return {
            "object": "atlas.memory",
            "status": "completed",
            "memory": self._memory_row_to_card(row, assets),
            "assets": assets,
            "chapters": chapters,
            "roles": roles,
            "stacks": stacks,
            "suggestions": memory_suggestions(row, assets, stacks),
        }

    def cleanup(self) -> dict[str, object]:
        self._ensure_current_cache()
        self._ensure_derived_layers()
        with self._connect() as connection:
            assets = self._all_cached_assets(connection)
            return {
                "object": "atlas.cleanup",
                "status": "completed",
                **self._cleanup_summary(connection, assets),
            }

    def asset_ids_for_memories(self, memory_ids: list[str]) -> list[str]:
        self._ensure_current_cache()
        normalized_ids = [memory_id.strip() for memory_id in memory_ids if memory_id.strip()]
        if not normalized_ids:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT asset_ids_json
                FROM atlas_memories
                WHERE id IN ({})
                """.format(",".join("?" for _ in normalized_ids)),
                normalized_ids,
            ).fetchall()
        asset_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for asset_id in parse_json_list(row["asset_ids_json"]):
                if asset_id not in seen:
                    asset_ids.append(asset_id)
                    seen.add(asset_id)
        return asset_ids

    def save_basket(
        self,
        *,
        asset_ids: list[str],
        name: str | None = None,
    ) -> dict[str, object]:
        self._ensure_current_cache()
        normalized_ids = [asset_id.strip() for asset_id in asset_ids if asset_id.strip()]
        unique_ids = list(dict.fromkeys(normalized_ids))[:60]
        now = utc_now_iso()
        basket_id = f"basket_{int(time.time() * 1000)}"
        with self._connect() as connection:
            if unique_ids:
                existing = {
                    row["image_id"]
                    for row in connection.execute(
                        "SELECT image_id FROM atlas_assets WHERE image_id IN ({})".format(
                            ",".join("?" for _ in unique_ids)
                        ),
                        unique_ids,
                    ).fetchall()
                }
                resolved_ids = [asset_id for asset_id in unique_ids if asset_id in existing]
            else:
                resolved_ids = []
            if unique_ids and not resolved_ids:
                raise ValueError("No basket assets exist in the Atlas cache.")
            connection.execute(
                """
                INSERT INTO atlas_baskets (
                    id, name, asset_ids_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    basket_id,
                    name.strip() if isinstance(name, str) and name.strip() else "Working basket",
                    json.dumps(resolved_ids),
                    now,
                    now,
                ),
            )
            basket = self._latest_basket(connection)
        return {
            "object": "atlas.basket",
            "status": "saved",
            "basket": basket,
        }

    def stack_action(
        self,
        *,
        stack_id: str,
        action: str,
        keep_asset_id: str | None = None,
    ) -> dict[str, object]:
        self._ensure_current_cache()
        self._ensure_derived_layers()
        normalized_action = action.strip().lower()
        if normalized_action not in {"keep_best", "hide_similar", "unstack"}:
            raise ValueError("Unsupported stack action.")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, asset_ids_json, best_image_id
                FROM atlas_stacks
                WHERE id = ?
                """,
                (stack_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Atlas stack does not exist.")
            asset_ids = parse_json_list(row["asset_ids_json"])
            keep_id = keep_asset_id or row["best_image_id"] or asset_ids[0]
            now = utc_now_iso()
            connection.execute(
                """
                INSERT INTO atlas_feedback (
                    target_kind, target_id, action, weight, note, created_at
                )
                VALUES ('stack', ?, ?, 1.0, ?, ?)
                """,
                (stack_id, normalized_action, keep_id, now),
            )
            if normalized_action in {"keep_best", "hide_similar"}:
                for asset_id in asset_ids:
                    if asset_id == keep_id:
                        continue
                    connection.execute(
                        """
                        INSERT INTO atlas_feedback (
                            target_kind, target_id, action, weight, note, created_at
                        )
                        VALUES ('asset', ?, 'hide_similar', 1.0, ?, ?)
                        """,
                        (asset_id, f"stack:{stack_id};keep:{keep_id}", now),
                    )
        return {
            "object": "atlas.stack_action",
            "status": "recorded",
            "stack_id": stack_id,
            "action": normalized_action,
            "keep_asset_id": keep_id,
        }

    def _ensure_current_cache(self) -> None:
        self.ensure_schema()

    def _ensure_derived_layers(self) -> None:
        self.ensure_schema()
        with self._connect() as connection:
            asset_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_assets").fetchone()[
                    "count"
                ]
            )
            memory_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_memories").fetchone()[
                    "count"
                ]
            )
            stack_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_stacks").fetchone()[
                    "count"
                ]
            )
            role_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM atlas_roles").fetchone()[
                    "count"
                ]
            )
            if asset_count == 0 or (memory_count > 0 and stack_count > 0 and role_count > 0):
                return
            assets = self._all_cached_assets(connection)
            edges = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, source_image_id, target_image_id, kind, weight
                    FROM atlas_edges
                    """
                ).fetchall()
            ]
            updated_at = utc_now_iso()
            connection.execute("DELETE FROM atlas_memories")
            connection.execute("DELETE FROM atlas_stacks")
            connection.execute("DELETE FROM atlas_roles")
            self._insert_memories(connection, self._build_memories(assets), updated_at)
            self._insert_stacks(connection, self._build_stacks(assets, edges), updated_at)
            self._insert_roles(connection, self._build_roles(assets), updated_at)

    def _all_cached_assets(self, connection: sqlite3.Connection) -> list[dict[str, object]]:
        rows = connection.execute(
            """
            SELECT image_id, filename, relative_path, taken_at, place_name,
                   country, description, tags_json, combined_text, embedding_backend,
                   x, y, cluster_id, event_id, duplicate_group_id,
                   neighbor_ids_json, quality_score, technical_quality_score,
                   people_risk, lat, lon, layout_version, updated_at
            FROM atlas_assets
            ORDER BY quality_score DESC, taken_at DESC, filename ASC
            """
        ).fetchall()
        cluster_labels = self._cluster_label_map(connection)
        forced_people_ids = self._feedback_asset_ids(connection, "never_show_people")
        hidden_ids = self._hidden_asset_ids(connection)
        return [
            self._asset_row_to_dict(
                row,
                mode="semantic",
                forced_people_ids=forced_people_ids,
                cluster_labels=cluster_labels,
            )
            for row in rows
            if row["image_id"] not in hidden_ids
        ]

    def _assets_by_ids(
        self,
        connection: sqlite3.Connection,
        asset_ids: list[str],
    ) -> list[dict[str, object]]:
        if not asset_ids:
            return []
        rows = connection.execute(
            """
            SELECT image_id, filename, relative_path, taken_at, place_name,
                   country, description, tags_json, combined_text, embedding_backend,
                   x, y, cluster_id, event_id, duplicate_group_id,
                   neighbor_ids_json, quality_score, technical_quality_score,
                   people_risk, lat, lon, layout_version, updated_at
            FROM atlas_assets
            WHERE image_id IN ({})
            """.format(",".join("?" for _ in asset_ids)),
            asset_ids,
        ).fetchall()
        cluster_labels = self._cluster_label_map(connection)
        forced_people_ids = self._feedback_asset_ids(connection, "never_show_people")
        by_id = {
            row["image_id"]: self._asset_row_to_dict(
                row,
                mode="semantic",
                forced_people_ids=forced_people_ids,
                cluster_labels=cluster_labels,
            )
            for row in rows
        }
        return [by_id[asset_id] for asset_id in asset_ids if asset_id in by_id]

    def _build_memories(self, assets: list[dict[str, object]]) -> list[dict[str, object]]:
        memories: list[dict[str, object]] = []
        memories.extend(
            self._memory_groups_from_assets(
                assets=assets,
                group_key="cluster_id",
                kind="topic",
                id_prefix="memory_topic",
            )
        )
        memories.extend(
            self._memory_groups_from_assets(
                assets=[asset for asset in assets if asset.get("event_id")],
                group_key="event_id",
                kind="event",
                id_prefix="memory_event",
                min_count=3,
            )
        )
        memories.extend(
            self._memory_groups_from_assets(
                assets=[asset for asset in assets if asset.get("place_name") or asset.get("country")],
                group_key="place_group",
                kind="place",
                id_prefix="memory_place",
                min_count=2,
            )
        )
        deduped = {memory["id"]: memory for memory in memories}
        return sorted(
            deduped.values(),
            key=lambda item: (float(item["score"]), int(item["count"])),
            reverse=True,
        )[:72]

    def _memory_groups_from_assets(
        self,
        *,
        assets: list[dict[str, object]],
        group_key: str,
        kind: str,
        id_prefix: str,
        min_count: int = 1,
    ) -> list[dict[str, object]]:
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for asset in assets:
            key = (
                str(asset.get("place_name") or asset.get("country") or "local")
                if group_key == "place_group"
                else str(asset.get(group_key) or "")
            )
            if key:
                grouped[key].append(asset)

        memories: list[dict[str, object]] = []
        for raw_key, members in grouped.items():
            if len(members) < min_count:
                continue
            representative_assets = best_assets(members, limit=5)
            top_concepts = top_terms(
                term
                for asset in members
                for term in extract_concepts(
                    [str(tag) for tag in asset.get("tags", [])],
                    str(asset.get("combined_text") or asset.get("description") or ""),
                )
            )
            place_label = common_label(
                [str(asset.get("place_name") or asset.get("country") or "") for asset in members]
            )
            time_label = time_label_for_assets(members)
            label = memory_label(
                kind=kind,
                fallback=str(members[0].get("cluster_label") or raw_key),
                top_concepts=top_concepts,
                place_label=place_label,
                time_label=time_label,
            )
            xs = [float(asset["x"]) for asset in members]
            ys = [float(asset["y"]) for asset in members]
            score = memory_score(members)
            memories.append(
                {
                    "id": f"{id_prefix}_{slugify(raw_key)}",
                    "kind": kind,
                    "label": label,
                    "asset_ids": [str(asset["id"]) for asset in members],
                    "representative_ids": [str(asset["id"]) for asset in representative_assets],
                    "top_concepts": top_concepts,
                    "place_label": place_label,
                    "time_label": time_label,
                    "x": sum(xs) / len(xs),
                    "y": sum(ys) / len(ys),
                    "score": score,
                    "people_risk": max(float(asset.get("people_risk") or 0.0) for asset in members),
                    "duplicate_count": sum(1 for asset in members if asset.get("duplicate_group_id")),
                    "chapter_count": len({str(asset.get("event_id") or "") for asset in members}),
                    "count": len(members),
                }
            )
        return memories

    def _build_stacks(
        self,
        assets: list[dict[str, object]],
        edges: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        by_id = {str(asset["id"]): asset for asset in assets}
        stacks: list[dict[str, object]] = []
        duplicate_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
        for asset in assets:
            group_id = asset.get("duplicate_group_id")
            if group_id:
                duplicate_groups[str(group_id)].append(asset)
        for group_id, members in duplicate_groups.items():
            if len(members) < 2:
                continue
            best = best_assets(members, limit=1)[0]
            stacks.append(
                {
                    "id": str(group_id),
                    "kind": "duplicate",
                    "asset_ids": [str(asset["id"]) for asset in members],
                    "representative_image_id": str(best["id"]),
                    "best_image_id": str(best["id"]),
                    "score": max(float(asset.get("quality_score") or 0.0) for asset in members),
                    "reason": "Near-duplicate burst detected from image embeddings.",
                }
            )

        similar_components = UnionFind(by_id.keys())
        for edge in edges:
            weight = float(edge.get("weight") or 0.0)
            if weight >= 0.78:
                similar_components.union(str(edge["source_image_id"]), str(edge["target_image_id"]))
        grouped_components: dict[str, list[dict[str, object]]] = defaultdict(list)
        for asset_id, asset in by_id.items():
            grouped_components[similar_components.find(asset_id)].append(asset)
        for index, members in enumerate(grouped_components.values()):
            if len(members) < 3:
                continue
            best = best_assets(members, limit=1)[0]
            stack_id = f"similar_{index:03d}_{slugify(str(best['filename']))}"
            if stack_id in {stack["id"] for stack in stacks}:
                continue
            stacks.append(
                {
                    "id": stack_id,
                    "kind": "similar",
                    "asset_ids": [str(asset["id"]) for asset in members],
                    "representative_image_id": str(best["id"]),
                    "best_image_id": str(best["id"]),
                    "score": max(float(asset.get("quality_score") or 0.0) for asset in members),
                    "reason": "Visually similar set that is useful for comparison.",
                }
            )
        return sorted(stacks, key=lambda item: (len(item["asset_ids"]), float(item["score"])), reverse=True)[:96]

    def _build_roles(self, assets: list[dict[str, object]]) -> list[dict[str, object]]:
        memory_groups = self._build_memories(assets)
        assets_by_id = {str(asset["id"]): asset for asset in assets}
        roles: list[dict[str, object]] = []
        for memory in memory_groups:
            members = [assets_by_id[asset_id] for asset_id in memory["asset_ids"] if asset_id in assets_by_id]
            if not members:
                continue
            cover = best_assets(members, limit=1)[0]
            roles.append(role_record(cover, memory["id"], "cover", 0.96, "Highest quality representative."))
            wide = best_matching_asset(members, ["wide", "panoramic", "landscape", "vista", "sky"])
            if wide:
                roles.append(role_record(wide, memory["id"], "wide", 0.82, "Wide establishing frame."))
            detail = best_matching_asset(members, ["detail", "close", "food", "table", "flower", "texture"])
            if detail:
                roles.append(role_record(detail, memory["id"], "detail", 0.74, "Detail frame for pacing."))
            ending = best_matching_asset(members, ["quiet", "calm", "soft", "sunset", "night", "reflection"])
            if ending:
                roles.append(role_record(ending, memory["id"], "ending", 0.7, "Quiet closing frame."))
            for asset in members:
                if float(asset.get("quality_score") or 0.0) < 0.34 or asset.get("duplicate_group_id"):
                    roles.append(role_record(asset, memory["id"], "cleanup_candidate", 0.68, "Review for quality or similarity."))
        return roles

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.repository.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _fetch_image_rows(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT id, filename, relative_path, file_size, width, height,
                       taken_at, lat, lon, place_name, country, description,
                       tags_json, combined_text, embedding_backend, embedding,
                       aesthetic_score, technical_quality_score, updated_at
                FROM image_index
                ORDER BY taken_at IS NULL, taken_at DESC, filename ASC
                """
            ).fetchall()

    def _build_atlas(
        self,
        rows: list[sqlite3.Row],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        if not rows:
            return [], [], []

        vectors = self._vectors_for_rows(rows)
        coords = project_vectors(vectors)
        cluster_indices = kmeans_cluster(coords, cluster_count_for(len(rows)))
        neighbor_ids, edge_weights, duplicate_groups = build_similarity_graph(
            rows=rows,
            vectors=vectors,
        )

        raw_assets: list[dict[str, object]] = []
        for index, row in enumerate(rows):
            tags = parse_tags(row["tags_json"])
            combined_text = str(row["combined_text"] or row["description"] or "")
            normalized_terms = extract_concepts(tags, combined_text)
            quality_score = coerce_score(row["aesthetic_score"])
            if quality_score is None:
                quality_score = coerce_score(row["technical_quality_score"])
            if quality_score is None:
                quality_score = metadata_quality_score(normalize_text(combined_text))
            cluster_index = int(cluster_indices[index])
            raw_assets.append(
                {
                    "id": row["id"],
                    "filename": row["filename"],
                    "relative_path": row["relative_path"],
                    "taken_at": row["taken_at"],
                    "place_name": row["place_name"],
                    "country": row["country"],
                    "description": row["description"],
                    "tags": tags,
                    "combined_text": combined_text,
                    "embedding_backend": row["embedding_backend"],
                    "x": float(coords[index, 0]),
                    "y": float(coords[index, 1]),
                    "cluster_index": cluster_index,
                    "event_id": build_event_id(row, cluster_index),
                    "duplicate_group_id": duplicate_groups.get(row["id"]),
                    "neighbor_ids": neighbor_ids.get(row["id"], []),
                    "quality_score": float(max(0.0, min(1.0, quality_score))),
                    "technical_quality_score": coerce_score(row["technical_quality_score"]),
                    "people_risk": people_risk(tags, combined_text),
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "concepts": normalized_terms,
                }
            )

        cluster_labels = label_semantic_clusters(raw_assets)
        assets: list[dict[str, object]] = []
        for asset in raw_assets:
            cluster_id = f"semantic_{asset['cluster_index']:02d}_{slugify(cluster_labels[int(asset['cluster_index'])]['label'])}"
            assets.append(
                {
                    **asset,
                    "cluster_id": cluster_id,
                }
            )

        clusters = [
            {
                "id": f"semantic_{index:02d}_{slugify(payload['label'])}",
                "mode": "semantic",
                **payload,
            }
            for index, payload in cluster_labels.items()
        ]

        edges: list[dict[str, object]] = []
        for source_id, targets in edge_weights.items():
            for target_id, weight in targets:
                if weight < EDGE_SIMILARITY_FLOOR:
                    continue
                first, second = sorted([source_id, target_id])
                edges.append(
                    {
                        "id": f"{first}__{second}",
                        "source_image_id": first,
                        "target_image_id": second,
                        "kind": "similarity",
                        "weight": float(weight),
                    }
                )
        deduped_edges = {edge["id"]: edge for edge in edges}
        return assets, clusters, list(deduped_edges.values())

    def _vectors_for_rows(self, rows: list[sqlite3.Row]) -> np.ndarray:
        decoded = [decode_embedding(row["embedding"]) for row in rows]
        dimensions = [vector.size for vector in decoded if vector is not None and vector.size > 0]
        target_dim = most_common_dimension(dimensions) if dimensions else 64
        vectors: list[np.ndarray] = []
        for row, vector in zip(rows, decoded):
            if vector is None or vector.size == 0:
                vector = hashed_text_vector(
                    " ".join(
                        [
                            str(row["filename"] or ""),
                            str(row["description"] or ""),
                            str(row["tags_json"] or ""),
                            str(row["combined_text"] or ""),
                        ]
                    ),
                    target_dim,
                )
            elif vector.size != target_dim:
                adjusted = np.zeros(target_dim, dtype=np.float32)
                copy_count = min(target_dim, vector.size)
                adjusted[:copy_count] = vector[:copy_count]
                vector = adjusted
            norm = float(np.linalg.norm(vector))
            vectors.append((vector / norm).astype(np.float32) if norm else vector)
        return np.vstack(vectors).astype(np.float32)

    def _insert_assets(
        self,
        connection: sqlite3.Connection,
        assets: list[dict[str, object]],
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO atlas_assets (
                image_id, filename, relative_path, taken_at, place_name,
                country, description, tags_json, combined_text, embedding_backend,
                x, y, cluster_id, event_id, duplicate_group_id,
                neighbor_ids_json, quality_score, technical_quality_score,
                people_risk, lat, lon, layout_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    asset["id"],
                    asset["filename"],
                    asset["relative_path"],
                    asset["taken_at"],
                    asset["place_name"],
                    asset["country"],
                    asset["description"],
                    json.dumps(asset["tags"], ensure_ascii=False),
                    asset["combined_text"],
                    asset["embedding_backend"],
                    asset["x"],
                    asset["y"],
                    asset["cluster_id"],
                    asset["event_id"],
                    asset["duplicate_group_id"],
                    json.dumps(asset["neighbor_ids"]),
                    asset["quality_score"],
                    asset["technical_quality_score"],
                    asset["people_risk"],
                    asset["lat"],
                    asset["lon"],
                    DEFAULT_LAYOUT_VERSION,
                    updated_at,
                )
                for asset in assets
            ],
        )

    def _insert_clusters(
        self,
        connection: sqlite3.Connection,
        clusters: list[dict[str, object]],
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO atlas_clusters (
                id, mode, label, count, representative_image_id,
                top_concepts_json, place_label, time_label, x, y,
                bounds_json, layout_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    cluster["id"],
                    cluster["mode"],
                    cluster["label"],
                    cluster["count"],
                    cluster["representative_image_id"],
                    json.dumps(cluster["top_concepts"], ensure_ascii=False),
                    cluster["place_label"],
                    cluster["time_label"],
                    cluster["x"],
                    cluster["y"],
                    json.dumps(cluster["bounds"]),
                    DEFAULT_LAYOUT_VERSION,
                    updated_at,
                )
                for cluster in clusters
            ],
        )

    def _insert_edges(
        self,
        connection: sqlite3.Connection,
        edges: list[dict[str, object]],
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO atlas_edges (
                id, source_image_id, target_image_id, kind, weight,
                layout_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    edge["id"],
                    edge["source_image_id"],
                    edge["target_image_id"],
                    edge["kind"],
                    edge["weight"],
                    DEFAULT_LAYOUT_VERSION,
                    updated_at,
                )
                for edge in edges
            ],
        )

    def _insert_memories(
        self,
        connection: sqlite3.Connection,
        memories: list[dict[str, object]],
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO atlas_memories (
                id, kind, label, asset_ids_json, representative_ids_json,
                top_concepts_json, place_label, time_label, x, y, score,
                people_risk, duplicate_count, chapter_count, layout_version,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    memory["id"],
                    memory["kind"],
                    memory["label"],
                    json.dumps(memory["asset_ids"]),
                    json.dumps(memory["representative_ids"]),
                    json.dumps(memory["top_concepts"], ensure_ascii=False),
                    memory["place_label"],
                    memory["time_label"],
                    memory["x"],
                    memory["y"],
                    memory["score"],
                    memory["people_risk"],
                    memory["duplicate_count"],
                    memory["chapter_count"],
                    DEFAULT_LAYOUT_VERSION,
                    updated_at,
                )
                for memory in memories
            ],
        )

    def _insert_stacks(
        self,
        connection: sqlite3.Connection,
        stacks: list[dict[str, object]],
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO atlas_stacks (
                id, kind, asset_ids_json, representative_image_id,
                best_image_id, score, reason, layout_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    stack["id"],
                    stack["kind"],
                    json.dumps(stack["asset_ids"]),
                    stack["representative_image_id"],
                    stack["best_image_id"],
                    stack["score"],
                    stack["reason"],
                    DEFAULT_LAYOUT_VERSION,
                    updated_at,
                )
                for stack in stacks
            ],
        )

    def _insert_roles(
        self,
        connection: sqlite3.Connection,
        roles: list[dict[str, object]],
        updated_at: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO atlas_roles (
                image_id, memory_id, role, confidence, reason, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    role["image_id"],
                    role["memory_id"],
                    role["role"],
                    role["confidence"],
                    role["reason"],
                    updated_at,
                )
                for role in roles
            ],
        )

    def _memory_cards(
        self,
        connection: sqlite3.Connection,
        visible_assets: list[dict[str, object]],
        lens: str,
    ) -> list[dict[str, object]]:
        kind_filter = {
            "explore": {"topic", "event", "place"},
            "story": {"event", "topic"},
            "map": {"place", "event"},
            "people": {"topic", "event"},
            "cleanup": {"topic", "event"},
            "similar": {"topic", "event"},
        }.get(lens, {"topic", "event", "place"})
        rows = connection.execute(
            """
            SELECT id, kind, label, asset_ids_json, representative_ids_json,
                   top_concepts_json, place_label, time_label, x, y, score,
                   people_risk, duplicate_count, chapter_count
            FROM atlas_memories
            ORDER BY score DESC, chapter_count DESC
            LIMIT 120
            """
        ).fetchall()
        visible_by_id = {str(asset["id"]): asset for asset in visible_assets}
        cards: list[dict[str, object]] = []
        for row in rows:
            if str(row["kind"]) not in kind_filter:
                continue
            asset_ids = [asset_id for asset_id in parse_json_list(row["asset_ids_json"]) if asset_id in visible_by_id]
            if not asset_ids:
                continue
            assets = [visible_by_id[asset_id] for asset_id in asset_ids]
            if lens == "people" and max(float(asset.get("people_risk") or 0.0) for asset in assets) < 0.45:
                continue
            if lens == "cleanup" and not any(
                float(asset.get("quality_score") or 0.0) < 0.42 or asset.get("duplicate_group_id")
                for asset in assets
            ):
                continue
            cards.append(self._memory_row_to_card(row, assets))
        if lens == "people" and not cards:
            cards = self._people_memory_cards(visible_assets)
        if lens == "cleanup" and not cards:
            cards = self._cleanup_memory_cards(visible_assets)
        return cards[:36]

    def _memory_row_to_card(
        self,
        row: sqlite3.Row,
        assets: list[dict[str, object]],
    ) -> dict[str, object]:
        representative_ids = parse_json_list(row["representative_ids_json"])
        assets_by_id = {str(asset["id"]): asset for asset in assets}
        representative_assets = [
            assets_by_id[asset_id]
            for asset_id in representative_ids
            if asset_id in assets_by_id
        ]
        if not representative_assets:
            representative_assets = best_assets(assets, limit=5)
        return {
            "object": "atlas.memory",
            "id": row["id"],
            "kind": row["kind"],
            "label": english_text(row["label"], "Memory"),
            "asset_count": len(assets),
            "asset_ids": [str(asset["id"]) for asset in assets],
            "representative_asset_ids": [str(asset["id"]) for asset in representative_assets],
            "representative_assets": representative_assets[:5],
            "top_concepts": english_terms(parse_json_list(row["top_concepts_json"])),
            "place_label": english_text(row["place_label"]) or None,
            "time_label": row["time_label"],
            "x": float(row["x"]),
            "y": float(row["y"]),
            "score": float(row["score"]),
            "people_risk": float(row["people_risk"]),
            "duplicate_count": int(row["duplicate_count"]),
            "chapter_count": int(row["chapter_count"]),
            "best_assets": best_assets(assets, limit=9),
        }

    def _people_memory_cards(self, assets: list[dict[str, object]]) -> list[dict[str, object]]:
        people_assets = [asset for asset in assets if float(asset.get("people_risk") or 0.0) >= 0.45]
        no_people_assets = [asset for asset in assets if float(asset.get("people_risk") or 0.0) < 0.45]
        cards: list[dict[str, object]] = []
        for key, label, members in [
            ("people_likely", "People likely", people_assets),
            ("people_absent", "No people detected", no_people_assets),
        ]:
            if not members:
                continue
            cards.append(
                memory_card_from_assets(
                    memory_id=f"memory_{key}",
                    kind="people",
                    label=label,
                    assets=members,
                )
            )
        return cards

    def _cleanup_memory_cards(self, assets: list[dict[str, object]]) -> list[dict[str, object]]:
        groups = [
            ("cleanup_duplicates", "Similar bursts", [asset for asset in assets if asset.get("duplicate_group_id")]),
            ("cleanup_quality", "Needs quality review", [asset for asset in assets if float(asset.get("quality_score") or 0.0) < 0.42]),
            ("cleanup_missing_time", "Missing dates", [asset for asset in assets if not asset.get("taken_at")]),
            ("cleanup_missing_place", "Missing places", [asset for asset in assets if not asset.get("place_name") and not asset.get("country")]),
        ]
        return [
            memory_card_from_assets(memory_id=memory_id, kind="cleanup", label=label, assets=members)
            for memory_id, label, members in groups
            if members
        ]

    def _cleanup_summary(
        self,
        connection: sqlite3.Connection,
        assets: list[dict[str, object]],
    ) -> dict[str, object]:
        stacks = self._stack_rows_to_cards(
            connection.execute(
                """
                SELECT id, kind, asset_ids_json, representative_image_id,
                       best_image_id, score, reason
                FROM atlas_stacks
                ORDER BY kind ASC, score DESC
                LIMIT 48
                """
            ).fetchall(),
            assets,
        )
        low_quality = [
            asset for asset in assets if float(asset.get("quality_score") or 0.0) < 0.42
        ][:24]
        missing_time = [asset for asset in assets if not asset.get("taken_at")][:24]
        missing_place = [
            asset for asset in assets if not asset.get("place_name") and not asset.get("country")
        ][:24]
        people_review = [
            asset for asset in assets if 0.35 <= float(asset.get("people_risk") or 0.0) < 0.8
        ][:24]
        return {
            "duplicate_stack_count": sum(1 for stack in stacks if stack["kind"] == "duplicate"),
            "similar_stack_count": sum(1 for stack in stacks if stack["kind"] == "similar"),
            "low_quality_count": len(low_quality),
            "missing_time_count": len(missing_time),
            "missing_place_count": len(missing_place),
            "people_review_count": len(people_review),
            "stacks": stacks,
            "low_quality_assets": low_quality,
            "missing_time_assets": missing_time,
            "missing_place_assets": missing_place,
            "people_review_assets": people_review,
        }

    def _stack_rows_to_cards(
        self,
        rows: list[sqlite3.Row],
        visible_assets: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        visible_by_id = {str(asset["id"]): asset for asset in visible_assets}
        cards: list[dict[str, object]] = []
        for row in rows:
            asset_ids = [asset_id for asset_id in parse_json_list(row["asset_ids_json"]) if asset_id in visible_by_id]
            if len(asset_ids) < 2:
                continue
            assets = [visible_by_id[asset_id] for asset_id in asset_ids]
            best_id = row["best_image_id"] if row["best_image_id"] in visible_by_id else asset_ids[0]
            cards.append(
                {
                    "object": "atlas.stack",
                    "id": row["id"],
                    "kind": row["kind"],
                    "asset_ids": asset_ids,
                    "assets": assets[:8],
                    "representative_asset_id": row["representative_image_id"],
                    "best_asset_id": best_id,
                    "best_asset": visible_by_id.get(best_id),
                    "score": float(row["score"]),
                    "reason": row["reason"],
                    "count": len(asset_ids),
                }
            )
        return cards

    def _stacks_for_asset_ids(
        self,
        connection: sqlite3.Connection,
        asset_ids: list[str],
    ) -> list[dict[str, object]]:
        if not asset_ids:
            return []
        rows = connection.execute(
            """
            SELECT id, kind, asset_ids_json, representative_image_id,
                   best_image_id, score, reason
            FROM atlas_stacks
            ORDER BY score DESC
            LIMIT 96
            """
        ).fetchall()
        matching_rows = [
            row
            for row in rows
            if set(parse_json_list(row["asset_ids_json"])) & set(asset_ids)
        ]
        stack_asset_ids: list[str] = []
        seen: set[str] = set()
        for row in matching_rows:
            for stack_asset_id in parse_json_list(row["asset_ids_json"]):
                if stack_asset_id not in seen:
                    stack_asset_ids.append(stack_asset_id)
                    seen.add(stack_asset_id)
        assets = self._assets_by_ids(connection, stack_asset_ids)
        return self._stack_rows_to_cards(matching_rows, assets)

    def _latest_basket(self, connection: sqlite3.Connection) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT id, name, asset_ids_json, created_at, updated_at
            FROM atlas_baskets
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return {
                "object": "atlas.basket",
                "id": None,
                "name": "Working basket",
                "asset_ids": [],
                "assets": [],
                "created_at": None,
                "updated_at": None,
            }
        asset_ids = parse_json_list(row["asset_ids_json"])
        return {
            "object": "atlas.basket",
            "id": row["id"],
            "name": row["name"],
            "asset_ids": asset_ids,
            "assets": self._assets_by_ids(connection, asset_ids),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _basket_ready_candidates(assets: list[dict[str, object]]) -> list[dict[str, object]]:
        return best_assets(assets, limit=18)

    def _asset_row_to_dict(
        self,
        row: sqlite3.Row,
        *,
        mode: str,
        forced_people_ids: set[str],
        cluster_labels: dict[str, str],
    ) -> dict[str, object]:
        tags = parse_tags(row["tags_json"])
        base_x = float(row["x"])
        base_y = float(row["y"])
        people_value = max(
            float(row["people_risk"]),
            1.0 if row["image_id"] in forced_people_ids else 0.0,
        )
        asset = {
            "object": "atlas.asset",
            "id": row["image_id"],
            "filename": row["filename"],
            "relative_path": row["relative_path"],
            "title": english_text(title_from_filename(row["filename"]), "Photo"),
            "taken_at": row["taken_at"],
            "place_name": english_text(row["place_name"]) or None,
            "country": english_text(row["country"]) or None,
            "description": english_text(row["description"], "Local library photo"),
            "tags": english_terms(tags),
            "combined_text": english_text(row["combined_text"], ""),
            "embedding_backend": row["embedding_backend"],
            "base_x": base_x,
            "base_y": base_y,
            "cluster_id": row["cluster_id"],
            "cluster_label": english_text(cluster_labels.get(row["cluster_id"], "Semantic group"), "Semantic group"),
            "event_id": row["event_id"],
            "duplicate_group_id": row["duplicate_group_id"],
            "neighbor_ids": parse_json_list(row["neighbor_ids_json"]),
            "quality_score": float(row["quality_score"]),
            "technical_quality_score": row["technical_quality_score"],
            "people_risk": people_value,
            "lat": row["lat"],
            "lon": row["lon"],
            "layout_version": row["layout_version"],
        }
        x, y = transform_asset_position(asset, mode)
        mode_cluster_id, mode_cluster_label = mode_cluster_for(asset, mode)
        return {
            **asset,
            "x": x,
            "y": y,
            "mode_cluster_id": mode_cluster_id,
            "mode_cluster_label": mode_cluster_label,
        }

    def _apply_filters(
        self,
        assets: list[dict[str, object]],
        filters: AtlasFilters,
    ) -> list[dict[str, object]]:
        query_terms = normalize_query_terms(filters.query or "")
        allowed_asset_ids = set(filters.asset_ids or [])
        duplicate_seen: set[str] = set()
        filtered: list[dict[str, object]] = []

        for asset in assets:
            if allowed_asset_ids and asset["id"] not in allowed_asset_ids:
                continue
            if filters.cluster_id and filters.cluster_id not in {
                asset["cluster_id"],
                asset["mode_cluster_id"],
                asset["event_id"],
            }:
                continue
            if filters.no_people and float(asset["people_risk"]) >= 0.45:
                continue
            if filters.min_quality is not None and float(asset["quality_score"]) < filters.min_quality:
                continue
            if query_terms and self._asset_query_match(asset, query_terms) <= 0:
                continue
            duplicate_group = str(asset.get("duplicate_group_id") or "")
            if duplicate_group and not filters.show_duplicates:
                if duplicate_group in duplicate_seen:
                    continue
                duplicate_seen.add(duplicate_group)
            filtered.append(asset)

        filtered.sort(
            key=lambda item: (
                self._asset_query_match(item, query_terms) if query_terms else 0.0,
                float(item["quality_score"]),
                str(item.get("taken_at") or ""),
            ),
            reverse=True,
        )
        return filtered

    def _asset_query_match(
        self,
        asset: dict[str, object],
        query_terms: list[str],
    ) -> float:
        if not query_terms:
            return 0.0
        searchable = normalize_text(
            " ".join(
                [
                    str(asset.get("filename") or ""),
                    str(asset.get("description") or ""),
                    str(asset.get("combined_text") or ""),
                    str(asset.get("place_name") or ""),
                    str(asset.get("country") or ""),
                    str(asset.get("cluster_label") or ""),
                    str(asset.get("mode_cluster_label") or ""),
                    " ".join(str(tag) for tag in asset.get("tags", [])),
                ]
            )
        )
        score = 0.0
        for term in query_terms:
            if term in searchable:
                score += 1.0
            elif any(token.startswith(term) or term.startswith(token) for token in searchable.split()):
                score += 0.45
        return score

    def _build_response_clusters(
        self,
        assets: list[dict[str, object]],
        mode: str,
    ) -> list[dict[str, object]]:
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for asset in assets:
            grouped[str(asset["mode_cluster_id"])].append(asset)

        clusters: list[dict[str, object]] = []
        for cluster_id, members in grouped.items():
            representative = max(
                members,
                key=lambda item: (float(item["quality_score"]), str(item.get("taken_at") or "")),
            )
            xs = [float(item["x"]) for item in members]
            ys = [float(item["y"]) for item in members]
            label = str(representative["mode_cluster_label"])
            top_concepts = top_terms(
                term
                for item in members
                for term in extract_concepts(
                    [str(tag) for tag in item.get("tags", [])],
                    str(item.get("combined_text") or item.get("description") or ""),
                )
            )
            clusters.append(
                {
                    "object": "atlas.cluster",
                    "id": cluster_id,
                    "mode": mode,
                    "label": label,
                    "count": len(members),
                    "representative_asset_id": representative["id"],
                    "top_concepts": top_concepts,
                    "place_label": common_label(
                        [str(item.get("place_name") or item.get("country") or "") for item in members]
                    ),
                    "time_label": time_label_for_assets(members),
                    "x": sum(xs) / len(xs),
                    "y": sum(ys) / len(ys),
                    "bounds": {
                        "min_x": min(xs),
                        "max_x": max(xs),
                        "min_y": min(ys),
                        "max_y": max(ys),
                    },
                }
            )
        clusters.sort(key=lambda item: (int(item["count"]), str(item["label"])), reverse=True)
        return clusters

    def _response_edges(self, asset_ids: set[str]) -> list[dict[str, object]]:
        if not asset_ids:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_image_id, target_image_id, kind, weight
                FROM atlas_edges
                WHERE source_image_id IN ({placeholders})
                   OR target_image_id IN ({placeholders})
                ORDER BY weight DESC
                LIMIT 240
                """.format(placeholders=",".join("?" for _ in asset_ids)),
                [*asset_ids, *asset_ids],
            ).fetchall()
        edges = []
        for row in rows:
            if row["source_image_id"] in asset_ids and row["target_image_id"] in asset_ids:
                edges.append(
                    {
                        "object": "atlas.edge",
                        "source": row["source_image_id"],
                        "target": row["target_image_id"],
                        "kind": row["kind"],
                        "weight": float(row["weight"]),
                    }
                )
        return edges

    @staticmethod
    def _overview_stats(assets: list[dict[str, object]]) -> dict[str, object]:
        if not assets:
            return {
                "quality_avg": 0.0,
                "people_risk_count": 0,
                "duplicate_group_count": 0,
                "top_concepts": [],
            }
        return {
            "quality_avg": round(
                sum(float(asset["quality_score"]) for asset in assets) / len(assets),
                4,
            ),
            "people_risk_count": sum(1 for asset in assets if float(asset["people_risk"]) >= 0.45),
            "duplicate_group_count": len(
                {
                    str(asset.get("duplicate_group_id"))
                    for asset in assets
                    if asset.get("duplicate_group_id")
                }
            ),
            "top_concepts": top_terms(
                term
                for asset in assets
                for term in extract_concepts(
                    [str(tag) for tag in asset.get("tags", [])],
                    str(asset.get("combined_text") or asset.get("description") or ""),
                )
            ),
        }

    def _curate_assets(
        self,
        candidates: list[dict[str, object]],
        *,
        text: str,
        top_k: int,
        context_assets: list[dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        if not candidates:
            return []
        with self._connect() as connection:
            feedback_boosts = self._feedback_boosts(connection)
            similarity = self._similarity_lookup(connection)

        query_terms = normalize_query_terms(text)
        context_assets = context_assets or []
        selected: list[dict[str, object]] = []
        pending = [
            {
                **asset,
                "_relevance": self._asset_relevance(asset, query_terms, feedback_boosts),
                "_context_score": self._context_asset_score(asset, context_assets, similarity),
            }
            for asset in candidates
        ]
        pending.sort(
            key=lambda item: (
                float(item["_relevance"]),
                float(item["_context_score"]),
                float(item["quality_score"]),
                str(item.get("taken_at") or ""),
            ),
            reverse=True,
        )

        wants_diversity = query_requests_diversity(text)
        diversity_weight = 0.74 if wants_diversity else 0.46
        duplicate_weight = 0.52 if wants_diversity else 0.34
        target_count = max(1, min(36, top_k))

        while pending and len(selected) < target_count:
            best_index = 0
            best_score = float("-inf")
            best_diversity = 1.0
            best_duplicate_penalty = 0.0
            for index, candidate in enumerate(pending):
                diversity_penalty = self._selection_similarity(candidate, selected, similarity)
                duplicate_penalty = self._duplicate_penalty(candidate, selected)
                score = (
                    0.64 * float(candidate["_relevance"])
                    + 0.34 * float(candidate["_context_score"])
                    + 0.3 * float(candidate["quality_score"])
                    - diversity_weight * diversity_penalty
                    - duplicate_weight * duplicate_penalty
                )
                if score > best_score:
                    best_score = score
                    best_index = index
                    best_diversity = max(0.0, 1.0 - diversity_penalty)
                    best_duplicate_penalty = duplicate_penalty
            chosen = pending.pop(best_index)
            chosen["score"] = round(best_score, 4)
            chosen["_diversity_score"] = round(best_diversity, 4)
            chosen["_duplicate_penalty"] = round(best_duplicate_penalty, 4)
            selected.append(chosen)
        return selected

    def _context_asset_score(
        self,
        candidate: dict[str, object],
        context_assets: list[dict[str, object]],
        similarity: dict[tuple[str, str], float],
    ) -> float:
        if not context_assets:
            return 0.0
        candidate_id = str(candidate["id"])
        candidate_terms = set(
            extract_concepts(
                [str(tag) for tag in candidate.get("tags", [])],
                str(candidate.get("combined_text") or candidate.get("description") or ""),
            )
        )
        best_similarity = 0.0
        best_overlap = 0.0
        for context_asset in context_assets:
            context_id = str(context_asset["id"])
            if context_id == candidate_id:
                best_similarity = max(best_similarity, 0.72)
            else:
                pair = tuple(sorted([candidate_id, context_id]))
                best_similarity = max(best_similarity, similarity.get(pair, 0.0))
            context_terms = set(
                extract_concepts(
                    [str(tag) for tag in context_asset.get("tags", [])],
                    str(context_asset.get("combined_text") or context_asset.get("description") or ""),
                )
            )
            if candidate_terms and context_terms:
                best_overlap = max(
                    best_overlap,
                    len(candidate_terms & context_terms) / max(1, len(candidate_terms | context_terms)),
                )
        return max(0.0, min(1.0, 0.65 * best_similarity + 0.35 * best_overlap))

    def _evidence_assets(
        self,
        assets: list[dict[str, object]],
        *,
        text: str,
        similarity: dict[tuple[str, str], float],
    ) -> list[dict[str, object]]:
        query_terms = normalize_query_terms(text)
        evidence: list[dict[str, object]] = []
        selected_so_far: list[dict[str, object]] = []
        requested_no_people = query_requests_no_people(text)
        for index, asset in enumerate(assets):
            relevance = self._asset_query_match(asset, query_terms)
            diversity_score = float(asset.get("_diversity_score") or 1.0)
            duplicate_penalty = float(asset.get("_duplicate_penalty") or 0.0)
            if index > 0 and "_diversity_score" not in asset:
                diversity_score = 1.0 - self._selection_similarity(asset, selected_so_far, similarity)
            matched_terms = [
                term
                for term in query_terms
                if self._asset_query_match(asset, [term]) > 0
            ][:5]
            reasons = evidence_reasons(
                asset=asset,
                matched_terms=matched_terms,
                requested_no_people=requested_no_people,
                diversity_score=diversity_score,
            )
            warnings = evidence_warnings(
                asset=asset,
                requested_no_people=requested_no_people,
                duplicate_penalty=duplicate_penalty,
            )
            evidence.append(
                {
                    "asset": strip_internal_scores(asset),
                    "rank": index + 1,
                    "relevance_score": round(relevance / max(1, len(query_terms)), 4) if query_terms else 0.25,
                    "quality_score": round(float(asset.get("quality_score") or 0.0), 4),
                    "diversity_score": round(max(0.0, min(1.0, diversity_score)), 4),
                    "people_risk": round(float(asset.get("people_risk") or 0.0), 4),
                    "duplicate_penalty": round(duplicate_penalty, 4),
                    "reasons": reasons,
                    "warnings": warnings,
                }
            )
            selected_so_far.append(asset)
        return evidence

    def _asset_relevance(
        self,
        asset: dict[str, object],
        query_terms: list[str],
        feedback_boosts: dict[str, float],
    ) -> float:
        query_score = self._asset_query_match(asset, query_terms)
        if query_terms:
            query_score = query_score / max(1, len(query_terms))
        else:
            query_score = 0.25
        concept_score = 0.1 * len(set(asset.get("tags", [])) & set(query_terms))
        feedback_score = feedback_boosts.get(str(asset["id"]), 0.0)
        return max(0.0, query_score + concept_score + feedback_score + 0.18)

    @staticmethod
    def _selection_similarity(
        candidate: dict[str, object],
        selected: list[dict[str, object]],
        similarity: dict[tuple[str, str], float],
    ) -> float:
        if not selected:
            return 0.0
        candidate_id = str(candidate["id"])
        max_similarity = 0.0
        for item in selected:
            pair = tuple(sorted([candidate_id, str(item["id"])]))
            max_similarity = max(max_similarity, similarity.get(pair, 0.0))
        return max_similarity

    @staticmethod
    def _duplicate_penalty(
        candidate: dict[str, object],
        selected: list[dict[str, object]],
    ) -> float:
        duplicate_group = candidate.get("duplicate_group_id")
        if not duplicate_group:
            return 0.0
        return 1.0 if any(item.get("duplicate_group_id") == duplicate_group for item in selected) else 0.0

    @staticmethod
    def _asset_to_retrieval_image(asset: dict[str, object]) -> dict[str, object]:
        return {
            "object": "retrieved_image",
            "id": asset["id"],
            "filename": asset["filename"],
            "relative_path": asset["relative_path"],
            "taken_at": asset["taken_at"],
            "place_name": asset["place_name"],
            "country": asset["country"],
            "description": asset["description"],
            "tags": asset["tags"],
            "score": round(float(asset.get("score") or asset.get("quality_score") or 0.0), 4),
            "matched_terms": asset.get("tags", [])[:6],
        }

    @staticmethod
    def _image_count(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT COUNT(*) AS count FROM image_index").fetchone()["count"])

    @staticmethod
    def _hidden_asset_ids(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            """
            SELECT target_id
            FROM atlas_feedback
            WHERE target_kind = 'asset' AND action IN ('hide', 'hide_similar')
            """
        ).fetchall()
        return {str(row["target_id"]) for row in rows}

    @staticmethod
    def _feedback_asset_ids(connection: sqlite3.Connection, action: str) -> set[str]:
        rows = connection.execute(
            """
            SELECT target_id
            FROM atlas_feedback
            WHERE target_kind = 'asset' AND action = ?
            """,
            (action,),
        ).fetchall()
        return {str(row["target_id"]) for row in rows}

    @staticmethod
    def _feedback_boosts(connection: sqlite3.Connection) -> dict[str, float]:
        boosts: dict[str, float] = defaultdict(float)
        rows = connection.execute(
            """
            SELECT target_id, action, weight
            FROM atlas_feedback
            WHERE target_kind = 'asset' AND action IN ('more_like', 'less_like')
            """
        ).fetchall()
        for row in rows:
            direction = 1.0 if row["action"] == "more_like" else -1.0
            boosts[str(row["target_id"])] += 0.18 * direction * float(row["weight"])
        return boosts

    @staticmethod
    def _similarity_lookup(connection: sqlite3.Connection) -> dict[tuple[str, str], float]:
        rows = connection.execute(
            "SELECT source_image_id, target_image_id, weight FROM atlas_edges"
        ).fetchall()
        return {
            tuple(sorted([str(row["source_image_id"]), str(row["target_image_id"])])): float(row["weight"])
            for row in rows
        }

    @staticmethod
    def _cluster_label_map(connection: sqlite3.Connection) -> dict[str, str]:
        rows = connection.execute(
            "SELECT id, label FROM atlas_clusters WHERE mode = 'semantic'"
        ).fetchall()
        return {str(row["id"]): str(row["label"]) for row in rows}


def normalize_mode(raw_mode: str | None) -> str:
    normalized = str(raw_mode or DEFAULT_MODE).strip().lower()
    return normalized if normalized in SUPPORTED_MODES else DEFAULT_MODE


def normalize_lens(raw_lens: str | None) -> str:
    normalized = str(raw_lens or DEFAULT_LENS).strip().lower()
    return normalized if normalized in SUPPORTED_LENSES else DEFAULT_LENS


def mode_for_lens(lens: str) -> str:
    return {
        "explore": "semantic",
        "story": "event",
        "map": "place",
        "people": "people",
        "cleanup": "duplicates",
        "similar": "semantic",
    }.get(normalize_lens(lens), "semantic")


def parse_limit(raw_limit: object) -> int:
    try:
        parsed = int(raw_limit)
    except (TypeError, ValueError):
        return DEFAULT_VISIBLE_LIMIT
    return max(40, min(MAX_VISIBLE_LIMIT, parsed))


def parse_bool(raw_value: object, *, default: bool = False) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}


def parse_float(raw_value: object) -> float | None:
    if raw_value is None or raw_value == "":
        return None
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


def best_assets(assets: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    return sorted(
        assets,
        key=lambda asset: (
            float(asset.get("quality_score") or 0.0),
            str(asset.get("taken_at") or ""),
            str(asset.get("filename") or ""),
        ),
        reverse=True,
    )[:limit]


def memory_score(assets: list[dict[str, object]]) -> float:
    if not assets:
        return 0.0
    quality = sum(float(asset.get("quality_score") or 0.0) for asset in assets) / len(assets)
    coverage = min(1.0, math.log2(len(assets) + 1) / 5.0)
    diversity = min(
        1.0,
        len({str(asset.get("event_id") or asset.get("cluster_id") or "") for asset in assets})
        / max(1, min(6, len(assets))),
    )
    return round(0.58 * quality + 0.26 * coverage + 0.16 * diversity, 4)


def memory_label(
    *,
    kind: str,
    fallback: str,
    top_concepts: list[str],
    place_label: str | None,
    time_label: str | None,
) -> str:
    concept = humanize_concept(top_concepts[0]) if top_concepts else humanize_concept(fallback)
    if kind == "place" and place_label:
        return place_label
    if kind == "event":
        parts = [part for part in [time_label, place_label, concept] if part]
        return " · ".join(parts[:3]) or concept
    if place_label and concept:
        return f"{place_label} · {concept}"
    if time_label and concept:
        return f"{time_label} · {concept}"
    return concept or "Memory"


def role_record(
    asset: dict[str, object],
    memory_id: str,
    role: str,
    confidence: float,
    reason: str,
) -> dict[str, object]:
    return {
        "image_id": asset["id"],
        "memory_id": memory_id,
        "role": role,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": reason,
    }


def best_matching_asset(
    assets: list[dict[str, object]],
    terms: list[str],
) -> dict[str, object] | None:
    scored: list[tuple[float, dict[str, object]]] = []
    for asset in assets:
        blob = normalize_text(
            " ".join(
                [
                    str(asset.get("filename") or ""),
                    str(asset.get("description") or ""),
                    str(asset.get("combined_text") or ""),
                    " ".join(str(tag) for tag in asset.get("tags", [])),
                ]
            )
        )
        match_score = sum(1.0 for term in terms if term in blob)
        if match_score <= 0:
            continue
        scored.append((match_score + float(asset.get("quality_score") or 0.0), asset))
    if not scored:
        return None
    return max(scored, key=lambda item: item[0])[1]


def memory_card_from_assets(
    *,
    memory_id: str,
    kind: str,
    label: str,
    assets: list[dict[str, object]],
) -> dict[str, object]:
    representatives = best_assets(assets, limit=5)
    top_concepts = top_terms(
        term
        for asset in assets
        for term in extract_concepts(
            [str(tag) for tag in asset.get("tags", [])],
            str(asset.get("combined_text") or asset.get("description") or ""),
        )
    )
    xs = [float(asset.get("x") or 0.0) for asset in assets]
    ys = [float(asset.get("y") or 0.0) for asset in assets]
    return {
        "object": "atlas.memory",
        "id": memory_id,
        "kind": kind,
        "label": label,
        "asset_count": len(assets),
        "asset_ids": [str(asset["id"]) for asset in assets],
        "representative_asset_ids": [str(asset["id"]) for asset in representatives],
        "representative_assets": representatives,
        "top_concepts": top_concepts,
        "place_label": common_label([str(asset.get("place_name") or asset.get("country") or "") for asset in assets]),
        "time_label": time_label_for_assets(assets),
        "x": sum(xs) / max(1, len(xs)),
        "y": sum(ys) / max(1, len(ys)),
        "score": memory_score(assets),
        "people_risk": max((float(asset.get("people_risk") or 0.0) for asset in assets), default=0.0),
        "duplicate_count": sum(1 for asset in assets if asset.get("duplicate_group_id")),
        "chapter_count": len({str(asset.get("event_id") or "") for asset in assets}),
        "best_assets": best_assets(assets, limit=9),
    }


def build_lens_summaries(
    assets: list[dict[str, object]],
    cleanup: dict[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "id": "explore",
            "label": "Explore",
            "count": len(assets),
            "summary": "Themes and representative photos",
        },
        {
            "id": "story",
            "label": "Story",
            "count": len({str(asset.get("event_id") or "") for asset in assets}),
            "summary": "Time and event chapters",
        },
        {
            "id": "map",
            "label": "Map",
            "count": len({str(asset.get("place_name") or asset.get("country") or "") for asset in assets if asset.get("place_name") or asset.get("country")}),
            "summary": "Places and trips",
        },
        {
            "id": "people",
            "label": "People",
            "count": sum(1 for asset in assets if float(asset.get("people_risk") or 0.0) >= 0.45),
            "summary": "People/no-people review",
        },
        {
            "id": "cleanup",
            "label": "Cleanup",
            "count": int(cleanup.get("duplicate_stack_count") or 0)
            + int(cleanup.get("low_quality_count") or 0),
            "summary": "Duplicates and quality issues",
        },
        {
            "id": "similar",
            "label": "Similar",
            "count": int(cleanup.get("similar_stack_count") or 0),
            "summary": "Compare related photos",
        },
    ]


def build_memory_chapters(assets: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for asset in assets:
        key = str(asset.get("event_id") or asset.get("taken_at") or "chapter_unknown")
        grouped[key].append(asset)
    chapters = []
    for key, members in grouped.items():
        chapters.append(
            {
                "id": key,
                "label": english_text(event_label(key) if key.startswith("event_") else key[:10], "Memory event"),
                "asset_count": len(members),
                "representative_assets": best_assets(members, limit=4),
                "time_label": time_label_for_assets(members),
                "place_label": common_label([str(asset.get("place_name") or asset.get("country") or "") for asset in members]),
            }
        )
    return sorted(chapters, key=lambda item: str(item.get("time_label") or item["label"]), reverse=True)


def memory_suggestions(
    row: sqlite3.Row,
    assets: list[dict[str, object]],
    stacks: list[dict[str, object]],
) -> list[str]:
    suggestions = []
    if assets:
        suggestions.append("Use the best 9 as a story-ready set.")
    if int(row["duplicate_count"]) > 0 or stacks:
        suggestions.append("Review similar bursts before exporting.")
    if float(row["people_risk"]) >= 0.45:
        suggestions.append("Check people/no-people corrections for this memory.")
    if not suggestions:
        suggestions.append("Pin this as a clean memory if the grouping feels right.")
    return suggestions


def build_library_summary(
    *,
    assets: list[dict[str, object]],
    memories: list[dict[str, object]],
    cleanup: dict[str, object],
    index_health: object,
) -> dict[str, object]:
    places = [
        english_text(label)
        for label, _count in Counter(
            str(asset.get("place_name") or asset.get("country") or "")
            for asset in assets
            if asset.get("place_name") or asset.get("country")
        ).most_common(6)
        if english_text(label)
    ]
    dated_assets = [str(asset.get("taken_at"))[:10] for asset in assets if asset.get("taken_at")]
    time_range = {
        "start": min(dated_assets) if dated_assets else None,
        "end": max(dated_assets) if dated_assets else None,
    }
    top_concepts = top_terms(
        term
        for asset in assets
        for term in extract_concepts(
            [str(tag) for tag in asset.get("tags", [])],
            str(asset.get("combined_text") or asset.get("description") or ""),
        )
    )
    people_count = sum(1 for asset in assets if float(asset.get("people_risk") or 0.0) >= 0.45)
    duplicate_count = int(cleanup.get("duplicate_stack_count") or 0) + int(cleanup.get("similar_stack_count") or 0)
    quality_avg = (
        round(sum(float(asset.get("quality_score") or 0.0) for asset in assets) / len(assets), 4)
        if assets
        else 0.0
    )
    strongest_memory = english_text(memories[0]["label"], "Memory") if memories else None
    return {
        "object": "atlas.library_summary",
        "asset_count": len(assets),
        "memory_count": len(memories),
        "top_concepts": top_concepts[:10],
        "places": places,
        "time_range": time_range,
        "people_risk_count": people_count,
        "duplicate_stack_count": duplicate_count,
        "quality_avg": quality_avg,
        "strongest_memory": strongest_memory,
        "summary": library_summary_sentence(
            asset_count=len(assets),
            top_concepts=top_concepts,
            places=places,
            strongest_memory=strongest_memory,
        ),
        "index_health": index_health,
    }


def build_inspiration_cards(
    *,
    memories: list[dict[str, object]],
    assets: list[dict[str, object]],
    cleanup: dict[str, object],
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    seen_prompts: set[str] = set()

    for memory in memories[:8]:
        concepts = english_terms(memory.get("top_concepts", []))[:4]
        prompt = prompt_for_memory(memory, social=True)
        if prompt in seen_prompts:
            continue
        seen_prompts.add(prompt)
        cards.append(
            {
                "object": "atlas.inspiration_card",
                "id": f"inspire_{slugify(str(memory['id']))}",
                "kind": str(memory.get("kind") or "memory"),
                "title": inspiration_title(memory),
                "summary": inspiration_summary(memory, concepts),
                "prompt": prompt,
                "memory_ids": [memory["id"]],
                "asset_ids": [str(asset_id) for asset_id in memory.get("representative_asset_ids", [])],
                "top_concepts": concepts,
                "confidence": round(float(memory.get("score") or 0.0), 4),
                "source": "local_index",
            }
        )

    mountain_assets = [
        asset
        for asset in assets
        if "mountain" in extract_concepts(
            [str(tag) for tag in asset.get("tags", [])],
            str(asset.get("combined_text") or asset.get("description") or ""),
        )
        and float(asset.get("people_risk") or 0.0) < 0.45
    ]
    if len(mountain_assets) >= 3:
        cards.insert(
            0,
            {
                "object": "atlas.inspiration_card",
                "id": "inspire_mountain_no_people",
                "kind": "theme",
                "title": "Quiet mountain set",
                "summary": "Mountain views with low people risk and low repetition.",
                "prompt": "Find 9 mountain landscape photos without people and with low repetition for a social post",
                "memory_ids": [],
                "asset_ids": [str(asset["id"]) for asset in best_assets(mountain_assets, limit=9)],
                "top_concepts": ["mountain", "landscape", "quiet"],
                "confidence": 0.92,
                "source": "local_index",
            },
        )

    if int(cleanup.get("duplicate_stack_count") or 0) or int(cleanup.get("similar_stack_count") or 0):
        cards.append(
            {
                "object": "atlas.inspiration_card",
                "id": "inspire_cleanup_review",
                "kind": "cleanup",
                "title": "Review similar photos",
                "summary": "Find repeated or weaker frames before making a set.",
                "prompt": "Review duplicate and highly similar photos without deleting anything",
                "memory_ids": [],
                "asset_ids": [],
                "top_concepts": ["cleanup", "duplicates"],
                "confidence": 0.78,
                "source": "local_index",
            }
        )

    return cards[:10]


def build_storylines(memories: list[dict[str, object]]) -> list[dict[str, object]]:
    story_memories = [
        memory
        for memory in memories
        if int(memory.get("asset_count") or 0) >= 3
    ][:8]
    return [
        {
            "object": "atlas.storyline",
            "id": f"story_{slugify(str(memory['id']))}",
            "title": storyline_title(memory),
            "summary": storyline_summary(memory),
            "prompt": prompt_for_memory(memory, social=False),
            "memory_ids": [memory["id"]],
            "asset_count": int(memory.get("asset_count") or 0),
            "chapter_count": int(memory.get("chapter_count") or 0),
            "top_concepts": english_terms(memory.get("top_concepts", []))[:5],
        }
        for memory in story_memories
    ]


def suggested_queries_from_memories(memories: list[dict[str, object]]) -> list[str]:
    queries = [prompt_for_memory(memory, social=True) for memory in memories[:5]]
    if not any("mountain" in query for query in queries):
        queries.append("Find 9 mountain landscape photos without people and with low repetition")
    queries.append("Suggest 3 publishable story themes from these photos")
    return list(dict.fromkeys(queries))[:8]


def parse_atlas_intent(text: str) -> dict[str, object]:
    query_text = str(text or "").strip()
    query_terms = normalize_query_terms(query_text)
    no_people_requested = query_requests_no_people(query_text)
    excluded_terms = sorted(HUMAN_TERMS) if no_people_requested else []
    return {
        "object": "atlas.intent",
        "kind": "find_set",
        "query_text": query_text,
        "target_count": target_count_from_query(query_text),
        "required_terms": query_terms,
        "excluded_terms": excluded_terms,
        "no_people_requested": no_people_requested,
        "diversity_requested": query_requests_diversity(query_text),
        "output_goal": "social_post" if query_mentions_social(query_text) else "curated_set",
        "style": style_from_query(query_text),
    }


def query_preview_warnings(
    *,
    text: str,
    assets: list[dict[str, object]],
    requested_no_people: bool,
) -> list[str]:
    warnings: list[str] = []
    if not assets:
        warnings.append("No matching photos were found in the current Atlas cache.")
    if requested_no_people and any(float(asset.get("people_risk") or 0.0) >= 0.45 for asset in assets):
        warnings.append("Some candidates still have people risk; review before generating.")
    if query_requests_diversity(text) and any(float(asset.get("_duplicate_penalty") or 0.0) > 0 for asset in assets):
        warnings.append("A few candidates are visually close; MemoLens penalized them during ranking.")
    return warnings


def evidence_reasons(
    *,
    asset: dict[str, object],
    matched_terms: list[str],
    requested_no_people: bool,
    diversity_score: float,
) -> list[str]:
    reasons: list[str] = []
    if matched_terms:
        reasons.append("Matches " + ", ".join(matched_terms[:3]))
    if float(asset.get("quality_score") or 0.0) >= 0.62:
        reasons.append("Strong quality score")
    if requested_no_people and float(asset.get("people_risk") or 0.0) < 0.45:
        reasons.append("Low people risk")
    if diversity_score >= 0.58:
        reasons.append("Adds visual variety")
    if not reasons:
        reasons.append("Useful representative candidate")
    return reasons[:4]


def evidence_warnings(
    *,
    asset: dict[str, object],
    requested_no_people: bool,
    duplicate_penalty: float,
) -> list[str]:
    warnings: list[str] = []
    if requested_no_people and float(asset.get("people_risk") or 0.0) >= 0.45:
        warnings.append("people risk")
    if duplicate_penalty > 0:
        warnings.append("similar to another selected photo")
    if float(asset.get("quality_score") or 0.0) < 0.42:
        warnings.append("lower quality")
    return warnings


def strip_internal_scores(asset: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in asset.items()
        if not str(key).startswith("_")
    }


def library_summary_sentence(
    *,
    asset_count: int,
    top_concepts: list[str],
    places: list[str],
    strongest_memory: str | None,
) -> str:
    if asset_count <= 0:
        return "No Atlas cache is available yet. Rebuild after indexing to see your photo map."
    concept_text = ", ".join(humanize_concept(term) for term in top_concepts[:3]) or "mixed memories"
    place_text = f" around {', '.join(places[:2])}" if places else ""
    memory_text = f" The strongest current memory is {strongest_memory}." if strongest_memory else ""
    return f"{asset_count} indexed photos cluster around {concept_text}{place_text}.{memory_text}"


def prompt_for_memory(memory: dict[str, object], *, social: bool) -> str:
    label = english_text(memory.get("label"), "this memory")
    concepts = ", ".join(english_terms(memory.get("top_concepts", [])[:3]))
    base = f"Pick 9 photos from {label}"
    if concepts:
        base += f", emphasizing {concepts}"
    base += ", with low repetition"
    if float(memory.get("people_risk") or 0.0) < 0.45:
        base += ", without people"
    if social:
        base += ", for a social post"
    else:
        base += ", arranged as a natural storyline"
    return base


def inspiration_title(memory: dict[str, object]) -> str:
    label = english_text(memory.get("label"), "Memory")
    if len(label) > 42:
        return label[:39] + "..."
    return label


def inspiration_summary(memory: dict[str, object], concepts: list[str]) -> str:
    concept_text = ", ".join(concepts[:3]) or "mixed scenes"
    return f"{int(memory.get('asset_count') or 0)} photos around {concept_text}."


def storyline_title(memory: dict[str, object]) -> str:
    label = english_text(memory.get("label"), "Story")
    return f"Story: {label}" if not label.lower().startswith("story") else label


def storyline_summary(memory: dict[str, object]) -> str:
    parts = [
        f"{int(memory.get('asset_count') or 0)} photos",
        f"{int(memory.get('chapter_count') or 0)} chapters",
    ]
    if memory.get("time_label"):
        parts.append(str(memory["time_label"]))
    if memory.get("place_label"):
        place_label = english_text(memory["place_label"])
        if place_label:
            parts.append(place_label)
    return " · ".join(parts)


def target_count_from_query(text: str) -> int:
    normalized = normalize_text(text)
    photo_count_match = re.search("([1-9]|[12]\\d|3[0-6])\\s*\u5f20", text)
    if photo_count_match:
        return int(photo_count_match.group(1))
    digit_match = re.search(r"\b([1-9]|[12]\d|3[0-6])\b", normalized)
    if digit_match:
        return int(digit_match.group(1))
    chinese_counts = {
        "\u4e00": 1,
        "\u4e8c": 2,
        "\u4e24": 2,
        "\u4e09": 3,
        "\u56db": 4,
        "\u4e94": 5,
        "\u516d": 6,
        "\u4e03": 7,
        "\u516b": 8,
        "\u4e5d": 9,
    }
    for token, value in chinese_counts.items():
        if f"{token}\u5f20" in text or f"{token} \u5f20" in text:
            return value
    return 9


def query_requests_no_people(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in ABSENCE_PHRASES)


def query_requests_diversity(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in DIVERSITY_PHRASES)


def query_mentions_social(text: str) -> bool:
    normalized = normalize_text(text)
    return any(phrase in normalized for phrase in SOCIAL_POST_PHRASES)


def style_from_query(text: str) -> str | None:
    normalized = normalize_text(text)
    if any(term in normalized for term in {"\u6e29\u67d4", "\u67d4\u548c", "soft", "gentle"}):
        return "soft"
    if any(term in normalized for term in {"\u5b89\u9759", "quiet", "calm"}):
        return "quiet"
    if any(term in normalized for term in {"\u81ea\u7136", "natural", "\u65e5\u5e38", "everyday"}):
        return "natural"
    return None


def decode_embedding(raw_embedding: object) -> np.ndarray | None:
    if raw_embedding is None:
        return None
    if isinstance(raw_embedding, memoryview):
        raw_bytes = raw_embedding.tobytes()
    elif isinstance(raw_embedding, bytearray):
        raw_bytes = bytes(raw_embedding)
    elif isinstance(raw_embedding, bytes):
        raw_bytes = raw_embedding
    else:
        return None
    if not raw_bytes:
        return None
    vector = np.frombuffer(raw_bytes, dtype=np.float32)
    if vector.size == 0:
        return None
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return None
    return (vector / norm).astype(np.float32)


def most_common_dimension(dimensions: list[int]) -> int:
    if not dimensions:
        return 64
    return Counter(dimensions).most_common(1)[0][0]


def hashed_text_vector(text: str, dimension: int) -> np.ndarray:
    vector = np.zeros(max(8, dimension), dtype=np.float32)
    for token in normalize_text(text).split():
        index = abs(hash(token)) % vector.size
        vector[index] += 1.0
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        vector[0] = 1.0
        return vector
    return vector / norm


def project_vectors(vectors: np.ndarray) -> np.ndarray:
    count = vectors.shape[0]
    if count == 1:
        return np.zeros((1, 2), dtype=np.float32)
    centered = vectors - vectors.mean(axis=0, keepdims=True)
    try:
        u, singular_values, _vh = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:
        return fallback_circle_layout(count)
    if u.size == 0 or singular_values.size == 0:
        return fallback_circle_layout(count)
    first = u[:, 0] * singular_values[0]
    second = u[:, 1] * singular_values[1] if singular_values.size > 1 else np.zeros(count)
    coords = np.column_stack([first, second]).astype(np.float32)
    return normalize_coords(coords)


def fallback_circle_layout(count: int) -> np.ndarray:
    if count <= 0:
        return np.zeros((0, 2), dtype=np.float32)
    angles = np.linspace(0, 2 * math.pi, count, endpoint=False)
    return np.column_stack([np.cos(angles) * 800, np.sin(angles) * 800]).astype(np.float32)


def normalize_coords(coords: np.ndarray) -> np.ndarray:
    normalized = coords.astype(np.float32, copy=True)
    for axis in range(2):
        values = normalized[:, axis]
        min_value = float(np.min(values))
        max_value = float(np.max(values))
        if math.isclose(min_value, max_value):
            normalized[:, axis] = 0.0
        else:
            normalized[:, axis] = ((values - min_value) / (max_value - min_value) - 0.5) * 2000.0
    return normalized


def cluster_count_for(item_count: int) -> int:
    if item_count <= 2:
        return item_count
    return max(2, min(28, round(math.sqrt(item_count) * 1.35)))


def kmeans_cluster(coords: np.ndarray, cluster_count: int) -> np.ndarray:
    item_count = coords.shape[0]
    if item_count == 0:
        return np.zeros(0, dtype=np.int32)
    cluster_count = max(1, min(cluster_count, item_count))
    if cluster_count == 1:
        return np.zeros(item_count, dtype=np.int32)

    order = np.argsort(coords[:, 0] + 0.25 * coords[:, 1])
    seed_indices = order[np.linspace(0, item_count - 1, cluster_count).astype(int)]
    centers = coords[seed_indices].astype(np.float32, copy=True)
    labels = np.zeros(item_count, dtype=np.int32)

    for _ in range(18):
        distances = np.linalg.norm(coords[:, None, :] - centers[None, :, :], axis=2)
        next_labels = np.argmin(distances, axis=1).astype(np.int32)
        if np.array_equal(labels, next_labels):
            break
        labels = next_labels
        for cluster_index in range(cluster_count):
            members = coords[labels == cluster_index]
            if members.size > 0:
                centers[cluster_index] = members.mean(axis=0)
    return labels


def build_similarity_graph(
    *,
    rows: list[sqlite3.Row],
    vectors: np.ndarray,
) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, float]]], dict[str, str]]:
    image_ids = [str(row["id"]) for row in rows]
    count = len(image_ids)
    if count == 0:
        return {}, {}, {}

    similarity = np.clip(vectors @ vectors.T, 0.0, 1.0)
    np.fill_diagonal(similarity, -1.0)
    neighbor_ids: dict[str, list[str]] = {}
    edge_weights: dict[str, list[tuple[str, float]]] = {}
    union_find = UnionFind(image_ids)

    for index, image_id in enumerate(image_ids):
        neighbor_count = min(NEIGHBOR_COUNT, count - 1)
        if neighbor_count <= 0:
            neighbor_ids[image_id] = []
            edge_weights[image_id] = []
            continue
        top_indices = np.argpartition(-similarity[index], neighbor_count - 1)[:neighbor_count]
        ranked_indices = sorted(top_indices, key=lambda item: float(similarity[index, item]), reverse=True)
        neighbors: list[str] = []
        weights: list[tuple[str, float]] = []
        for target_index in ranked_indices:
            weight = float(similarity[index, target_index])
            if weight < 0:
                continue
            target_id = image_ids[target_index]
            neighbors.append(target_id)
            weights.append((target_id, weight))
            if weight >= NEAR_DUPLICATE_THRESHOLD:
                union_find.union(image_id, target_id)
        neighbor_ids[image_id] = neighbors
        edge_weights[image_id] = weights

    duplicate_groups: dict[str, str] = {}
    grouped: dict[str, list[str]] = defaultdict(list)
    for image_id in image_ids:
        grouped[union_find.find(image_id)].append(image_id)
    for index, members in enumerate(grouped.values()):
        if len(members) < 2:
            continue
        duplicate_group_id = f"dup_{index:03d}"
        for image_id in members:
            duplicate_groups[image_id] = duplicate_group_id
    return neighbor_ids, edge_weights, duplicate_groups


class UnionFind:
    def __init__(self, items: Iterable[str]):
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def label_semantic_clusters(assets: list[dict[str, object]]) -> dict[int, dict[str, object]]:
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for asset in assets:
        grouped[int(asset["cluster_index"])].append(asset)

    labels: dict[int, dict[str, object]] = {}
    for cluster_index, members in grouped.items():
        concept_counts = Counter(
            concept
            for asset in members
            for concept in asset.get("concepts", [])
        )
        top_concepts = [term for term, _count in concept_counts.most_common(5)]
        place_label = common_label(
            [str(asset.get("place_name") or asset.get("country") or "") for asset in members]
        )
        time_label = time_label_for_assets(members)
        core_label = humanize_concept(top_concepts[0]) if top_concepts else "Memory region"
        if place_label and top_concepts:
            label = f"{place_label} · {core_label}"
        elif time_label and top_concepts:
            label = f"{time_label} · {core_label}"
        else:
            label = core_label
        representative = max(
            members,
            key=lambda item: (float(item["quality_score"]), str(item.get("taken_at") or "")),
        )
        xs = [float(asset["x"]) for asset in members]
        ys = [float(asset["y"]) for asset in members]
        labels[cluster_index] = {
            "label": label,
            "count": len(members),
            "representative_image_id": representative["id"],
            "top_concepts": top_concepts,
            "place_label": place_label,
            "time_label": time_label,
            "x": sum(xs) / len(xs),
            "y": sum(ys) / len(ys),
            "bounds": {
                "min_x": min(xs),
                "max_x": max(xs),
                "min_y": min(ys),
                "max_y": max(ys),
            },
        }
    return labels


def parse_tags(raw_tags: object) -> list[str]:
    if isinstance(raw_tags, list):
        return [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    if not isinstance(raw_tags, str):
        return []
    try:
        parsed = json.loads(raw_tags)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(tag).strip() for tag in parsed if str(tag).strip()]


def parse_json_list(raw_value: object) -> list[str]:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def extract_concepts(tags: list[str], text: str) -> list[str]:
    concepts: list[str] = []
    for raw_term in [*tags, *normalize_text(text).split()]:
        term = normalize_concept(raw_term)
        if not term or term in GENERIC_TERMS or len(term) < 3:
            continue
        if term not in concepts:
            concepts.append(term)
        if len(concepts) >= 16:
            break
    return concepts


def normalize_concept(raw_term: str) -> str:
    normalized = normalize_text(raw_term).strip()
    if not normalized:
        return ""
    if normalized in CONCEPT_ALIASES:
        return CONCEPT_ALIASES[normalized]
    if HAN_TEXT_RE.search(normalized):
        return ""
    return normalized


def english_text(value: object, fallback: str = "") -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return fallback
    for source, target in CONCEPT_ALIASES.items():
        if HAN_TEXT_RE.search(source):
            cleaned = cleaned.replace(source, humanize_concept(target))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned and not HAN_TEXT_RE.search(cleaned) else fallback


def english_terms(values: Iterable[object]) -> list[str]:
    terms: list[str] = []
    for value in values:
        normalized = normalize_concept(str(value))
        if normalized and normalized not in terms:
            terms.append(normalized)
            continue
        cleaned = english_text(value)
        if cleaned and cleaned not in terms:
            terms.append(cleaned)
    return terms


def normalize_text(text: str) -> str:
    normalized = str(text or "").lower()
    normalized = re.sub(r"[_/.,;:!?()[\]{}|]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def normalize_query_terms(text: str) -> list[str]:
    normalized_text = normalize_text(text)
    absence_requested = any(phrase in normalized_text for phrase in ABSENCE_PHRASES)
    terms: list[str] = []
    for raw_alias, concept in CONCEPT_ALIASES.items():
        if raw_alias in normalized_text and concept not in terms:
            terms.append(concept)
    for token in normalized_text.split():
        term = normalize_concept(token)
        if absence_requested and term in {"people", "person", "human"}:
            continue
        if token.isdigit():
            continue
        if not term or term in GENERIC_TERMS or term in terms:
            continue
        terms.append(term)
    return terms


def context_terms_from_assets(assets: list[dict[str, object]], limit: int = 10) -> list[str]:
    counter: Counter[str] = Counter()
    for asset in assets:
        concepts = extract_concepts(
            [str(tag) for tag in asset.get("tags", [])],
            str(asset.get("combined_text") or asset.get("description") or ""),
        )
        for concept in concepts[:8]:
            counter[concept] += 1
    return [term for term, _count in counter.most_common(limit)]


def append_context_terms(text: str, context_terms: list[str]) -> str:
    cleaned_text = str(text or "").strip()
    unique_terms = [term for term in context_terms if term and term not in normalize_query_terms(cleaned_text)]
    if not unique_terms:
        return cleaned_text
    if not cleaned_text:
        return " ".join(unique_terms[:8])
    return f"{cleaned_text} {' '.join(unique_terms[:8])}"


def people_risk(tags: list[str], text: str) -> float:
    blob = normalize_text(" ".join([*tags, text]))
    if any(phrase in blob for phrase in ABSENCE_PHRASES):
        return 0.0
    tokens = set(blob.split())
    matched = tokens & HUMAN_TERMS
    if not matched:
        return 0.0
    if {"portrait", "selfie", "face"} & matched:
        return 1.0
    return min(1.0, 0.38 + 0.16 * len(matched))


def coerce_score(raw_value: object) -> float | None:
    if raw_value is None:
        return None
    try:
        score = float(raw_value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score):
        return None
    return max(0.0, min(1.0, score))


def build_event_id(row: sqlite3.Row, cluster_index: int) -> str:
    date_label = "unknown_date"
    taken_at = row["taken_at"]
    if isinstance(taken_at, str) and len(taken_at) >= 10:
        date_label = taken_at[:10]
    place = slugify(str(row["place_name"] or row["country"] or "local"))
    return f"event_{date_label}_{place}_{cluster_index:02d}"


def transform_asset_position(asset: dict[str, object], mode: str) -> tuple[float, float]:
    base_x = float(asset["base_x"])
    base_y = float(asset["base_y"])
    if mode == "semantic":
        return base_x, base_y
    if mode == "time":
        return time_x(asset.get("taken_at")), base_y
    if mode == "place":
        lon = asset.get("lon")
        lat = asset.get("lat")
        if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
            return max(-1000.0, min(1000.0, float(lon) * 8.0)), max(-1000.0, min(1000.0, float(lat) * 14.0))
        return base_x, base_y
    if mode == "event":
        return time_x(asset.get("taken_at")), event_y(str(asset.get("event_id") or ""))
    if mode == "people":
        return -900.0 + 1800.0 * float(asset.get("people_risk") or 0.0), base_y
    if mode == "quality":
        return -950.0 + 1900.0 * float(asset.get("quality_score") or 0.0), base_y
    if mode == "duplicates":
        duplicate_group = asset.get("duplicate_group_id")
        if duplicate_group:
            return -950.0 + (abs(hash(str(duplicate_group))) % 1900), base_y
        return 860.0, base_y
    return base_x, base_y


def mode_cluster_for(asset: dict[str, object], mode: str) -> tuple[str, str]:
    if mode == "time":
        taken_at = str(asset.get("taken_at") or "")
        label = taken_at[:7] if len(taken_at) >= 7 else "Unknown time"
        return f"time_{slugify(label)}", label
    if mode == "place":
        label = str(asset.get("place_name") or asset.get("country") or "Local library")
        return f"place_{slugify(label)}", label
    if mode == "event":
        event_id = str(asset.get("event_id") or "event_unknown")
        return event_id, event_label(event_id)
    if mode == "people":
        has_people = float(asset.get("people_risk") or 0.0) >= 0.45
        return ("people_present", "People likely") if has_people else ("people_absent", "No people detected")
    if mode == "quality":
        quality = float(asset.get("quality_score") or 0.0)
        if quality >= 0.72:
            return "quality_high", "Presentation quality"
        if quality >= 0.45:
            return "quality_mid", "Usable quality"
        return "quality_low", "Needs review"
    if mode == "duplicates":
        duplicate_group = asset.get("duplicate_group_id")
        if duplicate_group:
            return str(duplicate_group), "Similar burst"
        return "duplicate_unique", "Distinct photos"
    return str(asset["cluster_id"]), str(asset.get("cluster_label") or "Semantic group")


def time_x(raw_taken_at: object) -> float:
    if not isinstance(raw_taken_at, str) or len(raw_taken_at) < 10:
        return -1050.0
    try:
        parsed = datetime.fromisoformat(raw_taken_at.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw_taken_at[:10])
        except ValueError:
            return -1050.0
    timestamp = parsed.timestamp()
    year_span = 365.25 * 24 * 60 * 60 * 8
    now = datetime.now(timezone.utc).timestamp()
    return max(-1000.0, min(1000.0, ((timestamp - (now - year_span)) / year_span - 0.5) * 2000.0))


def event_y(event_id: str) -> float:
    return -900.0 + (abs(hash(event_id)) % 1800)


def event_label(event_id: str) -> str:
    cleaned = event_id.removeprefix("event_")
    parts = [part for part in cleaned.split("_") if part]
    if not parts:
        return "Memory event"
    if len(parts) >= 2:
        return f"{parts[0]} · {parts[1].replace('-', ' ')}"
    return parts[0].replace("-", " ")


def common_label(values: list[str]) -> str | None:
    cleaned = [value.strip() for value in values if value.strip() and not HAN_TEXT_RE.search(value)]
    if not cleaned:
        return None
    return Counter(cleaned).most_common(1)[0][0]


def time_label_for_assets(assets: list[dict[str, object]]) -> str | None:
    labels = [
        str(asset.get("taken_at"))[:7]
        for asset in assets
        if isinstance(asset.get("taken_at"), str) and len(str(asset.get("taken_at"))) >= 7
    ]
    if not labels:
        return None
    return Counter(labels).most_common(1)[0][0]


def top_terms(terms: Iterable[str], limit: int = 6) -> list[str]:
    counter = Counter(term for term in terms if term and term not in GENERIC_TERMS)
    return [term for term, _count in counter.most_common(limit)]


def humanize_concept(concept: str) -> str:
    if HAN_TEXT_RE.search(concept):
        return "Memory"
    return concept.replace("_", " ").replace("-", " ").title()


def title_from_filename(filename: str) -> str:
    return re.sub(r"\.[^.]+$", "", filename).replace("_", " ").replace("-", " ")


def slugify(value: str) -> str:
    normalized = normalize_text(value)
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", normalized).strip("-")
    return slug[:48] or "group"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
