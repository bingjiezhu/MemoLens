"""Mixed-media inventory and current-successful video analysis reads."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from memolens_contracts import (
    MemoLensError,
    compact_media_asset,
    encode_cursor,
    parse_json_object,
    query_terms,
    safety_summary,
)
from memolens_ranking import RankedRow, keep_best, lexical_score
from memolens_sqlite import ReadOnlyDatabase


class MediaIndexReader:
    """Read media sources and only the authoritative video-analysis head."""

    def __init__(self, database: ReadOnlyDatabase) -> None:
        self.database = database

    @staticmethod
    def validate_identity(columns: set[str]) -> None:
        if columns and not {"id", "kind", "sha256"}.issubset(columns):
            raise MemoLensError(
                "The SQLite assets table is missing required MemoLens columns.",
                code="database_identity_mismatch",
            )

    @staticmethod
    def kind_counts(
        connection: sqlite3.Connection, asset_columns: set[str]
    ) -> dict[str, int]:
        if not asset_columns:
            return {}
        rows = connection.execute(
            "SELECT kind, COUNT(*) FROM assets GROUP BY kind ORDER BY kind"
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    @staticmethod
    def schema_available(
        asset_columns: set[str], source_columns: set[str]
    ) -> bool:
        return bool(
            {"id", "kind", "sha256"}.issubset(asset_columns)
            and {"id", "asset_id", "relative_path"}.issubset(source_columns)
            and {"display_filename", "filename"} & source_columns
            and {"availability", "status"} & source_columns
        )

    def video_status(
        self,
        connection: sqlite3.Connection,
        *,
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
    ) -> tuple[str | None, bool, int]:
        mode = self._video_schema_mode(connection, asset_columns, segment_columns)
        available = bool(mode and source_columns)
        if not available:
            return mode, False, 0
        video_sql, _ = self._video_rows_sql(
            connection, asset_columns, source_columns, segment_columns
        )
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM ({video_sql}) current_segments"
            ).fetchone()[0]
        )
        return mode, True, count

    def _require_schema(
        self, connection: sqlite3.Connection
    ) -> tuple[set[str], set[str], set[str]]:
        asset_columns = self.database.columns(connection, "assets")
        source_columns = self.database.columns(connection, "asset_sources")
        segment_columns = self.database.columns(connection, "video_segments")
        if not {"id", "kind", "sha256"}.issubset(asset_columns):
            raise MemoLensError(
                "The installed MemoLens index does not yet expose the mixed-media schema.",
                code="capability_unavailable",
            )
        required_source = {"id", "asset_id", "relative_path"}
        has_filename = bool({"display_filename", "filename"} & source_columns)
        has_availability = bool({"availability", "status"} & source_columns)
        if (
            not required_source.issubset(source_columns)
            or not has_filename
            or not has_availability
        ):
            raise MemoLensError(
                "The installed MemoLens index does not expose safe media sources.",
                code="capability_unavailable",
            )
        return asset_columns, source_columns, segment_columns

    @staticmethod
    def _media_select_parts(
        asset_columns: set[str], source_columns: set[str]
    ) -> list[str]:
        desired = [
            "id",
            "kind",
            "sha256",
            "mime_type",
            "file_size",
            "duration_ms",
            "width",
            "height",
            "rotation_degrees",
            "codec_json",
            "captured_at",
            "error_code",
        ]
        parts = [
            f'a."{name}" AS "{name}"'
            if name in asset_columns
            else f'NULL AS "{name}"'
            for name in desired
        ]
        status_column = "probe_status" if "probe_status" in asset_columns else "status"
        filename_column = (
            "display_filename" if "display_filename" in source_columns else "filename"
        )
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        parts.extend(
            [
                (
                    f'a."{status_column}" AS "probe_status"'
                    if status_column in asset_columns
                    else 'NULL AS "probe_status"'
                ),
                'src."id" AS "asset_source_id"',
                f'src."{filename_column}" AS "filename"',
                'src."relative_path" AS "relative_path"',
                f'src."{availability_column}" AS "source_availability"',
            ]
        )
        return parts

    @staticmethod
    def _source_join(
        source_columns: set[str], *, available_only: bool = False
    ) -> str:
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        available_filter = (
            f" AND src2.{availability_column} = 'available'" if available_only else ""
        )
        preferred_order = (
            "CASE WHEN src2.is_preferred = 1 THEN 0 ELSE 1 END, "
            if "is_preferred" in source_columns
            else ""
        )
        return (
            "LEFT JOIN asset_sources src ON src.id = ("
            "SELECT src2.id FROM asset_sources src2 WHERE src2.asset_id = a.id "
            f"{available_filter} ORDER BY "
            f"CASE WHEN src2.{availability_column} = 'available' THEN 0 ELSE 1 END, "
            f"{preferred_order}src2.id LIMIT 1)"
        )

    def _video_schema_mode(
        self,
        connection: sqlite3.Connection,
        asset_columns: set[str],
        segment_columns: set[str],
    ) -> str | None:
        common_segments = {
            "id",
            "asset_id",
            "start_ms",
            "end_ms",
            "combined_text",
        }
        if not common_segments.issubset(segment_columns):
            return None
        run_columns = self.database.columns(connection, "analysis_runs")
        final_runs = {"id", "asset_id", "revision", "status"}.issubset(run_columns)
        if "analysis_run_id" in segment_columns and final_runs:
            view_columns = self.database.columns(connection, "current_video_segments")
            if {
                *common_segments,
                "analysis_run_id",
                "analysis_revision",
            }.issubset(view_columns):
                return "analysis_heads_view"
            head_columns = self.database.columns(connection, "asset_analysis_heads")
            if {"asset_id", "analysis_run_id"}.issubset(head_columns):
                return "analysis_heads_join"
        if (
            "analysis_revision" in segment_columns
            and "current_analysis_revision" in asset_columns
        ):
            return "explicit_revision_head_compat"
        return None

    def _video_rows_sql(
        self,
        connection: sqlite3.Connection,
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
        *,
        asset_id_filter: bool = False,
    ) -> tuple[str, str]:
        mode = self._video_schema_mode(connection, asset_columns, segment_columns)
        if mode is None:
            raise MemoLensError(
                "No safe current-successful video analysis selector is available.",
                code="video_index_unavailable",
            )
        relation_columns = (
            self.database.columns(connection, "current_video_segments")
            if mode == "analysis_heads_view"
            else segment_columns
        )
        select_parts = self._video_select_parts(
            relation_columns=relation_columns,
            asset_columns=asset_columns,
            source_columns=source_columns,
            analysis_heads=mode.startswith("analysis_heads"),
        )
        from_sql = self._video_from_sql(mode, source_columns)
        if self._active_library_roots_available(connection, source_columns):
            from_sql += (
                " JOIN library_roots root ON root.id = src.library_root_id "
                "AND root.status = 'active'"
            )
        where = ["a.kind = 'video'", "src.id IS NOT NULL"]
        if mode == "explicit_revision_head_compat":
            where.append("s.analysis_revision = a.current_analysis_revision")
        if asset_id_filter:
            where.append("a.id = ?")
        return (
            f"SELECT {', '.join(select_parts)} {from_sql} "
            f"WHERE {' AND '.join(where)}",
            mode,
        )

    @staticmethod
    def _video_select_parts(
        *,
        relation_columns: set[str],
        asset_columns: set[str],
        source_columns: set[str],
        analysis_heads: bool,
    ) -> list[str]:
        desired_segments = [
            "id",
            "asset_id",
            "ordinal",
            "start_ms",
            "end_ms",
            "boundary_reason",
            "summary",
            "visible_text",
            "combined_text",
            "semantic_json",
            "visual_status",
            "transcript_status",
            "confidence",
        ]
        parts = [
            f's."{name}" AS "{name}"'
            if name in relation_columns
            else f'NULL AS "{name}"'
            for name in desired_segments
        ]
        if analysis_heads:
            parts.extend(
                [
                    's."analysis_run_id" AS "analysis_run_id"',
                    'ar."revision" AS "analysis_revision"',
                ]
            )
        else:
            parts.extend(
                [
                    'NULL AS "analysis_run_id"',
                    's."analysis_revision" AS "analysis_revision"',
                ]
            )
        for name in ("sha256", "duration_ms", "width", "height", "rotation_degrees"):
            parts.append(
                f'a."{name}" AS "asset_{name}"'
                if name in asset_columns
                else f'NULL AS "asset_{name}"'
            )
        filename_column = (
            "display_filename" if "display_filename" in source_columns else "filename"
        )
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        parts.extend(
            [
                'src."id" AS "asset_source_id"',
                f'src."{filename_column}" AS "filename"',
                'src."relative_path" AS "relative_path"',
                f'src."{availability_column}" AS "source_availability"',
            ]
        )
        return parts

    def _video_from_sql(self, mode: str, source_columns: set[str]) -> str:
        if mode == "analysis_heads_view":
            from_sql = (
                "FROM current_video_segments s "
                "JOIN assets a ON a.id = s.asset_id "
                "JOIN analysis_runs ar ON ar.id = s.analysis_run_id "
                "AND ar.asset_id = s.asset_id AND ar.status = 'succeeded' "
            )
        elif mode == "analysis_heads_join":
            from_sql = (
                "FROM video_segments s "
                "JOIN asset_analysis_heads ah ON ah.asset_id = s.asset_id "
                "AND ah.analysis_run_id = s.analysis_run_id "
                "JOIN analysis_runs ar ON ar.id = s.analysis_run_id "
                "AND ar.asset_id = s.asset_id AND ar.status = 'succeeded' "
                "JOIN assets a ON a.id = s.asset_id "
            )
        else:
            from_sql = "FROM video_segments s JOIN assets a ON a.id = s.asset_id "
        return from_sql + self._source_join(source_columns, available_only=True)

    def _active_library_roots_available(
        self, connection: sqlite3.Connection, source_columns: set[str]
    ) -> bool:
        return bool(
            "library_root_id" in source_columns
            and self.database.relation_exists(connection, "library_roots")
            and {"id", "status"}.issubset(
                self.database.columns(connection, "library_roots")
            )
        )

    def list(
        self, *, kinds: list[str], limit: int, cursor: str | None
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            asset_columns, source_columns, _ = self._require_schema(connection)
            placeholders = ", ".join("?" for _kind in kinds)
            where = [f"a.kind IN ({placeholders})"]
            params: list[Any] = list(kinds)
            if cursor is not None:
                where.append("a.id > ?")
                params.append(cursor)
            params.append(limit + 1)
            try:
                rows = connection.execute(
                    f"SELECT {', '.join(self._media_select_parts(asset_columns, source_columns))} "
                    f"FROM assets a {self._source_join(source_columns)} "
                    f"WHERE {' AND '.join(where)} ORDER BY a.id ASC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens mixed-media index could not be listed.",
                    code="database_unavailable",
                ) from exc
        has_more = len(rows) > limit
        selected = rows[:limit]
        assets = [compact_media_asset(dict(row)) for row in selected]
        return {
            "object": "memolens.media_list",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "kinds": kinds,
            "result_count": len(assets),
            "assets": assets,
            "next_cursor": (
                encode_cursor(str(selected[-1]["id"]))
                if has_more and selected
                else None
            ),
            "safety": safety_summary(),
        }

    def get(self, asset_id: str) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            asset_columns, source_columns, segment_columns = self._require_schema(
                connection
            )
            try:
                row = connection.execute(
                    f"SELECT {', '.join(self._media_select_parts(asset_columns, source_columns))} "
                    f"FROM assets a {self._source_join(source_columns)} WHERE a.id = ?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    raise MemoLensError(
                        "Media asset was not found.", code="media_not_found"
                    )
                raw = dict(row)
                segments, video_index_status = self._asset_video_segments(
                    connection,
                    raw=raw,
                    asset_columns=asset_columns,
                    source_columns=source_columns,
                    segment_columns=segment_columns,
                )
            except MemoLensError:
                raise
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens media asset could not be read.",
                    code="database_unavailable",
                ) from exc
        return {
            "object": "memolens.media_detail",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "asset": compact_media_asset(raw),
            "segments": segments,
            "video_index_status": video_index_status,
            "safety": safety_summary(),
        }

    def _asset_video_segments(
        self,
        connection: sqlite3.Connection,
        *,
        raw: dict[str, Any],
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
    ) -> tuple[list[dict[str, Any]], str | None]:
        if raw.get("kind") != "video":
            return [], None
        try:
            video_sql, video_schema_mode = self._video_rows_sql(
                connection,
                asset_columns,
                source_columns,
                segment_columns,
                asset_id_filter=True,
            )
        except MemoLensError as exc:
            if exc.code != "video_index_unavailable":
                raise
            return [], "video_index_unavailable"
        rows = connection.execute(
            f"{video_sql} ORDER BY s.start_ms ASC, s.id ASC LIMIT 500",
            (raw["id"],),
        ).fetchall()
        return [self._compact_segment(dict(item)) for item in rows], video_schema_mode

    def video_search(self, query: str, limit: int) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            asset_columns, source_columns, segment_columns = self._require_schema(
                connection
            )
            video_sql, video_schema_mode = self._video_rows_sql(
                connection, asset_columns, source_columns, segment_columns
            )
            try:
                scored, scanned_count = self._rank_video_rows(
                    connection.execute(video_sql), query=query, limit=limit
                )
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens video-segment index could not be searched.",
                    code="database_unavailable",
                ) from exc
        scored.sort(key=lambda item: item[:3], reverse=True)
        results = [self._compact_match(raw) for *_, raw in scored]
        return {
            "object": "memolens.video_search",
            "schema_version": "1",
            "status": "completed",
            "ranking": "deterministic_lexical",
            "query": query,
            "result_count": len(results),
            "scanned_count": scanned_count,
            "results": results,
            "video_schema_mode": video_schema_mode,
            "safety": safety_summary(),
        }

    def _rank_video_rows(
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
                raw = dict(row)
                score, matched = self._score_video(raw, query, phrase, terms)
                if score <= 0:
                    continue
                raw["matched_terms"] = list(dict.fromkeys(matched))
                raw["raw_score"] = score
                candidate = (score, str(raw.get("id") or ""), sequence, raw)
                sequence += 1
                keep_best(scored, candidate, limit)
        return scored, scanned_count

    @staticmethod
    def _score_video(
        raw: dict[str, Any], query: str, phrase: str, terms: list[str]
    ) -> tuple[float, list[str]]:
        fields = {
            "summary": str(raw.get("summary") or "").casefold(),
            "visible_text": str(raw.get("visible_text") or "").casefold(),
            "combined": str(raw.get("combined_text") or "").casefold(),
            "semantic": str(raw.get("semantic_json") or "").casefold(),
            "filename": str(raw.get("filename") or "").casefold(),
        }
        return lexical_score(
            fields,
            query=query,
            phrase=phrase,
            terms=terms,
            weights={
                "summary": 3.0,
                "visible_text": 2.5,
                "semantic": 2.0,
                "filename": 1.5,
                "combined": 1.0,
            },
        )

    @staticmethod
    def _compact_segment(raw: dict[str, Any]) -> dict[str, Any]:
        semantic = parse_json_object(raw.get("semantic_json"))
        return {
            key: raw.get(key)
            for key in (
                "id",
                "asset_id",
                "asset_source_id",
                "ordinal",
                "start_ms",
                "end_ms",
                "analysis_run_id",
                "analysis_revision",
                "boundary_reason",
                "summary",
                "visible_text",
                "visual_status",
                "transcript_status",
                "confidence",
            )
            if raw.get(key) is not None
        } | {"semantic": semantic or None}

    @staticmethod
    def _compact_match(raw: dict[str, Any]) -> dict[str, Any]:
        score = float(raw.get("raw_score") or 0.0)
        return {
            "object": "creative_asset_match",
            "schema_version": "1",
            "result_type": "video_segment",
            "id": raw.get("id"),
            "asset_id": raw.get("asset_id"),
            "asset_source_id": raw.get("asset_source_id"),
            "asset_sha256": raw.get("asset_sha256"),
            "segment_id": raw.get("id"),
            "start_ms": raw.get("start_ms"),
            "end_ms": raw.get("end_ms"),
            "asset_duration_ms": raw.get("asset_duration_ms"),
            "filename": raw.get("filename"),
            "relative_path": raw.get("relative_path"),
            "source_availability": raw.get("source_availability"),
            "summary": raw.get("summary"),
            "visible_text": raw.get("visible_text"),
            "matched_terms": raw.get("matched_terms", []),
            "score": round(score / (score + 5.0), 6),
            "confidence": raw.get("confidence"),
            "analysis_run_id": raw.get("analysis_run_id"),
            "analysis_revision": raw.get("analysis_revision"),
            "semantic": parse_json_object(raw.get("semantic_json")) or None,
            "provenance": [
                source
                for source, available in (
                    ("visual", bool(raw.get("summary") or raw.get("visible_text"))),
                    ("transcript", raw.get("transcript_status") == "complete"),
                )
                if available
            ],
        }
