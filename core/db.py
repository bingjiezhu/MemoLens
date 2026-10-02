from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from .image_analysis_contract import (
    ImageAnalysisContractError,
    canonical_projection_manifest_json,
    require_valid_projection_manifest,
)
from .image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    ImageProjectionRenderError,
    materialize_projected_image_index_values,
    require_valid_projected_image_index_row,
)
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
    aesthetic_score REAL,
    aesthetic_model TEXT,
    technical_quality_score REAL,
    aesthetic_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_image_index_relative_path
    ON image_index(relative_path);

CREATE INDEX IF NOT EXISTS idx_image_index_taken_at
    ON image_index(taken_at);

CREATE INDEX IF NOT EXISTS idx_image_index_place_name
    ON image_index(place_name);
"""

INDEX_STATEMENTS = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_image_index_relative_path ON image_index(relative_path)",
    "CREATE INDEX IF NOT EXISTS idx_image_index_taken_at ON image_index(taken_at)",
    "CREATE INDEX IF NOT EXISTS idx_image_index_place_name ON image_index(place_name)",
)

FALLBACK_DESCRIPTION_PREFIX = "Local image file named "
IMAGE_INDEX_PROJECTOR_AUTHORITY_ERROR = "image_index_projector_authority_required"
IMAGE_INDEX_MANAGED_SCHEMA_ERROR = "image_index_managed_schema_invalid"
CANONICAL_IMAGE_REANALYSIS_GUIDANCE = (
    "Managed V14+ databases accept image_index writes only from the canonical "
    "image projector. Re-import or re-enqueue the image through the approved "
    "Library image-analysis workflow."
)
_CANONICAL_IMAGE_CUTOVER_SCHEMA_VERSION = 14
_CANONICAL_IMAGE_READ_CUTOVER_SCHEMA_VERSION = 17


class ImageIndexDatabaseScopeChangedError(RuntimeError):
    """The SQLite pathname no longer names the admitted database inode."""

    code = "image_index_database_scope_changed"


class ImageIndexRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                managed_version = self._managed_schema_version(connection)
                if (
                    managed_version is not None
                    and managed_version >= _CANONICAL_IMAGE_CUTOVER_SCHEMA_VERSION
                ):
                    connection.execute("PRAGMA query_only=ON")
                    self._validate_managed_image_index_schema(connection)
                    connection.rollback()
                    return

                # Only an image-index-only database or a structurally valid
                # pre-V14 database can reach these writes. The metadata guard
                # and DDL share one snapshot, so a concurrent cutover can only
                # make the read transaction fail to upgrade; it cannot admit a
                # legacy repair after V14 commits.
                self._ensure_legacy_schema(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _ensure_legacy_schema(self, connection: sqlite3.Connection) -> None:
        if not self._table_exists(connection, "image_index"):
            connection.execute(CREATE_TABLE_SQL)
            self._ensure_indexes(connection)
            return

        columns = self._column_names(connection, "image_index")
        if "embedding_backend" not in columns or "embedding" not in columns:
            self._migrate_legacy_schema(connection)
            columns = self._column_names(connection, "image_index")

        self._ensure_text_columns(connection, columns)
        self._ensure_quality_columns(connection, columns)
        self._ensure_indexes(connection)

    def require_legacy_mutation_authority(self) -> None:
        """Admit only an index-only or structurally valid pre-V14 writer."""

        with closing(self._connect()) as connection:
            self._require_legacy_mutation_allowed(connection)

    def upsert(self, record: StoredImageRecord) -> None:
        with closing(self._connect()) as connection, connection:
            self._begin_legacy_mutation(connection)
            # Keep the path cleanup and replacement in one transaction. If the
            # INSERT fails (for example because of a SHA collision), SQLite
            # rolls the cleanup back instead of silently dropping the old row.
            connection.execute(
                "DELETE FROM image_index WHERE relative_path = ? AND id != ?",
                (record.relative_path, record.id),
            )
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
                    aesthetic_score,
                    aesthetic_model,
                    technical_quality_score,
                    aesthetic_updated_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    aesthetic_score = excluded.aesthetic_score,
                    aesthetic_model = excluded.aesthetic_model,
                    technical_quality_score = excluded.technical_quality_score,
                    aesthetic_updated_at = excluded.aesthetic_updated_at,
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
                    record.aesthetic_score,
                    record.aesthetic_model,
                    record.technical_quality_score,
                    record.aesthetic_updated_at,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def has_sha256(self, sha256: str) -> bool:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM image_index WHERE sha256 = ? LIMIT 1",
                (sha256,),
            ).fetchone()
        return row is not None

    def get_by_sha256(self, sha256: str) -> sqlite3.Row | None:
        with closing(self._connect()) as connection, connection:
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
        with closing(self._connect()) as connection, connection:
            self._begin_legacy_mutation(connection)
            # A content-identical file may have moved onto a path previously
            # occupied by another SHA. Serialize that check/cleanup/update so
            # readers never observe two records for the same filesystem path,
            # and so a missing source SHA cannot delete the destination row.
            existing = connection.execute(
                "SELECT 1 FROM image_index WHERE sha256 = ? LIMIT 1",
                (sha256,),
            ).fetchone()
            if existing is None:
                raise LookupError(f"Indexed SHA256 no longer exists: {sha256}")
            connection.execute(
                "DELETE FROM image_index WHERE relative_path = ? AND sha256 != ?",
                (relative_path, sha256),
            )
            cursor = connection.execute(
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
            if cursor.rowcount != 1:
                raise RuntimeError(f"Failed to refresh indexed SHA256: {sha256}")

    @staticmethod
    def _empty_index_health_summary(
        *,
        projection_status: str,
        reason_code: str | None,
    ) -> dict[str, int | float | bool | str | None]:
        return {
            "total_records": 0,
            "fallback_records": 0,
            "fallback_ratio": 0.0,
            "aesthetic_records": 0,
            "aesthetic_missing": 0,
            "needs_reindex": False,
            "projection_ready": False,
            "projection_status": projection_status,
            "projection_reason_code": reason_code,
            "projection_generation_id": None,
        }

    def _database_file_identity(self) -> tuple[int, int]:
        try:
            observed = os.stat(self.db_path, follow_symlinks=False)
        except OSError as exc:
            raise ImageIndexDatabaseScopeChangedError(
                ImageIndexDatabaseScopeChangedError.code
            ) from exc
        if not stat.S_ISREG(observed.st_mode):
            raise ImageIndexDatabaseScopeChangedError(
                ImageIndexDatabaseScopeChangedError.code
            )
        return int(observed.st_dev), int(observed.st_ino)

    @classmethod
    def _summarize_legacy_index_health(
        cls,
        connection: sqlite3.Connection,
    ) -> dict[str, int | float | bool | str | None]:
        summary = cls._empty_index_health_summary(
            projection_status="legacy",
            reason_code=None,
        )
        if not cls._table_exists(connection, "image_index"):
            return summary
        total_row = connection.execute(
            "SELECT COUNT(*) AS count FROM image_index"
        ).fetchone()
        fallback_row = connection.execute(
            "SELECT COUNT(*) AS count FROM image_index WHERE description LIKE ?",
            (f"{FALLBACK_DESCRIPTION_PREFIX}%",),
        ).fetchone()
        columns = cls._column_names(connection, "image_index")
        aesthetic_row = (
            connection.execute(
                "SELECT COUNT(*) AS count FROM image_index "
                "WHERE aesthetic_score IS NOT NULL"
            ).fetchone()
            if "aesthetic_score" in columns
            else None
        )
        total_records = int(total_row["count"]) if total_row is not None else 0
        fallback_records = int(fallback_row["count"]) if fallback_row is not None else 0
        aesthetic_records = int(aesthetic_row["count"]) if aesthetic_row is not None else 0
        fallback_ratio = (fallback_records / total_records) if total_records > 0 else 0.0
        summary["total_records"] = total_records
        summary["fallback_records"] = fallback_records
        summary["fallback_ratio"] = fallback_ratio
        summary["aesthetic_records"] = aesthetic_records
        summary["aesthetic_missing"] = max(0, total_records - aesthetic_records)
        summary["needs_reindex"] = total_records > 0 and fallback_ratio >= 0.25
        return summary

    @classmethod
    def _managed_projection_health_summary(
        cls,
        connection: sqlite3.Connection,
        *,
        managed_version: int,
    ) -> dict[str, int | float | bool | str | None]:
        unavailable = cls._empty_index_health_summary(
            projection_status="unavailable",
            reason_code="image_projection_read_cutover_unavailable",
        )
        required_tables = (
            "image_index",
            "image_projection_generations",
            "image_projection_manifests",
            "image_projection_read_manifests",
            "image_projection_rows",
        )
        if managed_version < _CANONICAL_IMAGE_READ_CUTOVER_SCHEMA_VERSION or any(
            not cls._table_exists(connection, table_name)
            for table_name in required_tables
        ):
            return unavailable

        active_rows = connection.execute(
            """SELECT generation.id,generation.database_uuid,
                      generation.canonical_high_water_position,
                      generation.compiler_id,generation.projector_version,
                      manifest.eligible_count,manifest.projected_count,
                      manifest.alias_count,manifest.missing_count,
                      manifest.unexpected_count,manifest.mismatched_count,
                      manifest.blocked_count,manifest.alias_set_sha256,
                      manifest.row_set_sha256,manifest.manifest_json,
                      manifest.manifest_sha256,
                      read_manifest.database_uuid AS read_database_uuid,
                      read_manifest.canonical_high_water_position
                        AS read_canonical_high_water_position,
                      read_manifest.compiler_id AS read_compiler_id,
                      read_manifest.projector_version AS read_projector_version,
                      read_manifest.eligible_count AS read_eligible_count,
                      read_manifest.projected_count AS read_projected_count,
                      read_manifest.alias_count AS read_alias_count,
                      read_manifest.missing_count AS read_missing_count,
                      read_manifest.unexpected_count AS read_unexpected_count,
                      read_manifest.mismatched_count AS read_mismatched_count,
                      read_manifest.blocked_count AS read_blocked_count,
                      read_manifest.alias_set_sha256 AS read_alias_set_sha256,
                      read_manifest.row_set_sha256 AS read_row_set_sha256,
                      read_manifest.manifest_json AS read_manifest_json,
                      read_manifest.manifest_sha256 AS read_manifest_sha256
                 FROM database_meta meta
                 JOIN image_projection_generations generation
                   ON generation.database_uuid=meta.database_uuid
                  AND generation.projection_contract=
                      'legacy-image-index-shadow/v1'
                  AND generation.status='complete'
                  AND generation.is_active=1
                 LEFT JOIN image_projection_manifests manifest
                   ON manifest.generation_id=generation.id
                 LEFT JOIN image_projection_read_manifests read_manifest
                   ON read_manifest.generation_id=generation.id
                WHERE meta.singleton=1"""
        ).fetchall()
        if len(active_rows) != 1:
            unavailable["projection_reason_code"] = (
                "image_projection_active_generation_unavailable"
            )
            return unavailable
        active = active_rows[0]
        mirrored_fields = (
            ("database_uuid", "read_database_uuid"),
            ("canonical_high_water_position", "read_canonical_high_water_position"),
            ("compiler_id", "read_compiler_id"),
            ("projector_version", "read_projector_version"),
            ("eligible_count", "read_eligible_count"),
            ("projected_count", "read_projected_count"),
            ("alias_count", "read_alias_count"),
            ("missing_count", "read_missing_count"),
            ("unexpected_count", "read_unexpected_count"),
            ("mismatched_count", "read_mismatched_count"),
            ("blocked_count", "read_blocked_count"),
            ("alias_set_sha256", "read_alias_set_sha256"),
            ("row_set_sha256", "read_row_set_sha256"),
            ("manifest_json", "read_manifest_json"),
            ("manifest_sha256", "read_manifest_sha256"),
        )
        clean_counts = (
            active["eligible_count"] == active["projected_count"]
            and active["missing_count"] == 0
            and active["unexpected_count"] == 0
            and active["mismatched_count"] == 0
            and active["blocked_count"] == 0
        )
        exact_read_mirror = all(
            active[canonical_field] == active[read_field]
            for canonical_field, read_field in mirrored_fields
        )
        if not clean_counts or not exact_read_mirror:
            unavailable["projection_reason_code"] = (
                "image_projection_read_manifest_unavailable"
            )
            return unavailable
        try:
            manifest = json.loads(str(active["manifest_json"]))
            require_valid_projection_manifest(manifest)
            if (
                canonical_projection_manifest_json(manifest)
                != active["manifest_json"]
                or manifest["database_uuid"] != active["database_uuid"]
                or manifest["canonical_high_water_mark"]
                != active["canonical_high_water_position"]
                or manifest["compiler_version"] != active["compiler_id"]
                or manifest["projector_version"] != active["projector_version"]
            ):
                raise ValueError("projection manifest binding mismatch")
            candidates = cls._managed_candidate_rows(
                connection,
                has_review_view=False,
                date_from=None,
                date_to=None,
                location_text=None,
            )
        except (
            ImageAnalysisContractError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            unavailable["projection_reason_code"] = (
                "image_projection_read_snapshot_unavailable"
            )
            return unavailable
        if len(candidates) != int(active["eligible_count"]):
            unavailable["projection_reason_code"] = (
                "image_projection_read_snapshot_unavailable"
            )
            return unavailable

        columns_sql = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        physical_rows = connection.execute(
            f"SELECT {columns_sql} FROM image_index ORDER BY id"
        ).fetchall()
        physical_by_id = {str(row["id"]): row for row in physical_rows}
        candidates_by_id = {str(row["id"]): row for row in candidates}
        if (
            len(physical_by_id) != len(physical_rows)
            or len(candidates_by_id) != len(candidates)
            or set(physical_by_id) != set(candidates_by_id)
        ):
            unavailable["projection_reason_code"] = (
                "image_projection_physical_parity_unavailable"
            )
            return unavailable
        semantic_columns = tuple(
            column
            for column in IMAGE_INDEX_SQL_COLUMNS
            if column not in {"created_at", "updated_at"}
        )
        for asset_id, candidate in candidates_by_id.items():
            physical = physical_by_id[asset_id]
            if (
                type(physical["created_at"]) is not str
                or type(physical["updated_at"]) is not str
                or any(
                    physical[column] != candidate[column]
                    for column in semantic_columns
                )
            ):
                unavailable["projection_reason_code"] = (
                    "image_projection_physical_parity_unavailable"
                )
                return unavailable

        total_records = len(candidates)
        fallback_records = sum(
            isinstance(row["description"], str)
            and str(row["description"]).startswith(FALLBACK_DESCRIPTION_PREFIX)
            for row in candidates
        )
        aesthetic_records = sum(
            row["aesthetic_score"] is not None for row in candidates
        )
        fallback_ratio = (
            fallback_records / total_records if total_records > 0 else 0.0
        )
        return {
            "total_records": total_records,
            "fallback_records": fallback_records,
            "fallback_ratio": fallback_ratio,
            "aesthetic_records": aesthetic_records,
            "aesthetic_missing": max(0, total_records - aesthetic_records),
            "needs_reindex": total_records > 0 and fallback_ratio >= 0.25,
            "projection_ready": True,
            "projection_status": "ready",
            "projection_reason_code": None,
            "projection_generation_id": str(active["id"]),
        }

    def summarize_index_health(
        self,
        *,
        expected_file_identity: tuple[int, int] | None = None,
    ) -> dict[str, int | float | bool | str | None]:
        try:
            initial_identity = self._database_file_identity()
        except ImageIndexDatabaseScopeChangedError:
            if expected_file_identity is not None:
                raise
            return self._empty_index_health_summary(
                projection_status="unavailable",
                reason_code="image_index_database_unavailable",
            )
        if (
            expected_file_identity is not None
            and initial_identity != expected_file_identity
        ):
            raise ImageIndexDatabaseScopeChangedError(
                ImageIndexDatabaseScopeChangedError.code
            )

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            managed_version = self._managed_schema_version(connection)
            if (
                managed_version is not None
                and managed_version >= _CANONICAL_IMAGE_CUTOVER_SCHEMA_VERSION
            ):
                summary = self._managed_projection_health_summary(
                    connection,
                    managed_version=managed_version,
                )
            else:
                summary = self._summarize_legacy_index_health(connection)
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

        if self._database_file_identity() != initial_identity:
            raise ImageIndexDatabaseScopeChangedError(
                ImageIndexDatabaseScopeChangedError.code
            )
        return summary

    @staticmethod
    def _managed_candidate_rows(
        connection: sqlite3.Connection,
        *,
        has_review_view: bool,
        date_from: str | None,
        date_to: str | None,
        location_text: str | None,
    ) -> list[dict[str, object]]:
        """Read ranking inputs from the immutable active generation.

        ``image_index`` remains a derived compatibility surface.  Reading its
        mutable fields before top-k ranking would let later physical drift omit
        an otherwise-current canonical candidate.  V17 therefore scopes with
        both clean manifests and decodes the immutable generation ``row_json``.
        The route still independently verifies selected observations against
        current physical parity before publishing them.
        """

        archived_filter = (
            """AND NOT EXISTS(
                   SELECT 1 FROM current_asset_reviews review
                    WHERE review.asset_id=projected.asset_id
                      AND review.inbox_state='archived')"""
            if has_review_view
            else ""
        )
        stored_rows = connection.execute(
            f"""
            SELECT generation.id AS canonical_generation_id,
                   generation.created_at AS generation_created_at,
                   generation.projector_version AS generation_projector_version,
                   projected.asset_id,projected.analysis_run_id,
                   projected.revision,projected.content_sha256,
                   projected.source_id,projected.source_binding_sha256,
                   projected.row_json,projected.row_sha256
              FROM database_meta meta
              JOIN image_projection_generations generation
                ON generation.database_uuid=meta.database_uuid
               AND generation.projection_contract=
                   'legacy-image-index-shadow/v1'
               AND generation.status='complete'
               AND generation.is_active=1
              JOIN image_projection_manifests manifest
                ON manifest.generation_id=generation.id
               AND manifest.database_uuid=generation.database_uuid
               AND manifest.canonical_high_water_position=
                   generation.canonical_high_water_position
               AND manifest.compiler_id=generation.compiler_id
               AND manifest.projector_version=generation.projector_version
               AND manifest.eligible_count=manifest.projected_count
               AND manifest.missing_count=0
               AND manifest.unexpected_count=0
               AND manifest.mismatched_count=0
               AND manifest.blocked_count=0
              JOIN image_projection_read_manifests read_manifest
                ON read_manifest.generation_id=generation.id
               AND read_manifest.database_uuid=generation.database_uuid
               AND read_manifest.canonical_high_water_position=
                   generation.canonical_high_water_position
               AND read_manifest.compiler_id=generation.compiler_id
               AND read_manifest.projector_version=generation.projector_version
               AND read_manifest.eligible_count=read_manifest.projected_count
               AND read_manifest.missing_count=0
               AND read_manifest.unexpected_count=0
               AND read_manifest.mismatched_count=0
               AND read_manifest.blocked_count=0
               AND read_manifest.manifest_json=manifest.manifest_json
               AND read_manifest.manifest_sha256=manifest.manifest_sha256
              JOIN image_projection_rows projected
                ON projected.generation_id=generation.id
              JOIN assets asset
                ON asset.id=projected.asset_id AND asset.kind='image'
              JOIN image_analysis_heads head
                ON head.asset_id=projected.asset_id
               AND head.analysis_run_id=projected.analysis_run_id
               AND head.revision=projected.revision
               AND head.content_sha256=projected.content_sha256
              JOIN image_analysis_results result
                ON result.asset_id=head.asset_id
               AND result.analysis_run_id=head.analysis_run_id
               AND result.revision=head.revision
               AND result.content_sha256=head.content_sha256
               AND result.source_id=projected.source_id
               AND result.source_binding_sha256=
                   projected.source_binding_sha256
              JOIN asset_sources source
                ON source.id=projected.source_id
               AND source.asset_id=asset.id
               AND source.availability='available'
              JOIN library_roots root
                ON root.id=source.library_root_id AND root.status='active'
             WHERE meta.singleton=1 {archived_filter}
             ORDER BY projected.asset_id
            """
        ).fetchall()

        candidates: list[dict[str, object]] = []
        for stored in stored_rows:
            try:
                document = json.loads(str(stored["row_json"]))
                require_valid_projected_image_index_row(document)
                analysis_binding = document["analysis_binding"]
                if (
                    document["asset_id"] != stored["asset_id"]
                    or document["projector_version"]
                    != stored["generation_projector_version"]
                    or analysis_binding
                    != {
                        "analysis_run_id": stored["analysis_run_id"],
                        "revision": stored["revision"],
                        "content_sha256": stored["content_sha256"],
                    }
                    or document["source_binding_sha256"]
                    != stored["source_binding_sha256"]
                    or document["row_sha256"] != stored["row_sha256"]
                ):
                    raise ValueError("projection row binding mismatch")
                values = materialize_projected_image_index_values(
                    document,
                    created_at=str(stored["generation_created_at"]),
                    updated_at=str(stored["generation_created_at"]),
                )
            except (
                ImageProjectionRenderError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise RuntimeError("managed_image_projection_row_invalid") from exc
            candidate = dict(zip(IMAGE_INDEX_SQL_COLUMNS, values, strict=True))
            if candidate["id"] != stored["asset_id"]:
                raise RuntimeError("managed_image_projection_row_invalid")
            candidate.update(
                {
                    "canonical_generation_id": stored[
                        "canonical_generation_id"
                    ],
                    "canonical_analysis_run_id": stored["analysis_run_id"],
                    "canonical_analysis_revision": stored["revision"],
                    "canonical_content_sha256": stored["content_sha256"],
                    "canonical_row_sha256": stored["row_sha256"],
                }
            )
            taken_at = candidate["taken_at"]
            if date_from and (not isinstance(taken_at, str) or taken_at < date_from):
                continue
            if date_to and (not isinstance(taken_at, str) or taken_at > date_to):
                continue
            candidates.append(candidate)

        candidates.sort(key=lambda row: str(row["filename"]))
        candidates.sort(
            key=lambda row: str(row["taken_at"] or ""),
            reverse=True,
        )
        normalized_location = str(location_text or "").strip().lower()
        if normalized_location:
            filtered = [
                row
                for row in candidates
                if any(
                    normalized_location in str(row[field] or "").lower()
                    for field in ("place_name", "country", "combined_text")
                )
            ]
            if filtered:
                return filtered
        return candidates

    def fetch_candidates(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        location_text: str | None = None,
    ) -> list[sqlite3.Row | dict[str, object]]:
        with closing(self._connect()) as connection:
            managed_version = self._managed_schema_version(connection)
            has_review_view = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='view' AND name='current_asset_reviews'"
            ).fetchone()
            if (
                managed_version is not None
                and managed_version >= _CANONICAL_IMAGE_CUTOVER_SCHEMA_VERSION
            ):
                if not self._table_exists(
                    connection,
                    "image_projection_read_manifests",
                ):
                    return []
                return self._managed_candidate_rows(
                    connection,
                    has_review_view=has_review_view is not None,
                    date_from=date_from,
                    date_to=date_to,
                    location_text=location_text,
                )

            where_clauses: list[str] = []
            params: list[str] = []
            if date_from:
                where_clauses.append(
                    "image_index.taken_at IS NOT NULL AND image_index.taken_at >= ?"
                )
                params.append(date_from)
            if date_to:
                where_clauses.append(
                    "image_index.taken_at IS NOT NULL AND image_index.taken_at <= ?"
                )
                params.append(date_to)
            if has_review_view:
                where_clauses.append(
                    "NOT EXISTS (SELECT 1 FROM current_asset_reviews review "
                    "WHERE review.asset_id=image_index.id "
                    "AND review.inbox_state='archived')"
                )
            sql = """
                SELECT image_index.*,
                       NULL AS canonical_generation_id,
                       NULL AS canonical_analysis_run_id,
                       NULL AS canonical_analysis_revision,
                       NULL AS canonical_content_sha256,
                       NULL AS canonical_row_sha256
                  FROM image_index
            """
            base_where_sql = (
                " WHERE " + " AND ".join(where_clauses)
                if where_clauses
                else ""
            )
            order_sql = (
                " ORDER BY image_index.taken_at IS NULL, "
                "image_index.taken_at DESC, image_index.filename ASC"
            )
            normalized_location = str(location_text or "").strip().lower()
            if normalized_location:
                location_clause = (
                    "(LOWER(COALESCE(image_index.place_name, '')) LIKE ? "
                    "OR LOWER(COALESCE(image_index.country, '')) LIKE ? "
                    "OR LOWER(COALESCE(image_index.combined_text, '')) LIKE ?)"
                )
                location_where = (
                    f"{base_where_sql} AND {location_clause}"
                    if base_where_sql
                    else f" WHERE {location_clause}"
                )
                pattern = f"%{normalized_location}%"
                filtered = connection.execute(
                    sql + location_where + order_sql,
                    [*params, pattern, pattern, pattern],
                ).fetchall()
                if filtered:
                    return filtered
            return connection.execute(
                sql + base_where_sql + order_sql,
                params,
            ).fetchall()

    def fetch_quality_backfill_candidates(
        self,
        *,
        force: bool,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT
                id,
                filename,
                relative_path,
                description,
                tags_json,
                combined_text,
                aesthetic_score
            FROM image_index
        """
        params: list[object] = []
        if not force:
            sql += " WHERE aesthetic_score IS NULL"
        sql += " ORDER BY updated_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with closing(self._connect()) as connection:
            return connection.execute(sql, params).fetchall()

    def fetch_text_embedding_backfill_candidates(
        self,
        *,
        recompute: bool,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT
                id,
                description,
                tags_json,
                place_name,
                country,
                combined_text,
                combined_text_embedding
            FROM image_index
        """
        params: list[object] = []
        if not recompute:
            sql += (
                " WHERE (combined_text_embedding IS NULL "
                "OR length(combined_text_embedding) = 0)"
            )
        sql += " ORDER BY updated_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with closing(self._connect()) as connection:
            return connection.execute(sql, params).fetchall()

    def update_image_quality(
        self,
        *,
        image_id: str,
        aesthetic_score: float,
        aesthetic_model: str,
        technical_quality_score: float | None,
        aesthetic_updated_at: str,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            self._begin_legacy_mutation(connection)
            connection.execute(
                """
                UPDATE image_index
                SET
                    aesthetic_score = ?,
                    aesthetic_model = ?,
                    technical_quality_score = ?,
                    aesthetic_updated_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    aesthetic_score,
                    aesthetic_model,
                    technical_quality_score,
                    aesthetic_updated_at,
                    aesthetic_updated_at,
                    image_id,
                ),
            )

    def update_text_embedding(
        self,
        *,
        image_id: str,
        combined_text: str,
        text_embedding_model: str,
        combined_text_embedding: bytes,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            self._begin_legacy_mutation(connection)
            connection.execute(
                """
                UPDATE image_index
                SET combined_text = ?, text_embedding_model = ?,
                    combined_text_embedding = ?
                WHERE id = ?
                """,
                (
                    combined_text,
                    text_embedding_model,
                    combined_text_embedding,
                    image_id,
                ),
            )

    def delete_by_relative_path(self, relative_path: str, keep_id: str | None = None) -> None:
        sql = "DELETE FROM image_index WHERE relative_path = ?"
        params: tuple[str, ...] | tuple[str, str]

        if keep_id:
            sql += " AND id != ?"
            params = (relative_path, keep_id)
        else:
            params = (relative_path,)

        with closing(self._connect()) as connection, connection:
            self._begin_legacy_mutation(connection)
            connection.execute(sql, params)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @classmethod
    def _require_legacy_mutation_allowed(cls, connection: sqlite3.Connection) -> None:
        """Close legacy mutation APIs after the canonical V14 cutover.

        The canonical projector writes ``image_index`` through its caller-owned
        transaction so row, alias, receipt, manifest, and active generation
        remain one atomic commit.  Index-only pre-V14 databases retain these
        methods solely for migration compatibility.
        """

        managed_version = cls._managed_schema_version(connection)
        if (
            managed_version is not None
            and managed_version >= _CANONICAL_IMAGE_CUTOVER_SCHEMA_VERSION
        ):
            raise RuntimeError(IMAGE_INDEX_PROJECTOR_AUTHORITY_ERROR)

    @classmethod
    def _begin_legacy_mutation(cls, connection: sqlite3.Connection) -> None:
        """Bind legacy admission and its mutation to one SQLite snapshot."""

        connection.execute("BEGIN IMMEDIATE")
        cls._require_legacy_mutation_allowed(connection)

    @classmethod
    def _managed_schema_version(cls, connection: sqlite3.Connection) -> int | None:
        """Read and structurally validate managed-schema markers without writes."""

        meta_exists = cls._table_exists(connection, "database_meta")
        migrations_exist = cls._table_exists(connection, "schema_migrations")
        if not meta_exists and not migrations_exist:
            return None
        if meta_exists != migrations_exist:
            raise RuntimeError(f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:metadata_incomplete")

        meta_columns = cls._column_names(connection, "database_meta")
        migration_columns = cls._column_names(connection, "schema_migrations")
        if not {"singleton", "schema_version"}.issubset(meta_columns) or "version" not in (
            migration_columns
        ):
            raise RuntimeError(f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:metadata_shape")

        meta_rows = connection.execute(
            "SELECT schema_version FROM database_meta WHERE singleton=1"
        ).fetchall()
        if len(meta_rows) != 1:
            raise RuntimeError(f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:metadata_ambiguous")
        try:
            schema_version = int(meta_rows[0]["schema_version"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:schema_version"
            ) from exc
        if schema_version < 2:
            raise RuntimeError(f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:schema_version")

        try:
            versions = [
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:migration_history"
            ) from exc
        if len(versions) != schema_version or versions != list(
            range(1, schema_version + 1)
        ):
            raise RuntimeError(
                f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:migration_history"
            )
        return schema_version

    @classmethod
    def _validate_managed_image_index_schema(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Validate the exact managed projection table without repairing it."""

        def normalized_schema_sql(value: str) -> str:
            return " ".join(value.strip().rstrip(";").split())

        expected = {
            ("table", "image_index"): normalized_schema_sql(
                CREATE_TABLE_SQL.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
            ),
        }
        for statement in INDEX_STATEMENTS:
            name = statement.split("INDEX IF NOT EXISTS ", 1)[1].split(" ", 1)[0]
            expected[("index", name)] = normalized_schema_sql(
                statement.replace(" IF NOT EXISTS", "", 1)
            )

        rows = connection.execute(
            """SELECT type,name,sql FROM sqlite_schema
                 WHERE tbl_name='image_index' AND sql IS NOT NULL
                   AND type IN ('table','index','trigger')
                 ORDER BY type,name"""
        ).fetchall()
        actual = {
            (str(row["type"]), str(row["name"])): normalized_schema_sql(
                str(row["sql"])
            )
            for row in rows
        }
        if actual != expected:
            raise RuntimeError(f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:image_index")

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
        index_rows = connection.execute("PRAGMA index_list(image_index)").fetchall()
        relative_path_index = next(
            (row for row in index_rows if row["name"] == "idx_image_index_relative_path"),
            None,
        )
        if relative_path_index is None or not bool(relative_path_index["unique"]):
            # Versions before 0.2 could leave more than one row pointing at the
            # same relative path after a file was replaced with already-indexed
            # bytes. Keep the most recently updated row before enforcing the
            # invariant at the database layer. This only removes stale index
            # rows; original files are never touched and can be reindexed.
            connection.execute(
                """
                DELETE FROM image_index
                WHERE EXISTS (
                    SELECT 1
                    FROM image_index AS preferred
                    WHERE preferred.relative_path = image_index.relative_path
                      AND (
                        COALESCE(preferred.updated_at, '') > COALESCE(image_index.updated_at, '')
                        OR (
                          COALESCE(preferred.updated_at, '') = COALESCE(image_index.updated_at, '')
                          AND preferred.rowid > image_index.rowid
                        )
                      )
                )
                """
            )
            # SQLite's IF NOT EXISTS would keep the old non-unique definition
            # under the same name, so replace it only during this migration.
            connection.execute("DROP INDEX IF EXISTS idx_image_index_relative_path")
        for statement in INDEX_STATEMENTS:
            connection.execute(statement)

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

    @staticmethod
    def _ensure_quality_columns(connection: sqlite3.Connection, columns: set[str]) -> None:
        if "aesthetic_score" not in columns:
            connection.execute("ALTER TABLE image_index ADD COLUMN aesthetic_score REAL")
        if "aesthetic_model" not in columns:
            connection.execute("ALTER TABLE image_index ADD COLUMN aesthetic_model TEXT")
        if "technical_quality_score" not in columns:
            connection.execute("ALTER TABLE image_index ADD COLUMN technical_quality_score REAL")
        if "aesthetic_updated_at" not in columns:
            connection.execute("ALTER TABLE image_index ADD COLUMN aesthetic_updated_at TEXT")

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
                aesthetic_score,
                aesthetic_model,
                technical_quality_score,
                aesthetic_updated_at,
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
                NULL,
                NULL,
                NULL,
                NULL,
                created_at,
                updated_at
            FROM image_index
            """
        )

        connection.execute("DROP TABLE image_index")
        connection.execute("ALTER TABLE image_index_new RENAME TO image_index")
        self._ensure_indexes(connection)
