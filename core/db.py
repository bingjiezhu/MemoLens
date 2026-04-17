from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .schemas import StoredImageRecord


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS image_index (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    taken_at TEXT,
    lat REAL,
    lon REAL,
    altitude REAL,
    place_name TEXT,
    country TEXT,
    description TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    combined_text TEXT NOT NULL,
    text_embedding_model TEXT,
    combined_text_embedding BLOB,
    embedding_backend TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_image_index_relative_path
    ON image_index(relative_path);

CREATE INDEX IF NOT EXISTS idx_image_index_taken_at
    ON image_index(taken_at);

CREATE INDEX IF NOT EXISTS idx_image_index_place_name
    ON image_index(place_name);
"""

FALLBACK_DESCRIPTION_PREFIX = "Local image file named "


class ImageIndexRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            if not self._table_exists(connection, "image_index"):
                connection.execute(CREATE_TABLE_SQL)
                self._ensure_indexes(connection)
                return

            columns = self._column_names(connection, "image_index")
            if "embedding_backend" not in columns or "embedding" not in columns:
                self._migrate_legacy_schema(connection)
                columns = self._column_names(connection, "image_index")

            self._ensure_text_columns(connection, columns)
            self._ensure_indexes(connection)

    def upsert(self, record: StoredImageRecord) -> None:
        self.delete_by_relative_path(record.relative_path, keep_id=record.id)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO image_index (
                    id,
                    sha256,
                    filename,
                    relative_path,
                    mime_type,
                    file_size,
                    width,
                    height,
                    taken_at,
                    lat,
                    lon,
                    altitude,
                    place_name,
                    country,
                    description,
                    tags_json,
                    combined_text,
                    text_embedding_model,
                    combined_text_embedding,
                    embedding_backend,
                    embedding,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    filename = excluded.filename,
                    relative_path = excluded.relative_path,
                    mime_type = excluded.mime_type,
                    file_size = excluded.file_size,
                    width = excluded.width,
                    height = excluded.height,
                    taken_at = excluded.taken_at,
                    lat = excluded.lat,
                    lon = excluded.lon,
                    altitude = excluded.altitude,
                    place_name = excluded.place_name,
                    country = excluded.country,
                    description = excluded.description,
                    tags_json = excluded.tags_json,
                    combined_text = excluded.combined_text,
                    text_embedding_model = excluded.text_embedding_model,
                    combined_text_embedding = excluded.combined_text_embedding,
                    embedding_backend = excluded.embedding_backend,
                    embedding = excluded.embedding,
                    updated_at = excluded.updated_at
                """,
                (
                    record.id,
                    record.sha256,
                    record.filename,
                    record.relative_path,
                    record.mime_type,
                    record.file_size,
                    record.width,
                    record.height,
                    record.taken_at,
                    record.lat,
                    record.lon,
                    record.altitude,
                    record.place_name,
                    record.country,
                    record.description,
                    json.dumps(record.tags),
                    record.combined_text,
                    record.text_embedding_model,
                    record.combined_text_embedding_blob,
                    record.embedding_backend,
                    record.embedding_blob,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def has_sha256(self, sha256: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM image_index WHERE sha256 = ? LIMIT 1",
                (sha256,),
            ).fetchone()
        return row is not None

    def get_by_sha256(self, sha256: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT
                    id,
                    sha256,
                    filename,
                    relative_path
                FROM image_index
                WHERE sha256 = ?
                LIMIT 1
                """,
                (sha256,),
            ).fetchone()

    def refresh_existing_file_metadata(
        self,
        *,
        sha256: str,
        filename: str,
        relative_path: str,
        mime_type: str,
        file_size: int,
        width: int | None,
        height: int | None,
        taken_at: str | None,
        lat: float | None,
        lon: float | None,
        altitude: float | None,
        updated_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE image_index
                SET
                    filename = ?,
                    relative_path = ?,
                    mime_type = ?,
                    file_size = ?,
                    width = ?,
                    height = ?,
                    taken_at = ?,
                    lat = ?,
                    lon = ?,
                    altitude = ?,
                    updated_at = ?
                WHERE sha256 = ?
                """,
                (
                    filename,
                    relative_path,
                    mime_type,
                    file_size,
                    width,
                    height,
                    taken_at,
                    lat,
                    lon,
                    altitude,
                    updated_at,
                    sha256,
                ),
            )

    def summarize_index_health(self) -> dict[str, int | float | bool]:
        summary: dict[str, int | float | bool] = {
            "total_records": 0,
            "fallback_records": 0,
            "fallback_ratio": 0.0,
            "needs_reindex": False,
        }
        if not self.db_path.exists():
            return summary

        with self._connect() as connection:
            if not self._table_exists(connection, "image_index"):
                return summary

            total_row = connection.execute("SELECT COUNT(*) AS count FROM image_index").fetchone()
            fallback_row = connection.execute(
                "SELECT COUNT(*) AS count FROM image_index WHERE description LIKE ?",
                (f"{FALLBACK_DESCRIPTION_PREFIX}%",),
            ).fetchone()

        total_records = int(total_row["count"]) if total_row is not None else 0
        fallback_records = int(fallback_row["count"]) if fallback_row is not None else 0
        fallback_ratio = (fallback_records / total_records) if total_records > 0 else 0.0
        summary["total_records"] = total_records
        summary["fallback_records"] = fallback_records
        summary["fallback_ratio"] = fallback_ratio
        summary["needs_reindex"] = total_records > 0 and fallback_ratio >= 0.25
        return summary

    def fetch_candidates(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        location_text: str | None = None,
    ) -> list[sqlite3.Row]:
        where_clauses: list[str] = []
        params: list[str] = []

        if date_from:
            where_clauses.append("taken_at IS NOT NULL AND taken_at >= ?")
            params.append(date_from)
        if date_to:
            where_clauses.append("taken_at IS NOT NULL AND taken_at <= ?")
            params.append(date_to)

        sql = """
            SELECT
                id,
                filename,
                relative_path,
                taken_at,
                place_name,
                country,
                description,
                tags_json,
                combined_text,
                text_embedding_model,
                combined_text_embedding,
                embedding_backend,
                embedding
            FROM image_index
        """
        base_where_sql = ""
        if where_clauses:
            base_where_sql = " WHERE " + " AND ".join(where_clauses)
        order_sql = " ORDER BY taken_at IS NULL, taken_at DESC, filename ASC"
        sql += base_where_sql + order_sql

        with self._connect() as connection:
            normalized_location = str(location_text or "").strip().lower()
            if normalized_location:
                location_clause = (
                    "("
                    "LOWER(COALESCE(place_name, '')) LIKE ? "
                    "OR LOWER(COALESCE(country, '')) LIKE ? "
                    "OR LOWER(COALESCE(combined_text, '')) LIKE ?"
                    ")"
                )
                location_sql = (
                    f"{base_where_sql} AND {location_clause}"
                    if base_where_sql
                    else f" WHERE {location_clause}"
                )
                location_pattern = f"%{normalized_location}%"
                filtered_rows = connection.execute(
                    sql.replace(base_where_sql + order_sql, "") + location_sql + order_sql,
                    [
                        *params,
                        location_pattern,
                        location_pattern,
                        location_pattern,
                    ],
                ).fetchall()
                if filtered_rows:
                    return filtered_rows
            return connection.execute(sql, params).fetchall()

    def delete_by_relative_path(self, relative_path: str, keep_id: str | None = None) -> None:
        sql = "DELETE FROM image_index WHERE relative_path = ?"
        params: tuple[str, ...] | tuple[str, str]

        if keep_id:
            sql += " AND id != ?"
            params = (relative_path, keep_id)
        else:
            params = (relative_path,)

        with self._connect() as connection:
            connection.execute(sql, params)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _column_names(connection: sqlite3.Connection, table_name: str) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}

    @staticmethod
    def _ensure_indexes(connection: sqlite3.Connection) -> None:
        connection.executescript(INDEXES_SQL)

    @staticmethod
    def _ensure_text_columns(connection: sqlite3.Connection, columns: set[str]) -> None:
        if "combined_text" not in columns:
            connection.execute(
                "ALTER TABLE image_index ADD COLUMN combined_text TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "UPDATE image_index SET combined_text = description WHERE combined_text = ''"
            )
        if "text_embedding_model" not in columns:
            connection.execute("ALTER TABLE image_index ADD COLUMN text_embedding_model TEXT")
        if "combined_text_embedding" not in columns:
            connection.execute("ALTER TABLE image_index ADD COLUMN combined_text_embedding BLOB")

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS image_index_new")
        connection.execute(CREATE_TABLE_SQL.replace("image_index", "image_index_new"))

        connection.execute(
            """
            INSERT INTO image_index_new (
                id,
                sha256,
                filename,
                relative_path,
                mime_type,
                file_size,
                width,
                height,
                taken_at,
                lat,
                lon,
                altitude,
                place_name,
                country,
                description,
                tags_json,
                combined_text,
                text_embedding_model,
                combined_text_embedding,
                embedding_backend,
                embedding,
                created_at,
                updated_at
            )
            SELECT
                id,
                sha256,
                filename,
                relative_path,
                mime_type,
                file_size,
                width,
                height,
                taken_at,
                lat,
                lon,
                altitude,
                place_name,
                country,
                description,
                tags_json,
                description,
                NULL,
                NULL,
                CASE
                    WHEN dino_embedding IS NOT NULL AND length(dino_embedding) > 0 THEN 'dino'
                    WHEN clip_embedding IS NOT NULL AND length(clip_embedding) > 0 THEN 'clip'
                    ELSE 'dino'
                END,
                CASE
                    WHEN dino_embedding IS NOT NULL AND length(dino_embedding) > 0 THEN dino_embedding
                    ELSE clip_embedding
                END,
                created_at,
                updated_at
            FROM image_index
            """
        )

        connection.execute("DROP TABLE image_index")
        connection.execute("ALTER TABLE image_index_new RENAME TO image_index")
        self._ensure_indexes(connection)
