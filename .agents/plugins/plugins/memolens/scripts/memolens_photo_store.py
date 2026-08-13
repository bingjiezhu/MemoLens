"""Legacy photo-index reads and bounded lexical ranking."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from memolens_contracts import (
    MemoLensError,
    compact_asset,
    parse_tags,
    quality_value,
    query_terms,
    safety_summary,
)
from memolens_ranking import RankedRow, keep_best, lexical_score
from memolens_sqlite import ReadOnlyDatabase


class PhotoIndexReader:
    """Read the compatibility photo index without opening media files."""

    def __init__(
        self, database: ReadOnlyDatabase, library_dir: Path | None
    ) -> None:
        self.database = database
        self.library_dir = library_dir

    def configure_library(self, library_dir: Path | None) -> None:
        self.library_dir = library_dir

    @staticmethod
    def validate_identity(columns: set[str]) -> None:
        if columns and not {"id", "filename", "relative_path"}.issubset(columns):
            raise MemoLensError(
                "The SQLite image_index table is missing required MemoLens columns.",
                code="database_identity_mismatch",
            )

    def columns_or_error(self, connection: sqlite3.Connection) -> set[str]:
        columns = self.database.columns(connection, "image_index")
        if not columns:
            raise MemoLensError(
                "The SQLite file does not contain the legacy MemoLens image index.",
                code="capability_unavailable",
            )
        return columns

    @staticmethod
    def count(connection: sqlite3.Connection, columns: set[str]) -> int:
        if not columns:
            return 0
        return int(connection.execute("SELECT COUNT(*) FROM image_index").fetchone()[0])

    @staticmethod
    def embedding_backends(
        connection: sqlite3.Connection, columns: set[str]
    ) -> list[dict[str, Any]]:
        if "embedding_backend" not in columns:
            return []
        return [
            {"name": row[0], "count": int(row[1])}
            for row in connection.execute(
                "SELECT embedding_backend, COUNT(*) FROM image_index "
                "GROUP BY embedding_backend ORDER BY COUNT(*) DESC"
            ).fetchall()
        ]

    def search(
        self,
        query: str,
        limit: int,
        *,
        require_media_provenance: bool = False,
        include_paths: bool = True,
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            columns = self.columns_or_error(connection)
            self.validate_identity(columns)
            try:
                select_parts, from_sql, where_sql = self._search_projection(
                    connection,
                    columns,
                    require_media_provenance=require_media_provenance,
                )
                scored, scanned_count = self._rank_rows(
                    connection.execute(
                        f"SELECT {', '.join(select_parts)} {from_sql} {where_sql}"
                    ),
                    query=query,
                    limit=limit,
                )
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens SQLite index could not be searched.",
                    code="database_unavailable",
                ) from exc
        scored.sort(key=lambda item: item[:3], reverse=True)
        results = [
            compact_asset(raw, self.library_dir if include_paths else None)
            for *_, raw in scored
        ]
        if not include_paths:
            for result in results:
                result.pop("absolute_path", None)
                result.pop("path_status", None)
        return {
            "object": "memolens.search",
            "status": "completed",
            "source": "sqlite_fallback",
            "ranking": "deterministic_lexical",
            "query": query,
            "result_count": len(results),
            "scanned_count": scanned_count,
            "results": results,
            "safety": safety_summary(),
        }

    @staticmethod
    def _select_parts(columns: set[str], *, alias: str = "") -> list[str]:
        desired = [
            "id",
            "filename",
            "relative_path",
            "taken_at",
            "place_name",
            "country",
            "description",
            "tags_json",
            "combined_text",
            "aesthetic_score",
            "technical_quality_score",
        ]
        prefix = f'{alias}.' if alias else ""
        return [
            f'{prefix}"{name}" AS "{name}"'
            if name in columns
            else f'NULL AS "{name}"'
            for name in desired
        ]

    def _search_projection(
        self,
        connection: sqlite3.Connection,
        image_columns: set[str],
        *,
        require_media_provenance: bool,
    ) -> tuple[list[str], str, str]:
        asset_columns = self.database.columns(connection, "assets")
        source_columns = self.database.columns(connection, "asset_sources")
        has_asset_identity = {"id", "kind", "sha256"}.issubset(asset_columns)
        has_source_identity = {"id", "asset_id", "relative_path"}.issubset(
            source_columns
        )
        has_filename = bool({"display_filename", "filename"} & source_columns)
        has_availability = bool({"availability", "status"} & source_columns)
        identity_join = None
        if has_asset_identity and "sha256" in image_columns:
            identity_join = "a.sha256=i.sha256"
        elif has_asset_identity and "id" in image_columns:
            identity_join = "a.id=i.id"
        media_join_available = bool(
            identity_join
            and has_source_identity
            and has_filename
            and has_availability
        )
        if not media_join_available:
            if require_media_provenance:
                raise MemoLensError(
                    "Photo results do not yet expose stable asset source provenance.",
                    code="capability_unavailable",
                )
            return (
                self._select_parts(image_columns, alias="i"),
                "FROM image_index i",
                "",
            )

        filename_column = (
            "display_filename" if "display_filename" in source_columns else "filename"
        )
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        preferred_order = (
            "CASE WHEN src2.is_preferred=1 THEN 0 ELSE 1 END,"
            if "is_preferred" in source_columns
            else ""
        )
        select_parts = self._select_parts(image_columns, alias="i")
        # The source binding, not the legacy image row ID or path, is the timeline
        # provenance authority.
        select_parts.extend(
            [
                "a.id AS asset_id",
                "a.sha256 AS asset_sha256",
                "src.id AS asset_source_id",
                f"src.{availability_column} AS source_availability",
                f"src.{filename_column} AS filename",
            ]
        )
        review_columns = self.database.columns(
            connection, "asset_review_revisions"
        )
        has_reviews = {
            "asset_id",
            "revision",
            "inbox_state",
            "favorite",
            "project_ready",
        }.issubset(review_columns)
        if has_reviews:
            select_parts.extend(
                [
                    "COALESCE(rv.revision,0) AS review_revision",
                    "COALESCE(rv.inbox_state,'inbox') AS inbox_state",
                    "COALESCE(rv.favorite,0) AS favorite",
                    "COALESCE(rv.project_ready,0) AS project_ready",
                ]
            )
            review_join = (
                " LEFT JOIN asset_review_revisions rv ON rv.asset_id=a.id "
                "AND rv.revision=(SELECT MAX(rv2.revision) "
                "FROM asset_review_revisions rv2 WHERE rv2.asset_id=a.id)"
            )
            archived_filter = " AND COALESCE(rv.inbox_state,'inbox')<>'archived'"
        else:
            select_parts.extend(
                [
                    "0 AS review_revision",
                    "'inbox' AS inbox_state",
                    "0 AS favorite",
                    "0 AS project_ready",
                ]
            )
            review_join = ""
            archived_filter = ""
        root_join = ""
        if (
            "library_root_id" in source_columns
            and self.database.relation_exists(connection, "library_roots")
            and {"id", "status"}.issubset(
                self.database.columns(connection, "library_roots")
            )
        ):
            root_join = (
                " JOIN library_roots root ON root.id=src.library_root_id "
                "AND root.status='active'"
            )
        from_sql = (
            f"FROM image_index i JOIN assets a ON {identity_join} AND a.kind='image' "
            "JOIN asset_sources src ON src.id=(SELECT src2.id FROM asset_sources src2 "
            "WHERE src2.asset_id=a.id ORDER BY "
            f"CASE WHEN src2.{availability_column}='available' THEN 0 ELSE 1 END,"
            f"{preferred_order}src2.id LIMIT 1)"
            f"{root_join}{review_join}"
        )
        where_sql = (
            f"WHERE src.{availability_column}='available'{archived_filter}"
        )
        return select_parts, from_sql, where_sql

    def _rank_rows(
        self, cursor: sqlite3.Cursor, *, query: str, limit: int
    ) -> tuple[list[RankedRow], int]:
        phrase = query.casefold()
        terms = query_terms(query)
        scored: list[RankedRow] = []
        scanned_count = 0
        sequence = 0
        while rows := cursor.fetchmany(512):
            for row in rows:
                scanned_count += 1
                scored_row = self._score_row(dict(row), query, phrase, terms)
                if scored_row is None:
                    continue
                score, prepared = scored_row
                candidate = (
                    score,
                    str(prepared.get("taken_at") or ""),
                    sequence,
                    prepared,
                )
                sequence += 1
                keep_best(scored, candidate, limit)
        return scored, scanned_count

    @staticmethod
    def _score_row(
        raw: dict[str, Any], query: str, phrase: str, terms: list[str]
    ) -> tuple[float, dict[str, Any]] | None:
        tags = parse_tags(raw.get("tags_json"))
        fields = {
            "filename": str(raw.get("filename") or "").casefold(),
            "path": str(raw.get("relative_path") or "").casefold(),
            "place": " ".join(
                str(raw.get(key) or "") for key in ("place_name", "country")
            ).casefold(),
            "description": str(raw.get("description") or "").casefold(),
            "tags": " ".join(tags).casefold(),
            "combined": str(raw.get("combined_text") or "").casefold(),
        }
        score, matched = lexical_score(
            fields,
            query=query,
            phrase=phrase,
            terms=terms,
            weights={
                "tags": 3.0,
                "place": 2.5,
                "description": 2.0,
                "filename|path": 1.5,
                "combined": 1.0,
            },
        )
        if score <= 0:
            return None
        score += quality_value(
            raw.get("aesthetic_score"), raw.get("technical_quality_score")
        ) * 0.25
        raw["tags"] = tags
        raw["score"] = round(score, 4)
        raw["matched_terms"] = list(dict.fromkeys(matched))
        return score, raw
