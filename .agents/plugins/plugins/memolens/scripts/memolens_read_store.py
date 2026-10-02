"""Facade over cohesive read-only MemoLens SQLite repositories."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from memolens_blueprint_authority import validate_exact_managed_schema_manifest
from memolens_blueprint_store import CreativeBlueprintService
from memolens_contracts import MemoLensError, safety_summary
from memolens_creative_blueprint import canonical_json, canonical_sha256
from memolens_creator_store import CreatorMemoryReader
from memolens_export_authority import project_validated_derivative_facts
from memolens_media_store import MediaIndexReader
from memolens_photo_store import PhotoIndexReader
from memolens_persisted_blueprint import PersistedBlueprintReader
from memolens_project_resume import ProjectResumePresenter
from memolens_project_store import ProjectIndexReader
from memolens_residual_authority import (
    ResidualAuthorityError,
    annotate_and_materialize,
    candidate_admitted,
    current_candidate_usage_facts,
    parse_usage_filters,
    usage_preference,
)
from memolens_sqlite import ReadOnlyDatabase
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json
from memolens_timeline_store import TimelineIndexReader


_LIBRARY_SCAN_INTEGRITY_ERROR = "library_scan_status_integrity_error"
_LIBRARY_BOOTSTRAP_RECEIPT_DOMAIN = b"memolens.library_bootstrap_receipt.v1\0"
_V20_LIBRARY_BOOTSTRAP_SCHEMA_NAMES = (
    "idx_library_bootstrap_receipts_project",
    "idx_library_bootstrap_receipts_root",
    "library_bootstrap_receipts",
    "trg_library_bootstrap_briefs_no_delete",
    "trg_library_bootstrap_briefs_no_update",
    "trg_library_bootstrap_jobs_identity_immutable",
    "trg_library_bootstrap_receipts_complete",
    "trg_library_bootstrap_receipts_no_delete",
    "trg_library_bootstrap_receipts_no_update",
    "trg_library_bootstrap_roots_identity_immutable",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LIBRARY_BOOTSTRAP_REQUEST_ID = re.compile(r"^lb_[0-9a-f]{64}$")
_LIBRARY_SCAN_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$"
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807
_LIBRARY_SCAN_CHECKPOINT_KEYS = frozenset(
    {
        "object",
        "schema_version",
        "root_permission_fingerprint",
        "last_relative_path",
        "processed_count",
        "imported_count",
        "skipped_count",
        "rejected_count",
        "child_job_count",
        "batch_count",
    }
)
_LIBRARY_SCAN_ERROR_KEYS = frozenset(
    {
        "object",
        "schema_version",
        "code",
        "stage",
        "retryable",
        "detail_sha256",
    }
)
_LIBRARY_SCAN_ERROR_RETRYABLE = {
    "worker_interrupted": True,
    "user_cancelled": True,
    "batch_failed": True,
    "root_binding_changed": False,
    "checkpoint_invalid": False,
    "authority_invalid": False,
    "unsupported_platform": False,
}
_LIBRARY_SCAN_STATUS_STAGES = {
    "queued": frozenset({"awaiting_scan_worker"}),
    "running": frozenset({"scanning"}),
    "cancelling": frozenset({"scanning"}),
    "interrupted": frozenset({"interrupted"}),
    "cancelled": frozenset({"cancelled"}),
    "failed": frozenset({"failed"}),
    "succeeded": frozenset({"scan_complete", "no_supported_media"}),
}
_LIBRARY_SCAN_CHECKPOINT_LIMITS = StrictJsonLimits(
    max_bytes=16_384,
    max_depth=2,
    max_nodes=24,
    max_object_items=10,
    max_array_items=1,
    max_string_chars=4_096,
    max_integer_bits=53,
)
_LIBRARY_SCAN_ERROR_LIMITS = StrictJsonLimits(
    max_bytes=2_048,
    max_depth=2,
    max_nodes=16,
    max_object_items=6,
    max_array_items=1,
    max_string_chars=128,
    max_integer_bits=53,
)
_LIBRARY_BOOTSTRAP_RESPONSE_LIMITS = StrictJsonLimits(
    max_bytes=2_048,
    max_depth=2,
    max_nodes=24,
    max_object_items=9,
    max_array_items=1,
    max_string_chars=128,
    max_integer_bits=53,
)
_LIBRARY_BOOTSTRAP_RECEIPT_MATERIAL_KEYS = (
    "database_uuid",
    "authenticated_principal",
    "command_type",
    "command_version",
    "request_id",
    "request_sha256",
    "intent_created_at_ms",
    "intent_expires_at_ms",
    "native_confirmed_at_ms",
    "candidate_binding_sha256",
    "library_root_id",
    "library_root_fingerprint",
    "library_root_device",
    "library_root_inode",
    "project_id",
    "brief_revision",
    "brief_content_sha256",
    "scan_job_id",
    "blueprint_operation_id",
    "blueprint_revision",
    "blueprint_content_sha256",
    "blueprint_semantic_sha256",
    "response_status",
    "response_sha256",
    "created_at",
)


def _library_scan_integrity_error() -> MemoLensError:
    return MemoLensError(
        "The committed Library scan status failed integrity validation.",
        code=_LIBRARY_SCAN_INTEGRITY_ERROR,
    )


def _v20_library_bootstrap_lineage_present(
    connection: sqlite3.Connection,
) -> bool:
    """Detect V20 lineage before allowing any historical-schema downgrade."""

    try:
        placeholders = ",".join(
            "?" for _name in _V20_LIBRARY_BOOTSTRAP_SCHEMA_NAMES
        )
        if connection.execute(
            "SELECT 1 FROM sqlite_schema "
            f"WHERE name IN ({placeholders}) LIMIT 1",
            _V20_LIBRARY_BOOTSTRAP_SCHEMA_NAMES,
        ).fetchone() is not None:
            return True
        migration_relations = connection.execute(
            "SELECT type FROM sqlite_schema "
            "WHERE name='schema_migrations' LIMIT 2"
        ).fetchall()
        if not migration_relations:
            return False
        if len(migration_relations) != 1 or migration_relations[0]["type"] != "table":
            raise _library_scan_integrity_error()
        migration_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(schema_migrations)"
            ).fetchall()
        }
        if "version" not in migration_columns:
            raise _library_scan_integrity_error()
        return (
            connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version>=20 LIMIT 1"
            ).fetchone()
            is not None
        )
    except MemoLensError:
        raise
    except sqlite3.Error as exc:
        raise _library_scan_integrity_error() from exc


def _decode_canonical_object(
    raw: object,
    *,
    limits: StrictJsonLimits,
) -> dict[str, Any]:
    if type(raw) is not str:
        raise _library_scan_integrity_error()
    try:
        parsed = decode_strict_json(raw, limits=limits, require_object=True)
    except (StrictJsonError, UnicodeError) as exc:
        raise _library_scan_integrity_error() from exc
    if type(parsed) is not dict or canonical_json(parsed) != raw:
        raise _library_scan_integrity_error()
    return parsed


class ReadOnlyMemoLensStore:
    """Coordinate one read-only database across photo, media, and timeline readers."""

    def __init__(self, db_path: Path | None, library_dir: Path | None) -> None:
        self.database = ReadOnlyDatabase(db_path)
        self.photos = PhotoIndexReader(self.database, library_dir)
        self.media = MediaIndexReader(self.database)
        self.timelines = TimelineIndexReader(self.database)
        self.creator = CreatorMemoryReader(self.database)
        self.projects = ProjectIndexReader(self.database)
        self.project_presenter = ProjectResumePresenter()
        self.blueprints = CreativeBlueprintService(
            self.database,
            self.projects,
            self.media,
            self.creator,
        )
        self.persisted_blueprints = PersistedBlueprintReader(
            self.database,
            self.projects,
        )

    @property
    def db_path(self) -> Path | None:
        return self.database.path

    def configure(self, *, db_path: Path | None, library_dir: Path | None) -> None:
        self.database.configure(db_path)
        self.photos.configure_library(library_dir)

    def connection(self) -> sqlite3.Connection:
        return self.database.connection()

    def status(self) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            image_columns = self.database.columns(connection, "image_index")
            asset_columns = self.database.columns(connection, "assets")
            self.photos.validate_identity(image_columns)
            self.media.validate_identity(asset_columns)
            if not image_columns and not asset_columns:
                raise MemoLensError(
                    "The SQLite file is not a MemoLens media index.",
                    code="database_identity_mismatch",
                )
            source_columns = self.database.columns(connection, "asset_sources")
            segment_columns = self.database.columns(connection, "video_segments")
            canonical_image_search_available = (
                self.photos.canonical_search_schema_available(connection)
            )
            try:
                counts = self._status_counts(
                    connection,
                    image_columns=image_columns,
                    asset_columns=asset_columns,
                    source_columns=source_columns,
                    segment_columns=segment_columns,
                )
                counts["library_scan"] = self._library_scan_status(connection)
                creator_capabilities = self.creator.schema_capabilities(connection)
                creator_capabilities["inbox_available"] = bool(
                    creator_capabilities["inbox_available"]
                    and self.media.schema_available(asset_columns, source_columns)
                )
                counts.update(creator_capabilities)
                counts.update(self.projects.schema_capabilities(connection))
                counts.update(
                    self.persisted_blueprints.schema_capabilities(connection)
                )
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens SQLite index could not be queried.",
                    code="database_unavailable",
                ) from exc
        return {
            "available": True,
            "open_mode": "read_only",
            **counts,
            # Compatibility key consumed by the existing Gateway capability
            # presenter. It now means the public photo-search lane can enforce
            # exact-current canonical authority; raw legacy rows are not a capability.
            "legacy_search_available": canonical_image_search_available,
            "media_schema_available": self.media.schema_available(
                asset_columns, source_columns
            ),
        }

    def _status_counts(
        self,
        connection: sqlite3.Connection,
        *,
        image_columns: set[str],
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
    ) -> dict[str, Any]:
        image_count = self.photos.count(connection, image_columns)
        kind_counts = self.media.kind_counts(connection, asset_columns)
        asset_count = sum(kind_counts.values()) if asset_columns else image_count
        video_status = self.media.video_status(
            connection,
            asset_columns=asset_columns,
            source_columns=source_columns,
            segment_columns=segment_columns,
        )
        timeline_schema = self.timelines.schema(connection)
        timeline_count, revision_count = self.timelines.counts(
            connection, timeline_schema
        )
        return {
            "asset_count": asset_count,
            "legacy_image_count": image_count,
            "asset_kind_counts": kind_counts,
            "video_segment_count": video_status["default_segment_count"],
            "current_video_asset_count": video_status["default_asset_count"],
            "readable_current_video_segment_count": video_status[
                "readable_segment_count"
            ],
            "readable_current_video_asset_count": video_status[
                "readable_asset_count"
            ],
            "successful_video_head_asset_count": video_status[
                "successful_head_asset_count"
            ],
            "timeline_count": timeline_count,
            "timeline_revision_count": revision_count,
            "video_search_available": video_status["search_available"],
            "video_schema_mode": video_status["mode"],
            "timeline_read_available": timeline_schema is not None,
            "embedding_backends": self.photos.embedding_backends(
                connection, image_columns
            ),
        }

    def _library_scan_status(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, Any] | None:
        """Project only the V20 receipt-anchored Library scan job."""

        meta_columns = self.database.columns(connection, "database_meta")
        if not meta_columns:
            if _v20_library_bootstrap_lineage_present(connection):
                raise _library_scan_integrity_error()
            return None
        expected_meta_columns = {
            "singleton",
            "database_uuid",
            "schema_version",
            "created_at",
            "updated_at",
        }
        legacy_meta_columns = {"database_uuid", "schema_version"}
        if meta_columns == legacy_meta_columns:
            try:
                legacy_rows = connection.execute(
                    "SELECT database_uuid,schema_version "
                    "FROM database_meta LIMIT 2"
                ).fetchall()
            except sqlite3.Error as exc:
                raise _library_scan_integrity_error() from exc
            if (
                len(legacy_rows) != 1
                or type(legacy_rows[0]["database_uuid"]) is not str
                or not legacy_rows[0]["database_uuid"]
                or type(legacy_rows[0]["schema_version"]) is not int
            ):
                raise _library_scan_integrity_error()
            if int(legacy_rows[0]["schema_version"]) < 20:
                if _v20_library_bootstrap_lineage_present(connection):
                    raise _library_scan_integrity_error()
                # Historical fixtures used this two-column identity shape.
                # They remain readable, but cannot claim a receipt-anchored
                # Library scan projection.
                return None
            raise _library_scan_integrity_error()
        if meta_columns != expected_meta_columns:
            # Unknown or partially managed identity shapes are not historical
            # evidence.  In particular, a damaged V20 table must not silently
            # downgrade to a pre-V20 read path.
            raise _library_scan_integrity_error()
        try:
            meta_rows = connection.execute(
                "SELECT singleton,database_uuid,schema_version "
                "FROM database_meta LIMIT 2"
            ).fetchall()
        except sqlite3.Error as exc:
            raise _library_scan_integrity_error() from exc
        if (
            len(meta_rows) != 1
            or meta_rows[0]["singleton"] != 1
            or type(meta_rows[0]["database_uuid"]) is not str
            or not meta_rows[0]["database_uuid"]
            or type(meta_rows[0]["schema_version"]) is not int
        ):
            raise _library_scan_integrity_error()
        schema_version = int(meta_rows[0]["schema_version"])
        if schema_version < 20:
            if _v20_library_bootstrap_lineage_present(connection):
                raise _library_scan_integrity_error()
            return None
        if schema_version != 20:
            raise _library_scan_integrity_error()
        try:
            validate_exact_managed_schema_manifest(connection)
            receipts = connection.execute(
                "SELECT * FROM library_bootstrap_receipts LIMIT 2"
            ).fetchall()
        except (MemoLensError, sqlite3.Error) as exc:
            raise _library_scan_integrity_error() from exc
        if not receipts:
            return None
        if len(receipts) != 1:
            raise _library_scan_integrity_error()
        receipt = receipts[0]
        database_uuid = str(meta_rows[0]["database_uuid"])
        self._validate_library_bootstrap_receipt(
            receipt,
            database_uuid=database_uuid,
        )
        scan_job_id = receipt["scan_job_id"]
        try:
            jobs = connection.execute(
                """SELECT id,database_uuid,kind,asset_id,analysis_run_id,status,
                          stage,progress,attempt,cancel_requested,checkpoint_json,
                          error_json,created_at,started_at,heartbeat_at,finished_at
                     FROM media_jobs WHERE id=? LIMIT 2""",
                (scan_job_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise _library_scan_integrity_error() from exc
        if len(jobs) != 1:
            raise _library_scan_integrity_error()
        job = jobs[0]
        if not (
            job["id"] == scan_job_id
            and job["database_uuid"] == database_uuid
            and job["kind"] == "library_scan"
            and job["asset_id"] is None
            and job["analysis_run_id"] is None
        ):
            raise _library_scan_integrity_error()
        return self._project_library_scan_job(
            job,
            root_permission_fingerprint=str(
                receipt["library_root_fingerprint"]
            ),
        )

    @staticmethod
    def _validate_library_bootstrap_receipt(
        receipt: sqlite3.Row,
        *,
        database_uuid: str,
    ) -> None:
        request_id = receipt["request_id"]
        scan_job_id = receipt["scan_job_id"]
        hashes = (
            "request_sha256",
            "candidate_binding_sha256",
            "library_root_fingerprint",
            "brief_content_sha256",
            "blueprint_content_sha256",
            "blueprint_semantic_sha256",
            "response_sha256",
            "receipt_sha256",
        )
        safe_integers = (
            "intent_created_at_ms",
            "intent_expires_at_ms",
            "native_confirmed_at_ms",
            "brief_revision",
            "blueprint_revision",
            "response_status",
        )
        if not (
            receipt["database_uuid"] == database_uuid
            and receipt["authenticated_principal"] == "main_native_user_gesture"
            and receipt["command_type"] == "library.bootstrap"
            and receipt["command_version"] == "1"
            and type(request_id) is str
            and _LIBRARY_BOOTSTRAP_REQUEST_ID.fullmatch(request_id)
            and type(scan_job_id) is str
            and _LIBRARY_SCAN_JOB_ID.fullmatch(scan_job_id)
            and all(
                type(receipt[key]) is str and _SHA256.fullmatch(receipt[key])
                for key in hashes
            )
            and all(
                type(receipt[key]) is int
                and 0 <= receipt[key] <= _MAX_SAFE_INTEGER
                for key in safe_integers
            )
            and all(
                type(receipt[key]) is int
                and 0 <= receipt[key] <= _MAX_SQLITE_INTEGER
                for key in ("library_root_device", "library_root_inode")
            )
            and receipt["intent_expires_at_ms"]
            == receipt["intent_created_at_ms"] + 300_000
            and receipt["intent_created_at_ms"]
            <= receipt["native_confirmed_at_ms"]
            < receipt["intent_expires_at_ms"]
            and receipt["brief_revision"] == 1
            and receipt["blueprint_revision"] == 1
            and receipt["response_status"] == 201
            and type(receipt["created_at"]) is str
            and _UTC_TIMESTAMP.fullmatch(receipt["created_at"])
        ):
            raise _library_scan_integrity_error()

        response = _decode_canonical_object(
            receipt["response_json"],
            limits=_LIBRARY_BOOTSTRAP_RESPONSE_LIMITS,
        )
        expected_response = {
            "object": "memolens.library_bootstrap_result",
            "schema_version": "1",
            "request_id": request_id,
            "status": "library_authority_committed",
            "scan_started": False,
            "scan_stage": "awaiting_scan_worker",
            "editor_state": "awaiting_grounded_timeline",
            "blueprint_authority": "unverified",
            "timeline_edit_granted": False,
        }
        response_json = str(receipt["response_json"])
        material = {
            key: receipt[key]
            for key in _LIBRARY_BOOTSTRAP_RECEIPT_MATERIAL_KEYS
        }
        expected_receipt_sha256 = hashlib.sha256(
            _LIBRARY_BOOTSTRAP_RECEIPT_DOMAIN
            + canonical_json(material).encode("utf-8")
        ).hexdigest()
        if not (
            response == expected_response
            and hmac.compare_digest(
                hashlib.sha256(response_json.encode("utf-8")).hexdigest(),
                str(receipt["response_sha256"]),
            )
            and hmac.compare_digest(
                expected_receipt_sha256,
                str(receipt["receipt_sha256"]),
            )
        ):
            raise _library_scan_integrity_error()

    @staticmethod
    def _library_scan_checkpoint(
        raw: object,
        *,
        root_permission_fingerprint: str,
        allow_empty: bool,
    ) -> tuple[dict[str, int], str | None]:
        checkpoint = _decode_canonical_object(
            raw,
            limits=_LIBRARY_SCAN_CHECKPOINT_LIMITS,
        )
        if not checkpoint:
            if not allow_empty:
                raise _library_scan_integrity_error()
            return (
                {
                    "processed": 0,
                    "imported": 0,
                    "skipped": 0,
                    "rejected": 0,
                    "child_jobs": 0,
                    "batches": 0,
                },
                None,
            )
        if set(checkpoint) != _LIBRARY_SCAN_CHECKPOINT_KEYS:
            raise _library_scan_integrity_error()
        cursor = checkpoint["last_relative_path"]
        if cursor is not None:
            if type(cursor) is not str or "\0" in cursor:
                raise _library_scan_integrity_error()
            try:
                cursor_bytes = cursor.encode("utf-8", errors="strict")
                normalized_cursor = PurePosixPath(cursor)
            except (UnicodeError, ValueError) as exc:
                raise _library_scan_integrity_error() from exc
            if not (
                1 <= len(cursor_bytes) <= 4_096
                and not normalized_cursor.is_absolute()
                and bool(normalized_cursor.parts)
                and all(part not in {"", ".", ".."} for part in normalized_cursor.parts)
                and normalized_cursor.as_posix() == cursor
            ):
                raise _library_scan_integrity_error()
        count_fields = {
            "processed": "processed_count",
            "imported": "imported_count",
            "skipped": "skipped_count",
            "rejected": "rejected_count",
            "child_jobs": "child_job_count",
            "batches": "batch_count",
        }
        if not (
            checkpoint["object"] == "memolens.library_scan_checkpoint"
            and checkpoint["schema_version"] == "1"
            and checkpoint["root_permission_fingerprint"]
            == root_permission_fingerprint
            and all(
                type(checkpoint[source]) is int
                and 0 <= checkpoint[source] <= _MAX_SAFE_INTEGER
                for source in count_fields.values()
            )
        ):
            raise _library_scan_integrity_error()
        counts = {
            public: int(checkpoint[source])
            for public, source in count_fields.items()
        }
        if not (
            counts["processed"] >= counts["imported"] + counts["skipped"]
            and counts["processed"] >= counts["rejected"]
            and counts["processed"] >= counts["child_jobs"]
            and counts["batches"] <= counts["processed"]
            and (counts["processed"] == 0) == (cursor is None)
            and (counts["processed"] == 0) == (counts["batches"] == 0)
        ):
            raise _library_scan_integrity_error()
        return counts, cursor

    @staticmethod
    def _library_scan_error(raw: object) -> dict[str, Any] | None:
        if raw is None:
            return None
        error = _decode_canonical_object(raw, limits=_LIBRARY_SCAN_ERROR_LIMITS)
        code = error.get("code")
        retryable = error.get("retryable")
        if not (
            set(error) == _LIBRARY_SCAN_ERROR_KEYS
            and error.get("object") == "memolens.library_scan_error"
            and error.get("schema_version") == "1"
            and type(code) is str
            and code in _LIBRARY_SCAN_ERROR_RETRYABLE
            and type(retryable) is bool
            and retryable is _LIBRARY_SCAN_ERROR_RETRYABLE[code]
            and type(error.get("stage")) is str
            and error["stage"] in {"awaiting_scan_worker", "scanning"}
            and type(error.get("detail_sha256")) is str
            and _SHA256.fullmatch(error["detail_sha256"])
        ):
            raise _library_scan_integrity_error()
        return {
            "code": code,
            "stage": error["stage"],
            "retryable": retryable,
        }

    @classmethod
    def _project_library_scan_job(
        cls,
        job: sqlite3.Row,
        *,
        root_permission_fingerprint: str,
    ) -> dict[str, Any]:
        status = job["status"]
        stage = job["stage"]
        progress = job["progress"]
        attempt = job["attempt"]
        cancel_requested = job["cancel_requested"]
        if not (
            type(status) is str
            and status in _LIBRARY_SCAN_STATUS_STAGES
            and type(stage) is str
            and stage in _LIBRARY_SCAN_STATUS_STAGES[status]
            and type(progress) in {int, float}
            and not isinstance(progress, bool)
            and math.isfinite(float(progress))
            and 0 <= float(progress) <= 1
            and (status == "succeeded") == (float(progress) == 1)
            and type(attempt) is int
            and 1 <= attempt <= _MAX_SAFE_INTEGER
            and type(cancel_requested) is int
            and cancel_requested in {0, 1}
        ):
            raise _library_scan_integrity_error()
        cancel = bool(cancel_requested)
        if (
            status in {"queued", "running", "interrupted", "succeeded"}
            and cancel
        ) or (status in {"cancelling", "cancelled"} and not cancel):
            raise _library_scan_integrity_error()
        timestamps: dict[str, datetime | None] = {}
        for key in ("created_at", "started_at", "heartbeat_at", "finished_at"):
            value = job[key]
            if value is not None and (
                type(value) is not str or not _UTC_TIMESTAMP.fullmatch(value)
            ):
                raise _library_scan_integrity_error()
            try:
                parsed = datetime.fromisoformat(value) if value is not None else None
            except ValueError as exc:
                raise _library_scan_integrity_error() from exc
            if parsed is not None and (
                parsed.tzinfo != timezone.utc or parsed.isoformat() != value
            ):
                raise _library_scan_integrity_error()
            timestamps[key] = parsed
        if job["created_at"] is None:
            raise _library_scan_integrity_error()
        presence = {
            key: job[key] is not None
            for key in ("started_at", "heartbeat_at", "finished_at")
        }
        if status == "queued" and any(presence.values()):
            raise _library_scan_integrity_error()
        if status in {"running", "cancelling"} and presence != {
            "started_at": True,
            "heartbeat_at": True,
            "finished_at": False,
        }:
            raise _library_scan_integrity_error()
        if status in {"cancelled", "failed"} and not (
            presence["heartbeat_at"] and presence["finished_at"]
        ):
            raise _library_scan_integrity_error()
        if status in {"interrupted", "succeeded"} and not all(
            presence.values()
        ):
            raise _library_scan_integrity_error()
        ordered = [
            timestamps[key]
            for key in ("created_at", "started_at", "heartbeat_at", "finished_at")
            if timestamps[key] is not None
        ]
        if any(later < earlier for earlier, later in zip(ordered, ordered[1:])):
            raise _library_scan_integrity_error()

        counts, cursor = cls._library_scan_checkpoint(
            job["checkpoint_json"],
            root_permission_fingerprint=root_permission_fingerprint,
            allow_empty=status == "queued",
        )
        error = cls._library_scan_error(job["error_json"])
        if status in {"queued", "running", "cancelling", "succeeded"}:
            if error is not None:
                raise _library_scan_integrity_error()
        elif status == "interrupted":
            if error is None or error["code"] != "worker_interrupted":
                raise _library_scan_integrity_error()
        elif status == "cancelled":
            if error is None or error["code"] != "user_cancelled":
                raise _library_scan_integrity_error()
        elif error is None or error["code"] in {
            "worker_interrupted",
            "user_cancelled",
        }:
            raise _library_scan_integrity_error()
        if error is not None:
            expected_error_stage = (
                "awaiting_scan_worker"
                if job["started_at"] is None and status in {"cancelled", "failed"}
                else "scanning"
            )
            if error["stage"] != expected_error_stage:
                raise _library_scan_integrity_error()

        no_supported_media = stage == "no_supported_media"
        if no_supported_media and (
            cursor is not None or any(counts.values())
        ):
            raise _library_scan_integrity_error()
        if (
            status == "succeeded"
            and not no_supported_media
            and cursor is None
            and not any(counts.values())
        ):
            raise _library_scan_integrity_error()
        resumable = bool(
            status in {"interrupted", "cancelled", "failed"}
            and error is not None
            and error["retryable"]
        )
        public_error = (
            None
            if error is None
            else {"code": error["code"], "retryable": error["retryable"]}
        )
        return {
            "object": "memolens.library_scan_job",
            "schema_version": "1",
            "id": str(job["id"]),
            "kind": "library_scan",
            "status": status,
            "stage": stage,
            "progress": float(progress),
            "attempt": attempt,
            "cancel_requested": cancel,
            "resumable": resumable,
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "heartbeat_at": job["heartbeat_at"],
            "finished_at": job["finished_at"],
            "counts": counts,
            "no_supported_media": no_supported_media,
            "eta_seconds": None,
            "error": public_error,
        }

    def search(self, query: str, limit: int) -> dict[str, Any]:
        return self.photos.search(
            query,
            limit,
            require_media_provenance=True,
            require_canonical_authority=True,
            # The photos-only tool retains its explicit, traversal-checked
            # local inspection path, but only after exact V14 admission. Paths
            # are never identity or authority and remain absent from mixed/Wiki.
            include_paths=True,
        )

    def mixed_image_search(self, query: str, limit: int) -> dict[str, Any]:
        return self.photos.search(
            query,
            limit,
            require_media_provenance=True,
            require_canonical_authority=True,
            include_paths=False,
        )

    def mixed_material_search(
        self,
        query: str,
        limit: int,
        *,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Validate and rank all raw material within one private DB snapshot."""

        try:
            policy = parse_usage_filters(
                None if filters is None else dict(filters)
            )
        except ResidualAuthorityError as exc:
            raise MemoLensError(str(exc), code=exc.code) from exc

        with closing(self.database.connection()) as connection:
            try:
                validate_exact_managed_schema_manifest(connection)
            except (MemoLensError, sqlite3.Error) as exc:
                raise MemoLensError(
                    "Canonical Export derivative authority is unavailable in this index.",
                    code="derivative_authority_unavailable",
                ) from exc

            # Residual IDs are derived read projections, never persisted
            # segment identities.  Keep this census outside branch fallback.
            self.media.validate_no_persisted_residual_rows(connection)

            # This authority check deliberately sits outside branch_errors.
            # A corrupt five-table ledger cannot become an apparent empty
            # derivative set through photo/video compatibility fallback.
            derivative = project_validated_derivative_facts(connection)
            excluded_sha256 = derivative["video_sha256"]
            assert isinstance(excluded_sha256, frozenset)
            raw_occurrences = derivative.get("usage_occurrences")
            if type(raw_occurrences) is not list:
                raise MemoLensError(
                    "Canonical Usage authority did not return a closed occurrence stream.",
                    code="usage_authority_integrity_error",
                )
            if any(not isinstance(item, dict) for item in raw_occurrences):
                raise MemoLensError(
                    "Canonical Usage authority returned a malformed occurrence.",
                    code="usage_authority_integrity_error",
                )
            try:
                occurrences, current_usage_revision = current_candidate_usage_facts(
                    connection,
                    raw_occurrences,
                )
            except ResidualAuthorityError as exc:
                raise MemoLensError(str(exc), code=exc.code) from exc
            usage_revision = current_usage_revision if policy.requested else None
            used_image_ids = frozenset(
                str(item["asset_id"])
                for item in occurrences
                if item.get("media_kind") == "image"
                and isinstance(item.get("asset_id"), str)
            )
            excluded_image_ids = (
                used_image_ids
                if policy.requested
                and (policy.unused_only or not policy.allow_reuse)
                else frozenset()
            )

            def video_candidate(raw: dict[str, Any]) -> dict[str, object]:
                return {
                    "result_type": "video_segment",
                    "asset_id": raw.get("asset_id"),
                    "asset_duration_ms": raw.get("asset_duration_ms"),
                    "start_ms": raw.get("start_ms"),
                    "end_ms": raw.get("end_ms"),
                }

            def admit_video(raw: dict[str, Any]) -> bool:
                try:
                    return candidate_admitted(
                        video_candidate(raw),
                        occurrences,
                        policy,
                    )
                except ResidualAuthorityError as exc:
                    raise MemoLensError(
                        str(exc),
                        code="usage_authority_integrity_error",
                    ) from exc

            branches: list[tuple[str, dict[str, Any]]] = []
            branch_errors: list[dict[str, str]] = []
            for kind, searcher in (
                (
                    "image",
                    lambda: self.photos.search_in_connection(
                        connection,
                        query,
                        limit,
                        require_media_provenance=True,
                        require_canonical_authority=True,
                        include_paths=False,
                        excluded_asset_sha256=excluded_sha256,
                        excluded_asset_ids=excluded_image_ids,
                    ),
                ),
                (
                    "video_segment",
                    lambda: self.media.video_search_in_connection(
                        connection,
                        query,
                        limit,
                        excluded_asset_sha256=excluded_sha256,
                        candidate_predicate=(
                            admit_video if policy.requested else None
                        ),
                    ),
                ),
            ):
                try:
                    branches.append((kind, searcher()))
                except MemoLensError as exc:
                    if exc.code == "usage_authority_integrity_error":
                        raise
                    branch_errors.append(
                        {
                            "result_type": kind,
                            "code": exc.code,
                            "message": str(exc),
                        }
                    )
            if not branches:
                raise MemoLensError(
                    "Neither the photo index nor the current video-segment index could be searched.",
                    code="search_unavailable",
                )

            fused: list[tuple[float, int, int, str, dict[str, Any]]] = []
            kind_order = {"image": 0, "video_segment": 1}
            excluded_candidate_count = 0
            for kind, payload in branches:
                excluded_candidate_count += int(
                    payload.pop("excluded_derivative_count", 0) or 0
                )
                raw_results = payload.get("results")
                if not isinstance(raw_results, list):
                    continue
                normalized: list[dict[str, object]] = []
                for branch_rank, raw in enumerate(raw_results, start=1):
                    if not isinstance(raw, dict):
                        continue
                    item = dict(raw)
                    item["result_type"] = kind
                    item["media_kind"] = (
                        "image" if kind == "image" else "video"
                    )
                    item["_branch_rank"] = branch_rank
                    normalized.append(item)
                try:
                    projected = annotate_and_materialize(
                        normalized,
                        occurrences,
                        usage_revision=(usage_revision or canonical_sha256([])),
                        policy=policy,
                    )
                except ResidualAuthorityError as exc:
                    raise MemoLensError(
                        str(exc),
                        code="usage_authority_integrity_error",
                    ) from exc
                for projected_item in projected:
                    rank = int(projected_item.get("_branch_rank") or 1)
                    fused_score = 1.0 / (60.0 + rank)
                    item = dict(projected_item)
                    item["rank_score"] = round(fused_score, 8)
                    stable_id = str(
                        item.get("segment_id")
                        or item.get("asset_id")
                        or item.get("id")
                        or ""
                    )
                    fused.append(
                        (
                            fused_score,
                            usage_preference(item) if policy.prefer_unused else 0,
                            kind_order[kind],
                            stable_id,
                            item,
                        )
                    )
            fused.sort(
                key=lambda value: (-value[0], value[1], value[2], value[3])
            )
            results = [
                item for _score, _usage, _kind, _id, item in fused[:limit]
            ]
            for item in results:
                item.pop("_branch_rank", None)
                item.pop("source_binding_sha256", None)
                item.pop("input_asset_sha256", None)
            facts = derivative["derivative_facts"]
            assert isinstance(facts, list)
            revision = str(derivative["derivative_revision"])
            search_revision = canonical_sha256(
                {
                    "contract": "memolens.plugin_mixed_search_revision/v1",
                    "query": query,
                    "limit": limit,
                    "filters": dict(filters or {}),
                    "result_types": sorted(kind for kind, _payload in branches),
                    "derivative_revision": revision,
                    **(
                        {"usage_revision": usage_revision}
                        if usage_revision is not None
                        else {}
                    ),
                    "candidate_bindings": [
                        {
                            key: item.get(key)
                            for key in (
                                "result_type",
                                "id",
                                "asset_id",
                                "asset_source_id",
                                "asset_sha256",
                                "segment_id",
                                "start_ms",
                                "end_ms",
                                "analysis_run_id",
                                "analysis_revision",
                                "parent_segment_id",
                                "residual_binding",
                                "usage",
                            )
                        }
                        for item in results
                    ],
                }
            )
            response = {
                "object": "memolens.mixed_search",
                "schema_version": "1",
                "status": "completed",
                "source": "read_only_federated",
                "ranking": "reciprocal_rank_fusion",
                "query": query,
                "result_count": len(results),
                "results": results,
                "searched_result_types": [kind for kind, _payload in branches],
                "branch_errors": branch_errors,
                "search_revision": search_revision,
                "derivative_revision": revision,
                "derivative_exclusion": {
                    "authority": "cold_audited_canonical_export",
                    "identity": "full_assets_sha256",
                    "revision": revision,
                    "validated_fact_count": len(facts),
                    "validated_sha256_count": len(excluded_sha256),
                    "excluded_candidate_count": excluded_candidate_count,
                    "path_or_name_heuristics": False,
                    "bypass_available": False,
                },
                "safety": safety_summary(),
            }
            if usage_revision is not None:
                response["usage_revision"] = usage_revision
                response["usage_policy"] = {
                    "unused_only": policy.unused_only,
                    "prefer_unused": policy.prefer_unused,
                    "allow_reuse": policy.allow_reuse,
                    "used_in": (
                        None
                        if policy.used_in is None
                        else {
                            "project_id": policy.used_in.project_id,
                            "export_revision": policy.used_in.export_revision,
                        }
                    ),
                    "residual_of": policy.residual_of,
                }
            return response

    def media_list(
        self, *, kinds: list[str], limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.media.list(kinds=kinds, limit=limit, cursor=cursor)

    def wiki_media_list(
        self, *, kinds: list[str], limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.media.list(
            kinds=kinds,
            limit=limit,
            cursor=cursor,
            active_root_only=True,
        )

    def media_get(self, asset_id: str) -> dict[str, Any]:
        return self.media.get(asset_id)

    def wiki_asset_get(self, asset_id: str) -> dict[str, Any]:
        return self.media.get(
            asset_id,
            include_image_observation=True,
            require_verified_video_head=True,
            active_root_only=True,
            include_unavailable_video_sources=True,
        )

    def segment_get(self, segment_id: str) -> dict[str, Any] | None:
        return self.media.segment_get(segment_id)

    def video_search(self, query: str, limit: int) -> dict[str, Any]:
        return self.media.video_search(query, limit)

    def creator_context(self) -> dict[str, Any]:
        return self.creator.creator_context()

    def inbox_list(
        self,
        *,
        state: str,
        kinds: list[str],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        return self.creator.inbox_list(
            state=state,
            kinds=kinds,
            limit=limit,
            cursor=cursor,
        )

    def timeline_list(
        self, *, project_id: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.timelines.list(
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )

    def timeline_get(
        self, timeline_id: str, revision: int | None
    ) -> dict[str, Any]:
        return self.timelines.get(timeline_id, revision)

    def project_list(
        self, *, status: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.project_presenter.project_list(
            self.projects.list_raw(status=status, limit=limit, cursor=cursor)
        )

    def project_open(self, project_id: str) -> dict[str, Any]:
        # Project facts and the canonical Blueprint head must come from one
        # private snapshot.  Otherwise a concurrent desktop commit could make
        # the Resume capsule internally inconsistent.
        with closing(self.database.connection()) as connection:
            raw = self.projects.open_in_connection(connection, project_id)
            raw["persisted_blueprint"] = (
                self.persisted_blueprints.get_in_connection(
                    connection,
                    project_id,
                )
                if raw.get("project") is not None
                else None
            )
        return self.project_presenter.project_resume(raw)

    def project_history(self, project_id: str, limit: int) -> dict[str, Any]:
        return self.project_presenter.project_history(
            self.projects.history_raw(project_id, limit=limit)
        )

    def blueprint_shadow(self, project_id: str) -> dict[str, Any]:
        return self.blueprints.shadow(project_id)

    def blueprint_validate(self, candidate: Any) -> dict[str, Any]:
        return self.blueprints.validate(candidate)

    def blueprint_get(
        self, project_id: str, revision: int | None = None
    ) -> dict[str, Any]:
        return self.persisted_blueprints.get(project_id, revision)

    def blueprint_history(self, project_id: str, limit: int = 50) -> dict[str, Any]:
        return self.persisted_blueprints.history(project_id, limit)
