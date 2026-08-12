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

    def search(self, query: str, limit: int) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            columns = self.columns_or_error(connection)
            self.validate_identity(columns)
            try:
                scored, scanned_count = self._rank_rows(
                    connection.execute(
                        f"SELECT {', '.join(self._select_parts(columns))} "
                        "FROM image_index"
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
        results = [compact_asset(raw, self.library_dir) for *_, raw in scored]
        return {
            "object": "memolens.search",
            "status": "completed",
            "source": "sqlite_fallback",
            "ranking": "deterministic_lexical",
            "query": query,
            "result_count": len(results),
            "scanned_count": scanned_count,
            "results": results,
            "database_path": str(self.database.path),
            "safety": safety_summary(),
        }

    @staticmethod
    def _select_parts(columns: set[str]) -> list[str]:
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
        return [
            f'"{name}" AS "{name}"' if name in columns else f'NULL AS "{name}"'
            for name in desired
        ]

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
