"""Mixed-media inventory and current-successful video analysis reads."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any, Callable

from memolens_contracts import (
    MemoLensError,
    compact_media_asset,
    encode_cursor,
    parse_json_object,
    query_terms,
    safety_summary,
)
from memolens_image_authority import CanonicalImageObservationReader
from memolens_ranking import RankedRow, keep_best, lexical_score
from memolens_residual_authority import (
    ResidualAuthorityError,
    source_binding_sha256,
)
from memolens_sqlite import ReadOnlyDatabase


class MediaIndexReader:
    """Read media sources and only the authoritative video-analysis head."""

    def __init__(self, database: ReadOnlyDatabase) -> None:
        self.database = database
        self.image_authority = CanonicalImageObservationReader()

    @staticmethod
    def validate_identity(columns: set[str]) -> None:
        if columns and not {"id", "kind", "sha256"}.issubset(columns):
            raise MemoLensError(
                "The SQLite assets table is missing required MemoLens columns.",
                code="database_identity_mismatch",
            )

    def validate_no_persisted_residual_rows(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Reject reserved residual identities at the persisted read boundary."""

        if not self.database.relation_exists(connection, "video_segments"):
            return
        try:
            forged = connection.execute(
                "SELECT id FROM video_segments "
                "WHERE substr(id,1,5)='rseg_' LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The persisted video-segment identity census could not be read.",
                code="database_unavailable",
            ) from exc
        if forged is not None:
            raise MemoLensError(
                "A residual projection identity was found in persisted video segments.",
                code="residual_authority_integrity_error",
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
    ) -> dict[str, Any]:
        mode = self._video_schema_mode(connection, asset_columns, segment_columns)
        available = bool(mode and source_columns)
        result = {
            "mode": mode,
            "search_available": available,
            "default_segment_count": 0,
            "default_asset_count": 0,
            "readable_segment_count": 0,
            "readable_asset_count": 0,
            "successful_head_asset_count": 0,
        }
        if not available:
            return result
        default_sql, _ = self._video_rows_sql(
            connection, asset_columns, source_columns, segment_columns
        )
        readable_sql, _ = self._video_rows_sql(
            connection,
            asset_columns,
            source_columns,
            segment_columns,
            exclude_archived=False,
            require_available_source=False,
        )
        default_row = connection.execute(
            f"SELECT COUNT(*) AS segment_count,"
            "COUNT(DISTINCT asset_id) AS asset_count "
            f"FROM ({default_sql}) current_segments"
        ).fetchone()
        readable_row = connection.execute(
            f"SELECT COUNT(*) AS segment_count,"
            "COUNT(DISTINCT asset_id) AS asset_count "
            f"FROM ({readable_sql}) current_segments"
        ).fetchone()
        successful_head_asset_count = 0
        if mode and mode.startswith("analysis_heads"):
            successful_head_asset_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT ah.asset_id) "
                    "FROM asset_analysis_heads ah "
                    "JOIN analysis_runs ar ON ar.id = ah.analysis_run_id "
                    "AND ar.asset_id = ah.asset_id AND ar.status = 'succeeded' "
                    "JOIN assets a ON a.id = ah.asset_id AND a.kind = 'video'"
                ).fetchone()[0]
            )
        result.update(
            {
                "default_segment_count": int(default_row["segment_count"]),
                "default_asset_count": int(default_row["asset_count"]),
                "readable_segment_count": int(readable_row["segment_count"]),
                "readable_asset_count": int(readable_row["asset_count"]),
                "successful_head_asset_count": successful_head_asset_count,
            }
        )
        return result

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

    def evidence_available_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        identifier: str,
    ) -> bool:
        """Resolve current asset/span availability inside a caller-owned snapshot."""

        try:
            asset_columns, source_columns, segment_columns = self._require_schema(
                connection
            )
            if kind == "asset":
                availability = (
                    "availability" if "availability" in source_columns else "status"
                )
                active_root = ""
                if self._active_library_roots_available(connection, source_columns):
                    active_root = (
                        " AND EXISTS (SELECT 1 FROM library_roots root "
                        "WHERE root.id=src.library_root_id AND root.status='active')"
                    )
                row = connection.execute(
                    "SELECT 1 FROM assets a JOIN asset_sources src ON src.asset_id=a.id "
                    f"WHERE a.id=? AND src.{availability}='available'{active_root} LIMIT 1",
                    (identifier,),
                ).fetchone()
                return row is not None
            if kind != "span":
                return False
            mode = self._video_schema_mode(
                connection, asset_columns, segment_columns
            )
            if mode is None or not mode.startswith("analysis_heads"):
                return False
            sql, _mode = self._video_rows_sql(
                connection,
                asset_columns,
                source_columns,
                segment_columns,
                segment_id_filter=True,
                exclude_archived=False,
                require_available_source=True,
                require_active_root=True,
                require_source_binding=True,
            )
            return (
                connection.execute(sql + " LIMIT 1", (identifier,)).fetchone()
                is not None
            )
        except MemoLensError as exc:
            if exc.code == "capability_unavailable":
                return False
            raise
        except sqlite3.Error as exc:
            raise MemoLensError(
                "Evidence availability could not be read from the MemoLens index.",
                code="database_unavailable",
            ) from exc

    def evidence_proof_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        identifier: str,
    ) -> dict[str, str] | None:
        """Resolve the current evidence identity and its exact asset digest.

        This is deliberately separate from the public availability predicate:
        callers that admit executable material must compare the database-owned
        ``asset_sha256`` proof with the cold-audited Export derivative set.
        Reference-only consumers can continue to use the weaker boolean read.
        """

        try:
            asset_columns, source_columns, segment_columns = self._require_schema(
                connection
            )
            if kind == "asset":
                availability = (
                    "availability" if "availability" in source_columns else "status"
                )
                active_root = ""
                if self._active_library_roots_available(connection, source_columns):
                    active_root = (
                        " AND EXISTS (SELECT 1 FROM library_roots root "
                        "WHERE root.id=src.library_root_id AND root.status='active')"
                    )
                row = connection.execute(
                    "SELECT a.id AS asset_id,a.sha256 AS asset_sha256 "
                    "FROM assets a JOIN asset_sources src ON src.asset_id=a.id "
                    f"WHERE a.id=? AND src.{availability}='available'{active_root} "
                    "ORDER BY src.id LIMIT 1",
                    (identifier,),
                ).fetchone()
                if row is None:
                    return None
                proof = {
                    "kind": "asset",
                    "identifier": identifier,
                    "asset_id": str(row["asset_id"]),
                    "asset_sha256": str(row["asset_sha256"]),
                }
            elif kind == "span":
                mode = self._video_schema_mode(
                    connection, asset_columns, segment_columns
                )
                if mode is None or not mode.startswith("analysis_heads"):
                    return None
                sql, _mode = self._video_rows_sql(
                    connection,
                    asset_columns,
                    source_columns,
                    segment_columns,
                    segment_id_filter=True,
                    exclude_archived=False,
                    require_available_source=True,
                    require_active_root=True,
                    require_source_binding=True,
                )
                row = connection.execute(sql + " LIMIT 1", (identifier,)).fetchone()
                if row is None:
                    return None
                proof = {
                    "kind": "span",
                    "identifier": identifier,
                    "asset_id": str(row["asset_id"]),
                    "asset_sha256": str(row["asset_sha256"]),
                }
            else:
                return None
            if (
                proof["identifier"] != identifier
                or not proof["asset_id"]
                or len(proof["asset_sha256"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in proof["asset_sha256"]
                )
            ):
                raise MemoLensError(
                    "The current evidence identity proof is invalid.",
                    code="evidence_identity_integrity_error",
                )
            return proof
        except MemoLensError:
            raise
        except sqlite3.Error as exc:
            raise MemoLensError(
                "Evidence identity proof could not be read from the MemoLens index.",
                code="database_unavailable",
            ) from exc

    def _media_select_parts(
        self,
        connection: sqlite3.Connection,
        asset_columns: set[str],
        source_columns: set[str],
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
        review_parts, _review_join, _archived_filter = self._review_projection(
            connection
        )
        parts.extend(review_parts)
        return parts

    def _review_projection(
        self, connection: sqlite3.Connection
    ) -> tuple[list[str], str, str]:
        review_columns = self.database.columns(
            connection, "asset_review_revisions"
        )
        if not {
            "asset_id",
            "revision",
            "inbox_state",
            "favorite",
            "project_ready",
        }.issubset(review_columns):
            return (
                [
                    "0 AS review_revision",
                    "'inbox' AS inbox_state",
                    "0 AS favorite",
                    "0 AS project_ready",
                ],
                "",
                "",
            )
        return (
            [
                "COALESCE(rv.revision,0) AS review_revision",
                "COALESCE(rv.inbox_state,'inbox') AS inbox_state",
                "COALESCE(rv.favorite,0) AS favorite",
                "COALESCE(rv.project_ready,0) AS project_ready",
            ],
            (
                " LEFT JOIN asset_review_revisions rv ON rv.asset_id=a.id "
                "AND rv.revision=(SELECT MAX(rv2.revision) "
                "FROM asset_review_revisions rv2 WHERE rv2.asset_id=a.id)"
            ),
            "COALESCE(rv.inbox_state,'inbox')<>'archived'",
        )

    @staticmethod
    def _source_join(
        source_columns: set[str],
        *,
        available_only: bool = False,
        active_root_only: bool = False,
        prefer_active_root: bool = False,
    ) -> str:
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        available_filter = (
            f" AND src2.{availability_column} = 'available'" if available_only else ""
        )
        active_root_filter = (
            " AND EXISTS (SELECT 1 FROM library_roots root2 "
            "WHERE root2.id = src2.library_root_id AND root2.status = 'active')"
            if active_root_only
            else ""
        )
        preferred_order = (
            "CASE WHEN src2.is_preferred = 1 THEN 0 ELSE 1 END, "
            if "is_preferred" in source_columns
            else ""
        )
        active_root_order = (
            "CASE WHEN EXISTS (SELECT 1 FROM library_roots root3 "
            "WHERE root3.id = src2.library_root_id AND root3.status = 'active') "
            "THEN 0 ELSE 1 END, "
            if prefer_active_root
            else ""
        )
        return (
            "LEFT JOIN asset_sources src ON src.id = ("
            "SELECT src2.id FROM asset_sources src2 WHERE src2.asset_id = a.id "
            f"{available_filter}{active_root_filter} ORDER BY "
            f"{active_root_order}"
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
            head_columns = self.database.columns(connection, "asset_analysis_heads")
            has_explicit_head = {"asset_id", "analysis_run_id"}.issubset(
                head_columns
            )
            view_columns = self.database.columns(connection, "current_video_segments")
            if has_explicit_head and {
                *common_segments,
                "analysis_run_id",
                "analysis_revision",
            }.issubset(view_columns):
                return "analysis_heads_view"
            if has_explicit_head:
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
        segment_id_filter: bool = False,
        exclude_archived: bool = True,
        require_available_source: bool = True,
        require_active_root: bool = True,
        require_source_binding: bool = True,
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
        root_status_available = self._active_library_roots_available(
            connection, source_columns
        )
        select_parts = self._video_select_parts(
            relation_columns=relation_columns,
            asset_columns=asset_columns,
            source_columns=source_columns,
            run_columns=self.database.columns(connection, "analysis_runs"),
            analysis_heads=mode.startswith("analysis_heads"),
            root_status_available=root_status_available,
        )
        from_sql = self._video_from_sql(
            mode,
            source_columns,
            require_available_source=require_available_source,
            active_root_only=require_active_root and root_status_available,
            prefer_active_root=root_status_available,
        )
        review_parts, review_join, archived_filter = self._review_projection(
            connection
        )
        select_parts.extend(review_parts)
        from_sql += review_join
        if require_active_root and root_status_available:
            from_sql += (
                " JOIN library_roots root ON root.id = src.library_root_id "
                "AND root.status = 'active'"
            )
        where = ["a.kind = 'video'"]
        if require_source_binding:
            where.append("src.id IS NOT NULL")
        if exclude_archived and archived_filter:
            where.append(archived_filter)
        if mode == "explicit_revision_head_compat":
            where.append("s.analysis_revision = a.current_analysis_revision")
        if asset_id_filter:
            where.append("a.id = ?")
        if segment_id_filter:
            where.append("s.id = ?")
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
        run_columns: set[str],
        analysis_heads: bool,
        root_status_available: bool,
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
                    (
                        'ar."input_asset_sha256" AS "input_asset_sha256"'
                        if "input_asset_sha256" in run_columns
                        else 'NULL AS "input_asset_sha256"'
                    ),
                ]
            )
        else:
            parts.extend(
                [
                    'NULL AS "analysis_run_id"',
                    's."analysis_revision" AS "analysis_revision"',
                    'NULL AS "input_asset_sha256"',
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
                (
                    'src."library_root_id" AS "source_library_root_id"'
                    if "library_root_id" in source_columns
                    else 'NULL AS "source_library_root_id"'
                ),
                (
                    'src."observed_size" AS "source_observed_size"'
                    if "observed_size" in source_columns
                    else 'NULL AS "source_observed_size"'
                ),
                (
                    'src."observed_mtime_ns" AS "source_observed_mtime_ns"'
                    if "observed_mtime_ns" in source_columns
                    else 'NULL AS "source_observed_mtime_ns"'
                ),
                (
                    'src."source_file_id" AS "source_file_id"'
                    if "source_file_id" in source_columns
                    else 'NULL AS "source_file_id"'
                ),
                (
                    "(SELECT source_root.status FROM library_roots source_root "
                    "WHERE source_root.id = src.library_root_id) AS library_root_status"
                    if root_status_available
                    else 'NULL AS "library_root_status"'
                ),
            ]
        )
        return parts

    def _video_from_sql(
        self,
        mode: str,
        source_columns: set[str],
        *,
        require_available_source: bool = True,
        active_root_only: bool = False,
        prefer_active_root: bool = False,
    ) -> str:
        if mode == "analysis_heads_view":
            from_sql = (
                "FROM current_video_segments s "
                "JOIN assets a ON a.id = s.asset_id "
                "JOIN asset_analysis_heads ah ON ah.asset_id = s.asset_id "
                "AND ah.analysis_run_id = s.analysis_run_id "
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
        return from_sql + self._source_join(
            source_columns,
            available_only=require_available_source,
            active_root_only=active_root_only,
            prefer_active_root=prefer_active_root,
        )

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
        self,
        *,
        kinds: list[str],
        limit: int,
        cursor: str | None,
        active_root_only: bool = False,
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            asset_columns, source_columns, _ = self._require_schema(connection)
            placeholders = ", ".join("?" for _kind in kinds)
            where = [f"a.kind IN ({placeholders})"]
            _review_parts, review_join, archived_filter = self._review_projection(
                connection
            )
            if archived_filter:
                where.append(archived_filter)
            params: list[Any] = list(kinds)
            if cursor is not None:
                where.append("a.id > ?")
                params.append(cursor)
            params.append(limit + 1)
            try:
                rows = connection.execute(
                    f"SELECT {', '.join(self._media_select_parts(connection, asset_columns, source_columns))} "
                    f"FROM assets a {self._source_join(source_columns, active_root_only=active_root_only and self._active_library_roots_available(connection, source_columns))}"
                    f"{review_join} "
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

    def get(
        self,
        asset_id: str,
        *,
        include_image_observation: bool = False,
        require_verified_video_head: bool = False,
        active_root_only: bool = False,
        include_unavailable_video_sources: bool = False,
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            asset_columns, source_columns, segment_columns = self._require_schema(
                connection
            )
            try:
                row = connection.execute(
                    f"SELECT {', '.join(self._media_select_parts(connection, asset_columns, source_columns))} "
                    f"FROM assets a {self._source_join(source_columns, active_root_only=active_root_only and self._active_library_roots_available(connection, source_columns))}"
                    f"{self._review_projection(connection)[1]} WHERE a.id = ?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    raise MemoLensError(
                        "Media asset was not found.", code="media_not_found"
                    )
                raw = dict(row)
                image_observation = (
                    self._image_observation(connection, raw=raw)
                    if include_image_observation
                    else None
                )
                segments, video_index_status = self._asset_video_segments(
                    connection,
                    raw=raw,
                    asset_columns=asset_columns,
                    source_columns=source_columns,
                    segment_columns=segment_columns,
                    require_verified_head=require_verified_video_head,
                    require_available_source=not include_unavailable_video_sources,
                    require_active_root=not include_unavailable_video_sources,
                    require_source_binding=not include_unavailable_video_sources,
                )
            except MemoLensError:
                raise
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens media asset could not be read.",
                    code="database_unavailable",
                ) from exc
        result = {
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
        if include_image_observation:
            result["image_observation"] = image_observation
        return result

    def _image_observation(
        self,
        connection: sqlite3.Connection,
        *,
        raw: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return only an exact-current canonical observation or typed gap state."""

        return self.image_authority.read(connection, raw=raw)

    def _asset_video_segments(
        self,
        connection: sqlite3.Connection,
        *,
        raw: dict[str, Any],
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
        require_verified_head: bool = False,
        require_available_source: bool = True,
        require_active_root: bool = True,
        require_source_binding: bool = True,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if raw.get("kind") != "video":
            return [], None
        self.validate_no_persisted_residual_rows(connection)
        try:
            video_sql, video_schema_mode = self._video_rows_sql(
                connection,
                asset_columns,
                source_columns,
                segment_columns,
                asset_id_filter=True,
                exclude_archived=False,
                require_available_source=require_available_source,
                require_active_root=require_active_root,
                require_source_binding=require_source_binding,
            )
            if require_verified_head and not video_schema_mode.startswith(
                "analysis_heads"
            ):
                return [], "verified_successful_head_unavailable"
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
            return self.video_search_in_connection(connection, query, limit)

    def video_search_in_connection(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
        *,
        excluded_asset_sha256: frozenset[str] = frozenset(),
        candidate_predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Search one caller-owned snapshot, filtering derivatives pre-rank."""

        asset_columns, source_columns, segment_columns = self._require_schema(
            connection
        )
        self.validate_no_persisted_residual_rows(connection)
        video_sql, video_schema_mode = self._video_rows_sql(
            connection, asset_columns, source_columns, segment_columns
        )
        try:
            scored, scanned_count, excluded_derivative_count = self._rank_video_rows(
                connection.execute(video_sql),
                query=query,
                limit=limit,
                excluded_asset_sha256=excluded_asset_sha256,
                candidate_predicate=candidate_predicate,
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
            "excluded_derivative_count": excluded_derivative_count,
            "results": results,
            "video_schema_mode": video_schema_mode,
            "safety": safety_summary(),
        }

    def segment_get(self, segment_id: str) -> dict[str, Any] | None:
        """Read one segment only when it belongs to the explicit successful head."""

        with closing(self.database.connection()) as connection:
            asset_columns, source_columns, segment_columns = self._require_schema(
                connection
            )
            self.validate_no_persisted_residual_rows(connection)
            video_schema_mode = self._video_schema_mode(
                connection, asset_columns, segment_columns
            )
            if not (
                video_schema_mode
                and video_schema_mode.startswith("analysis_heads")
            ):
                return None
            video_sql, video_schema_mode = self._video_rows_sql(
                connection,
                asset_columns,
                source_columns,
                segment_columns,
                segment_id_filter=True,
                exclude_archived=False,
                require_available_source=False,
                require_active_root=False,
                require_source_binding=False,
            )
            try:
                row = connection.execute(video_sql, (segment_id,)).fetchone()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens video segment could not be read.",
                    code="database_unavailable",
                ) from exc
        if row is None:
            return None
        raw = dict(row)
        segment = self._compact_segment(raw)
        return {
            "object": "memolens.media_segment_detail",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "asset": {
                "object": "memolens.media_asset",
                "schema_version": "1",
                "id": raw.get("asset_id"),
                "kind": "video",
                "sha256": raw.get("asset_sha256"),
                "asset_source_id": raw.get("asset_source_id"),
                "filename": raw.get("filename"),
                "source_availability": raw.get("source_availability"),
                "library_root_status": raw.get("library_root_status"),
                "duration_ms": raw.get("asset_duration_ms"),
                "width": raw.get("asset_width"),
                "height": raw.get("asset_height"),
                "rotation_degrees": raw.get("asset_rotation_degrees"),
                "review": {
                    "revision": int(raw.get("review_revision") or 0),
                    "inbox_state": raw.get("inbox_state") or "inbox",
                    "favorite": bool(raw.get("favorite")),
                    "project_ready": bool(raw.get("project_ready")),
                },
            },
            "segment": segment,
            "video_index_status": video_schema_mode,
            "safety": safety_summary(),
        }

    def _rank_video_rows(
        self,
        cursor: sqlite3.Cursor,
        *,
        query: str,
        limit: int,
        excluded_asset_sha256: frozenset[str] = frozenset(),
        candidate_predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> tuple[list[RankedRow], int, int]:
        phrase = query.casefold()
        terms = query_terms(query)
        scored: list[RankedRow] = []
        scanned_count = 0
        excluded_derivative_count = 0
        sequence = 0
        while rows := cursor.fetchmany(512):
            for row in rows:
                scanned_count += 1
                raw = dict(row)
                if raw.get("asset_sha256") in excluded_asset_sha256:
                    excluded_derivative_count += 1
                    continue
                if candidate_predicate is not None and not candidate_predicate(raw):
                    continue
                score, matched = self._score_video(raw, query, phrase, terms)
                if score <= 0:
                    continue
                raw["matched_terms"] = list(dict.fromkeys(matched))
                raw["raw_score"] = score
                candidate = (score, str(raw.get("id") or ""), sequence, raw)
                sequence += 1
                keep_best(scored, candidate, limit)
        return scored, scanned_count, excluded_derivative_count

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
        result = {
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
            "input_asset_sha256": raw.get("input_asset_sha256"),
            "review": {
                "revision": int(raw.get("review_revision") or 0),
                "inbox_state": raw.get("inbox_state") or "inbox",
                "favorite": bool(raw.get("favorite")),
                "project_ready": bool(raw.get("project_ready")),
            },
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
        source_facts = {
            "asset_source_id": raw.get("asset_source_id"),
            "asset_id": raw.get("asset_id"),
            "asset_sha256": raw.get("asset_sha256"),
            "library_root_id": raw.get("source_library_root_id"),
            "observed_size": raw.get("source_observed_size"),
            "observed_mtime_ns": raw.get("source_observed_mtime_ns"),
            "source_file_id": raw.get("source_file_id"),
        }
        try:
            result["source_binding_sha256"] = source_binding_sha256(source_facts)
        except ResidualAuthorityError:
            # Compatibility-only parents remain readable, but cannot be
            # promoted into an exact residual binding.
            pass
        return result
