"""Read-only access to persisted immutable timeline revisions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from memolens_contracts import (
    MemoLensError,
    encode_cursor,
    parse_json_object,
    safety_summary,
)
from memolens_sqlite import ReadOnlyDatabase
from memolens_timeline import validate_timeline


class TimelineIndexReader:
    def __init__(self, database: ReadOnlyDatabase) -> None:
        self.database = database

    def schema(
        self, connection: sqlite3.Connection
    ) -> tuple[str, str] | None:
        required = {
            "project_id",
            "revision",
            "schema_version",
            "timeline_json",
            "content_sha256",
            "provenance_json",
            "validation_status",
            "created_at",
        }
        for table, timeline_id_column in (
            ("timelines", "id"),
            ("timeline_revisions", "timeline_id"),
        ):
            columns = self.database.columns(connection, table)
            if required | {timeline_id_column} <= columns:
                return table, timeline_id_column
        return None

    @staticmethod
    def counts(
        connection: sqlite3.Connection,
        schema: tuple[str, str] | None,
    ) -> tuple[int, int]:
        if schema is None:
            return 0, 0
        table, timeline_id_column = schema
        revision_count = int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
        timeline_count = int(
            connection.execute(
                f"SELECT COUNT(DISTINCT {timeline_id_column}) FROM {table}"
            ).fetchone()[0]
        )
        return timeline_count, revision_count

    def _require_schema(
        self, connection: sqlite3.Connection
    ) -> tuple[str, str]:
        schema = self.schema(connection)
        if schema is None:
            raise MemoLensError(
                "The installed MemoLens index does not expose persisted timeline revisions.",
                code="capability_unavailable",
            )
        return schema

    def list(
        self, *, project_id: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            timeline_table, timeline_id_column = self._require_schema(connection)
            where: list[str] = []
            params: list[Any] = []
            if project_id is not None:
                where.append("t.project_id = ?")
                params.append(project_id)
            if cursor is not None:
                where.append(f"t.{timeline_id_column} > ?")
                params.append(cursor)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            try:
                rows = connection.execute(
                    f"SELECT t.{timeline_id_column} AS timeline_id, t.project_id, "
                    "t.revision, t.schema_version, t.timeline_json, t.content_sha256, "
                    "t.validation_status, t.created_at "
                    f"FROM {timeline_table} t JOIN ("
                    f"SELECT {timeline_id_column} AS timeline_id, "
                    "MAX(revision) AS latest_revision "
                    f"FROM {timeline_table} GROUP BY {timeline_id_column}"
                    f") latest ON latest.timeline_id = t.{timeline_id_column} "
                    "AND latest.latest_revision = t.revision "
                    f"{where_sql} ORDER BY t.{timeline_id_column} ASC LIMIT ?",
                    [*params, limit + 1],
                ).fetchall()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "Persisted timelines could not be listed.",
                    code="database_unavailable",
                ) from exc
        has_more = len(rows) > limit
        selected = rows[:limit]
        timelines = []
        for row in selected:
            raw = dict(row)
            timeline = parse_json_object(raw.pop("timeline_json", None))
            raw["format"] = timeline.get("format") if timeline else None
            raw["track_count"] = len(timeline.get("tracks", [])) if timeline else 0
            timelines.append(raw)
        return {
            "object": "memolens.timeline_list",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "project_id": project_id,
            "result_count": len(timelines),
            "timelines": timelines,
            "next_cursor": (
                encode_cursor(str(selected[-1]["timeline_id"]))
                if has_more and selected
                else None
            ),
            "safety": safety_summary(),
        }

    def get(self, timeline_id: str, revision: int | None) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            timeline_table, timeline_id_column = self._require_schema(connection)
            query = (
                f"SELECT {timeline_id_column} AS timeline_id, project_id, revision, "
                "schema_version, timeline_json, content_sha256, provenance_json, "
                f"validation_status, created_at FROM {timeline_table} "
                f"WHERE {timeline_id_column} = ?"
            )
            params: list[Any] = [timeline_id]
            if revision is not None:
                query += " AND revision = ?"
                params.append(revision)
            query += " ORDER BY revision DESC LIMIT 1"
            try:
                row = connection.execute(query, params).fetchone()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The persisted timeline could not be read.",
                    code="database_unavailable",
                ) from exc
        if row is None:
            raise MemoLensError(
                "Timeline revision was not found.", code="timeline_not_found"
            )
        raw = dict(row)
        timeline = parse_json_object(raw.pop("timeline_json", None))
        if not timeline:
            raise MemoLensError(
                "The persisted timeline JSON is invalid.",
                code="invalid_persisted_timeline",
            )
        stored_provenance = parse_json_object(raw.pop("provenance_json", None))
        return {
            "object": "memolens.timeline_detail",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            **raw,
            "stored_provenance": stored_provenance,
            "timeline": timeline,
            "validation": validate_timeline(timeline),
            "safety": safety_summary(),
        }
