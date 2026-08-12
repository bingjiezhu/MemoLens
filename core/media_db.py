from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 2
BASELINE_CHECKSUM = hashlib.sha256(b"memolens-image-index-v1").hexdigest()


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        checksum TEXT NOT NULL, applied_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS database_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        database_uuid TEXT NOT NULL UNIQUE,
        schema_version INTEGER NOT NULL CHECK(schema_version>=2),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS library_roots (
        id TEXT PRIMARY KEY, canonical_path TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL, permission_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','unavailable','revoked')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('image','video','audio')),
        sha256 TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL,
        file_size INTEGER NOT NULL CHECK(file_size>=0),
        duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>0),
        width INTEGER CHECK(width IS NULL OR width>0),
        height INTEGER CHECK(height IS NULL OR height>0),
        rotation_degrees INTEGER, codec_json TEXT NOT NULL DEFAULT '{}',
        captured_at TEXT,
        probe_status TEXT NOT NULL CHECK(probe_status IN ('pending','ready','unsupported','failed')),
        error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS asset_sources (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
        library_root_id TEXT NOT NULL REFERENCES library_roots(id) ON DELETE RESTRICT,
        relative_path TEXT NOT NULL, display_filename TEXT NOT NULL,
        observed_size INTEGER NOT NULL CHECK(observed_size>=0),
        observed_mtime_ns INTEGER, source_file_id TEXT,
        availability TEXT NOT NULL CHECK(availability IN ('available','missing','changed','revoked')),
        is_preferred INTEGER NOT NULL DEFAULT 0 CHECK(is_preferred IN (0,1)),
        last_verified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(library_root_id,relative_path))""",
    """CREATE TABLE IF NOT EXISTS analysis_runs (
        id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL CHECK(revision>=1),
        run_kind TEXT NOT NULL CHECK(run_kind IN ('initial','reanalyze','refinement')),
        parent_run_id TEXT REFERENCES analysis_runs(id) ON DELETE RESTRICT,
        analysis_profile_id TEXT NOT NULL, analysis_profile_json TEXT NOT NULL,
        input_asset_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','partial','failed','cancelling','cancelled','interrupted')),
        transcript_status TEXT NOT NULL, visual_status TEXT NOT NULL,
        error_json TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
        UNIQUE(asset_id,revision), UNIQUE(asset_id,id))""",
    """CREATE TABLE IF NOT EXISTS asset_analysis_heads (
        asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
        analysis_run_id TEXT NOT NULL, updated_at TEXT NOT NULL,
        FOREIGN KEY(asset_id,analysis_run_id)
          REFERENCES analysis_runs(asset_id,id) ON DELETE RESTRICT)""",
    """CREATE TABLE IF NOT EXISTS video_segments (
        id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, analysis_run_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        start_ms INTEGER NOT NULL CHECK(start_ms>=0),
        end_ms INTEGER NOT NULL CHECK(end_ms>start_ms),
        boundary_reason TEXT NOT NULL, summary TEXT,
        semantic_json TEXT NOT NULL DEFAULT '{}', visible_text TEXT,
        combined_text TEXT NOT NULL DEFAULT '', text_embedding_model TEXT,
        text_embedding BLOB, visual_status TEXT NOT NULL,
        transcript_status TEXT NOT NULL,
        confidence REAL CHECK(confidence IS NULL OR (confidence>=0 AND confidence<=1)),
        created_at TEXT NOT NULL,
        FOREIGN KEY(asset_id,analysis_run_id)
          REFERENCES analysis_runs(asset_id,id) ON DELETE CASCADE,
        UNIQUE(analysis_run_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS keyframes (
        id TEXT PRIMARY KEY,
        segment_id TEXT NOT NULL REFERENCES video_segments(id) ON DELETE CASCADE,
        timestamp_ms INTEGER NOT NULL CHECK(timestamp_ms>=0), cache_key TEXT NOT NULL,
        sha256 TEXT NOT NULL, width INTEGER NOT NULL CHECK(width>0),
        height INTEGER NOT NULL CHECK(height>0), selection_reason TEXT NOT NULL,
        clarity_score REAL, novelty_score REAL,
        is_representative INTEGER NOT NULL CHECK(is_representative IN (0,1)),
        created_at TEXT NOT NULL, UNIQUE(segment_id,timestamp_ms,sha256))""",
    """CREATE TABLE IF NOT EXISTS transcript_segments (
        id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, analysis_run_id TEXT NOT NULL,
        start_ms INTEGER NOT NULL CHECK(start_ms>=0),
        end_ms INTEGER NOT NULL CHECK(end_ms>start_ms),
        text TEXT NOT NULL CHECK(length(trim(text))>0), language TEXT,
        speaker_label TEXT,
        confidence REAL CHECK(confidence IS NULL OR (confidence>=0 AND confidence<=1)),
        source TEXT NOT NULL, provider TEXT NOT NULL, model TEXT,
        payload_manifest_id TEXT, created_at TEXT NOT NULL,
        FOREIGN KEY(asset_id,analysis_run_id)
          REFERENCES analysis_runs(asset_id,id) ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS creative_projects (
        id TEXT PRIMARY KEY, title TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('draft','active','archived')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS creative_briefs (
        project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision>=1), brief_json TEXT NOT NULL,
        content_sha256 TEXT NOT NULL, provenance_json TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(project_id,revision))""",
    """CREATE TABLE IF NOT EXISTS timelines (
        id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision>=1), parent_revision INTEGER,
        brief_revision INTEGER NOT NULL, schema_version TEXT NOT NULL,
        timeline_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        validation_status TEXT NOT NULL CHECK(validation_status IN ('valid','invalid')),
        validation_errors_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
        PRIMARY KEY(id,revision),
        FOREIGN KEY(project_id,brief_revision)
          REFERENCES creative_briefs(project_id,revision) ON DELETE RESTRICT,
        FOREIGN KEY(id,parent_revision)
          REFERENCES timelines(id,revision) ON DELETE RESTRICT)""",
    """CREATE TABLE IF NOT EXISTS output_roots (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('app_preview','user_export')),
        canonical_path TEXT NOT NULL UNIQUE, permission_fingerprint TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','unavailable','revoked')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS export_grants (
        id TEXT PRIMARY KEY,
        output_root_id TEXT NOT NULL REFERENCES output_roots(id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
        filename TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE,
        allow_overwrite INTEGER NOT NULL DEFAULT 0 CHECK(allow_overwrite IN (0,1)),
        single_use INTEGER NOT NULL DEFAULT 1 CHECK(single_use IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('active','consumed','expired','revoked')),
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS media_jobs (
        id TEXT PRIMARY KEY, database_uuid TEXT NOT NULL,
        kind TEXT NOT NULL, asset_id TEXT REFERENCES assets(id) ON DELETE RESTRICT,
        analysis_run_id TEXT REFERENCES analysis_runs(id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','partial','failed','cancelling','cancelled','interrupted','blocked_source_unavailable')),
        stage TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0 CHECK(progress>=0 AND progress<=1),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt>=1),
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        checkpoint_json TEXT NOT NULL DEFAULT '{}', error_json TEXT,
        created_at TEXT NOT NULL, started_at TEXT, heartbeat_at TEXT, finished_at TEXT,
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT)""",
    """CREATE TABLE IF NOT EXISTS render_jobs (
        id TEXT PRIMARY KEY, database_uuid TEXT NOT NULL,
        timeline_id TEXT NOT NULL, timeline_revision INTEGER NOT NULL,
        profile TEXT NOT NULL CHECK(profile IN ('preview-low','export-1080p')),
        output_root_id TEXT NOT NULL REFERENCES output_roots(id) ON DELETE RESTRICT,
        export_grant_id TEXT REFERENCES export_grants(id) ON DELETE RESTRICT,
        output_relative_path TEXT NOT NULL, timeline_content_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelling','cancelled','interrupted','blocked_source_unavailable')),
        stage TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0 CHECK(progress>=0 AND progress<=1),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt>=1),
        ffmpeg_command_json TEXT, ffmpeg_version TEXT, output_sha256 TEXT,
        size_bytes INTEGER, duration_ms INTEGER, stderr_tail TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        error_json TEXT, created_at TEXT NOT NULL, started_at TEXT,
        heartbeat_at TEXT, finished_at TEXT,
        FOREIGN KEY(timeline_id,timeline_revision)
          REFERENCES timelines(id,revision) ON DELETE RESTRICT,
        FOREIGN KEY(database_uuid) REFERENCES database_meta(database_uuid) ON DELETE RESTRICT)""",
    """CREATE TABLE IF NOT EXISTS idempotency_records (
        scope TEXT NOT NULL, key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('in_progress','completed','failed')),
        resource_type TEXT, resource_id TEXT, response_status INTEGER,
        response_json TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        PRIMARY KEY(scope,key))""",
    """CREATE TABLE IF NOT EXISTS provider_opt_in_grants (
        id TEXT PRIMARY KEY, provider TEXT NOT NULL,
        capability TEXT NOT NULL CHECK(capability IN ('video_vlm','transcription','codex_visual_inspection')),
        payload_classes_json TEXT NOT NULL, asset_scope_json TEXT NOT NULL,
        token_sha256 TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK(status IN ('active','consumed','expired','revoked')),
        single_use INTEGER NOT NULL DEFAULT 1 CHECK(single_use IN (0,1)),
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS provider_payload_manifests (
        id TEXT PRIMARY KEY, job_id TEXT,
        grant_id TEXT NOT NULL REFERENCES provider_opt_in_grants(id) ON DELETE RESTRICT,
        provider TEXT NOT NULL, model TEXT, capability TEXT NOT NULL,
        payload_classes_json TEXT NOT NULL, asset_ids_json TEXT NOT NULL,
        time_ranges_json TEXT NOT NULL, payload_sha256_json TEXT NOT NULL,
        planned_bytes INTEGER, bytes_sent INTEGER,
        outcome TEXT NOT NULL CHECK(outcome IN ('planned','sent','failed_before_send','failed_after_send','cancelled')),
        retention_policy TEXT, retention_evidence TEXT, user_opt_in_at TEXT NOT NULL,
        created_at TEXT NOT NULL, completed_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_assets_kind_probe ON assets(kind,probe_status)",
    "CREATE INDEX IF NOT EXISTS idx_asset_sources_asset_availability ON asset_sources(asset_id,availability)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_asset_sources_one_preferred ON asset_sources(asset_id) WHERE is_preferred=1",
    "CREATE INDEX IF NOT EXISTS idx_video_segments_run_time ON video_segments(analysis_run_id,start_ms,end_ms)",
    "CREATE INDEX IF NOT EXISTS idx_keyframes_segment ON keyframes(segment_id,is_representative,timestamp_ms)",
    "CREATE INDEX IF NOT EXISTS idx_transcript_run_time ON transcript_segments(analysis_run_id,start_ms,end_ms)",
    "CREATE INDEX IF NOT EXISTS idx_timelines_project_revision ON timelines(project_id,revision DESC)",
    "CREATE INDEX IF NOT EXISTS idx_media_jobs_status_created ON media_jobs(status,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_render_jobs_status_created ON render_jobs(status,created_at)",
    """CREATE VIEW IF NOT EXISTS current_video_segments AS
       SELECT vs.*, ar.revision AS analysis_revision
       FROM video_segments vs
       JOIN asset_analysis_heads ah
         ON ah.asset_id=vs.asset_id AND ah.analysis_run_id=vs.analysis_run_id
       JOIN analysis_runs ar ON ar.id=vs.analysis_run_id AND ar.status='succeeded'""",
)

V2_CHECKSUM = hashlib.sha256(
    "\n".join(" ".join(statement.split()) for statement in SCHEMA_STATEMENTS).encode()
).hexdigest()


class MediaMigrationError(RuntimeError):
    pass


class IdempotencyConflictError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _loads(value: object, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _permission_fingerprint(path: Path) -> str:
    stat_result = path.stat()
    material = f"{path}\0{stat_result.st_dev}\0{stat_result.st_ino}\0{stat_result.st_mode}"
    return hashlib.sha256(material.encode()).hexdigest()


def permission_fingerprint(path: Path) -> str:
    return _permission_fingerprint(path)


class MediaRepository:
    """Versioned additive media store. Each instance is permanently bound to one DB path."""

    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser().resolve()
        # Keep app-managed output roots open for this repository lifetime.
        # Holding the original directory inode prevents Linux from immediately
        # reusing it after a remove-and-recreate at the same path, closing a
        # platform-specific identity gap that a persisted dev/inode fingerprint
        # alone cannot detect.
        self._output_root_descriptors: dict[str, int] = {}
        self._artifact_cache: dict[str, tuple[int, int, int, int, int, str]] = {}
        self._artifact_cache_lock = threading.Lock()
        self._source_cache: dict[str, tuple[int, int, int, int, int, str]] = {}
        self._source_cache_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("PRAGMA synchronous=NORMAL")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        if str(journal).casefold() != "wal" or int(foreign_keys) != 1:
            connection.close()
            raise MediaMigrationError("SQLite must support WAL and foreign keys.")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self, default_library_root: Path) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(immediate=True) as connection:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            self._migration(connection, 1, "image_index_baseline", BASELINE_CHECKSUM)
            self._migration(connection, 2, "video_creative_workbench", V2_CHECKSUM)
            now = utc_now_iso()
            connection.execute(
                """INSERT INTO database_meta(singleton,database_uuid,schema_version,created_at,updated_at)
                   VALUES(1,?,2,?,?) ON CONFLICT(singleton) DO UPDATE SET
                   schema_version=2,updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), now, now),
            )
            root_id = self._upsert_library_root(connection, default_library_root.resolve())
            self._backfill_images(connection, root_id, default_library_root.resolve())

    @staticmethod
    def _migration(connection: sqlite3.Connection, version: int, name: str, checksum: str) -> None:
        row = connection.execute("SELECT name,checksum FROM schema_migrations WHERE version=?", (version,)).fetchone()
        if row:
            if row["name"] != name or row["checksum"] != checksum:
                raise MediaMigrationError(f"migration_checksum_mismatch for version {version}")
            return
        connection.execute(
            "INSERT INTO schema_migrations VALUES(?,?,?,?)",
            (version, name, checksum, utc_now_iso()),
        )

    @property
    def database_uuid(self) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT database_uuid FROM database_meta WHERE singleton=1").fetchone()
        if not row:
            raise MediaMigrationError("database_meta is missing.")
        return str(row["database_uuid"])

    @staticmethod
    def _root_id(path: Path) -> str:
        return f"root_{hashlib.sha256(str(path).encode()).hexdigest()[:24]}"

    @classmethod
    def _upsert_library_root(cls, connection: sqlite3.Connection, path: Path) -> str:
        if not path.is_dir():
            raise ValueError("Library root must be an existing directory.")
        now = utc_now_iso()
        root_id = cls._root_id(path)
        connection.execute(
            """INSERT INTO library_roots(
                 id,canonical_path,label,permission_fingerprint,status,created_at,updated_at)
               VALUES(?,?,?,?, 'active',?,?)
               ON CONFLICT(canonical_path) DO UPDATE SET
                 label=excluded.label,permission_fingerprint=excluded.permission_fingerprint,
                 status='active',updated_at=excluded.updated_at""",
            (root_id, str(path), path.name or "Library", _permission_fingerprint(path), now, now),
        )
        row = connection.execute("SELECT id FROM library_roots WHERE canonical_path=?", (str(path),)).fetchone()
        assert row
        return str(row["id"])

    def register_library_root(self, path: Path) -> dict[str, object]:
        canonical = path.expanduser().resolve(strict=True)
        with self.transaction(immediate=True) as connection:
            root_id = self._upsert_library_root(connection, canonical)
        return {"id": root_id, "label": canonical.name, "status": "active"}

    def library_root(self, root_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM library_roots WHERE id=?", (root_id,)).fetchone()
        return dict(row) if row else None

    def validate_library_root(self, root_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM library_roots WHERE id=?", (root_id,)).fetchone()
        if not row or row["status"] != "active":
            raise ValueError("Approved library root is unavailable.")
        stored = Path(str(row["canonical_path"]))
        current = Path(stored.anchor)
        for part in stored.parts[1:]:
            current /= part
            if current.is_symlink():
                self._set_library_root_status(root_id, "unavailable")
                raise ValueError("Approved library root identity changed.")
        try:
            resolved = stored.resolve(strict=True)
        except OSError as exc:
            self._set_library_root_status(root_id, "unavailable")
            raise ValueError("Approved library root is unavailable.") from exc
        if (
            str(resolved) != str(stored)
            or not resolved.is_dir()
            or _permission_fingerprint(resolved) != row["permission_fingerprint"]
        ):
            self._set_library_root_status(root_id, "unavailable")
            raise ValueError("Approved library root identity changed.")
        return resolved

    def _set_library_root_status(self, root_id: str, status: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE library_roots SET status=?,updated_at=? WHERE id=?",
                (status, utc_now_iso(), root_id),
            )

    def library_roots(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,label,status,created_at,updated_at FROM library_roots ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def source_id(root_id: str, relative_path: str) -> str:
        value = f"{root_id}\0{relative_path}"
        return f"src_{hashlib.sha256(value.encode()).hexdigest()[:24]}"

    @staticmethod
    def _backfill_images(connection: sqlite3.Connection, root_id: str, root: Path) -> None:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_index'").fetchone():
            return
        rows = connection.execute(
            """SELECT id,sha256,filename,relative_path,mime_type,file_size,width,height,
                      taken_at,created_at,updated_at FROM image_index"""
        ).fetchall()
        for row in rows:
            now = utc_now_iso()
            connection.execute(
                """INSERT INTO assets(id,kind,sha256,mime_type,file_size,width,height,
                         rotation_degrees,codec_json,captured_at,probe_status,created_at,updated_at)
                   VALUES(?,'image',?,?,?,?,?,0,'{}',?,'ready',?,?)
                   ON CONFLICT(sha256) DO NOTHING""",
                (
                    row["id"],
                    row["sha256"],
                    row["mime_type"],
                    row["file_size"],
                    row["width"],
                    row["height"],
                    row["taken_at"],
                    row["created_at"] or now,
                    row["updated_at"] or now,
                ),
            )
            asset = connection.execute("SELECT id FROM assets WHERE sha256=?", (row["sha256"],)).fetchone()
            if not asset:
                continue
            source_path = root / str(row["relative_path"])
            available = source_path.is_file() and not source_path.is_symlink()
            stat_result = source_path.stat() if available else None
            preferred = not connection.execute(
                "SELECT 1 FROM asset_sources WHERE asset_id=? AND is_preferred=1", (asset["id"],)
            ).fetchone()
            connection.execute(
                """INSERT INTO asset_sources(id,asset_id,library_root_id,relative_path,
                     display_filename,observed_size,observed_mtime_ns,source_file_id,
                     availability,is_preferred,last_verified_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(library_root_id,relative_path) DO UPDATE SET
                     availability=excluded.availability,last_verified_at=excluded.last_verified_at,
                     updated_at=excluded.updated_at""",
                (
                    MediaRepository.source_id(root_id, str(row["relative_path"])),
                    asset["id"],
                    root_id,
                    row["relative_path"],
                    row["filename"],
                    row["file_size"],
                    stat_result.st_mtime_ns if stat_result else None,
                    str(stat_result.st_ino) if stat_result else None,
                    "available" if available else "missing",
                    int(preferred),
                    now,
                    row["created_at"] or now,
                    row["updated_at"] or now,
                ),
            )

    def upsert_asset_source(
        self,
        *,
        root_id: str,
        relative_path: str,
        filename: str,
        kind: str,
        sha256: str,
        mime_type: str,
        file_size: int,
        mtime_ns: int,
        source_file_id: str | None,
        asset_id_override: str | None = None,
    ) -> dict[str, object]:
        asset_id = asset_id_override or f"asset_{sha256[:24]}"
        source_id = self.source_id(root_id, relative_path)
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO assets(id,kind,sha256,mime_type,file_size,probe_status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(sha256) DO NOTHING""",
                (asset_id, kind, sha256, mime_type, file_size, now, now),
            )
            asset = connection.execute("SELECT id,kind,probe_status FROM assets WHERE sha256=?", (sha256,)).fetchone()
            assert asset
            if asset["kind"] != kind:
                raise ValueError("Identical bytes already belong to a different media kind.")
            asset_id = str(asset["id"])
            prior = connection.execute(
                "SELECT id,asset_id,is_preferred FROM asset_sources WHERE library_root_id=? AND relative_path=?",
                (root_id, relative_path),
            ).fetchone()
            if prior and prior["asset_id"] != asset_id:
                # A path is only a source locator, never content identity. Rebind it atomically
                # to the new hash while preserving the old asset and any other sources/history.
                old_asset_id = str(prior["asset_id"])
                connection.execute(
                    "DELETE FROM asset_sources WHERE id=?",
                    (prior["id"],),
                )
                if bool(prior["is_preferred"]):
                    replacement = connection.execute(
                        """SELECT id FROM asset_sources WHERE asset_id=? AND availability='available'
                           ORDER BY created_at LIMIT 1""",
                        (old_asset_id,),
                    ).fetchone()
                    if replacement:
                        connection.execute(
                            "UPDATE asset_sources SET is_preferred=1,updated_at=? WHERE id=?",
                            (now, replacement["id"]),
                        )
                # The new content identity may already have a preferred source. A
                # path rebind must not violate the one-preferred-source invariant.
                preferred = not connection.execute(
                    "SELECT 1 FROM asset_sources WHERE asset_id=? AND is_preferred=1", (asset_id,)
                ).fetchone()
            else:
                preferred = not connection.execute(
                    "SELECT 1 FROM asset_sources WHERE asset_id=? AND is_preferred=1", (asset_id,)
                ).fetchone()
            connection.execute(
                """INSERT INTO asset_sources(id,asset_id,library_root_id,relative_path,
                     display_filename,observed_size,observed_mtime_ns,source_file_id,
                     availability,is_preferred,last_verified_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?, 'available',?,?,?,?)
                   ON CONFLICT(library_root_id,relative_path) DO UPDATE SET
                     observed_size=excluded.observed_size,observed_mtime_ns=excluded.observed_mtime_ns,
                     source_file_id=excluded.source_file_id,availability='available',
                     last_verified_at=excluded.last_verified_at,updated_at=excluded.updated_at""",
                (
                    source_id,
                    asset_id,
                    root_id,
                    relative_path,
                    filename,
                    file_size,
                    mtime_ns,
                    source_file_id,
                    int(preferred),
                    now,
                    now,
                    now,
                ),
            )
        return {
            "id": asset_id,
            "asset_source_id": source_id,
            "kind": kind,
            "filename": filename,
            "relative_path": relative_path,
            "probe_status": str(asset["probe_status"]),
        }

    @staticmethod
    def _asset_dict(row: sqlite3.Row) -> dict[str, object]:
        value = dict(row)
        value["codec"] = _loads(value.pop("codec_json", None), {})
        return value

    def get_asset(self, asset_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.*,s.id AS asset_source_id,s.display_filename AS filename,
                          s.relative_path,s.availability AS source_availability,
                          s.observed_size,s.observed_mtime_ns,s.library_root_id,
                          r.canonical_path AS root_path
                   FROM assets a LEFT JOIN asset_sources s ON s.asset_id=a.id AND s.is_preferred=1
                   LEFT JOIN library_roots r ON r.id=s.library_root_id WHERE a.id=?""",
                (asset_id,),
            ).fetchone()
        return self._asset_dict(row) if row else None

    def get_asset_source(self, source_id: str) -> dict[str, object] | None:
        """Return one source binding for validation; callers must not serialize root_path."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT s.*,r.canonical_path AS root_path,a.kind,a.sha256,a.mime_type,
                          a.file_size,a.duration_ms,a.width,a.height,a.rotation_degrees,
                          a.codec_json,a.probe_status
                   FROM asset_sources s
                   JOIN assets a ON a.id=s.asset_id
                   JOIN library_roots r ON r.id=s.library_root_id
                   WHERE s.id=?""",
                (source_id,),
            ).fetchone()
        if not row:
            return None
        try:
            self.validate_library_root(str(row["library_root_id"]))
        except ValueError:
            return None
        return self._asset_dict(row)

    def open_library_root_fd(self, root_id: str) -> tuple[Path, int]:
        path = self.validate_library_root(root_id)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        expected = path.stat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_dev != expected.st_dev
            or observed.st_ino != expected.st_ino
        ):
            os.close(descriptor)
            self._set_library_root_status(root_id, "unavailable")
            raise ValueError("Approved library root identity changed.")
        return path, descriptor

    @staticmethod
    def _open_relative_regular(root_fd: int, relative_path: str) -> int:
        relative = Path(relative_path)
        if (
            not relative_path
            or "\x00" in relative_path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("Asset source path is invalid.")
        current_fd = os.dup(root_fd)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            return os.open(relative.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=current_fd)
        finally:
            os.close(current_fd)

    def open_asset_file(self, asset_id: str):
        """Open, content-verify, and return the exact source inode used for streaming."""
        asset = self.get_asset(asset_id)
        if not asset or not asset.get("asset_source_id"):
            return None
        source = self.get_asset_source(str(asset["asset_source_id"]))
        if not source or source.get("availability") != "available":
            return None
        root_fd: int | None = None
        file_fd: int | None = None
        try:
            _, root_fd = self.open_library_root_fd(str(source["library_root_id"]))
            file_fd = self._open_relative_regular(root_fd, str(source["relative_path"]))
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != int(asset["file_size"]):
                raise ValueError("Asset source identity changed.")
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            source_id = str(source["id"])
            with self._source_cache_lock:
                cached = self._source_cache.get(source_id)
            expected_sha = str(asset["sha256"])
            if cached != (*identity, expected_sha):
                digest = hashlib.sha256()
                with os.fdopen(os.dup(file_fd), "rb") as verification:
                    for chunk in iter(lambda: verification.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = os.fstat(file_fd)
                observed = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if observed != identity or digest.hexdigest() != expected_sha:
                    raise ValueError("Asset source content changed.")
                with self._source_cache_lock:
                    self._source_cache[source_id] = (*identity, expected_sha)
            handle = os.fdopen(file_fd, "rb")
            file_fd = None
            return asset, handle, before.st_size
        except (OSError, ValueError):
            self.mark_source_availability(str(source["id"]), "changed")
            if file_fd is not None:
                os.close(file_fd)
            return None
        finally:
            if root_fd is not None:
                os.close(root_fd)

    def available_sources(self, asset_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id AS asset_source_id,library_root_id,relative_path,
                          display_filename AS filename,availability,is_preferred
                   FROM asset_sources WHERE asset_id=? AND availability='available'
                   ORDER BY is_preferred DESC,created_at""",
                (asset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_source_availability(self, source_id: str, availability: str) -> None:
        if availability not in {"available", "missing", "changed", "revoked"}:
            raise ValueError("Invalid source availability.")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE asset_sources SET availability=?,last_verified_at=?,updated_at=? WHERE id=?",
                (availability, utc_now_iso(), utc_now_iso(), source_id),
            )

    def update_image_probe(self, asset_id: str, *, width: int, height: int) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE assets SET width=?,height=?,rotation_degrees=0,codec_json='{}',
                          probe_status='ready',error_code=NULL,updated_at=?
                   WHERE id=? AND kind='image'""",
                (width, height, utc_now_iso(), asset_id),
            )

    def update_asset_probe(self, asset_id: str, probe: dict[str, object]) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE assets SET duration_ms=?,width=?,height=?,rotation_degrees=?,
                     codec_json=?,captured_at=COALESCE(?,captured_at),probe_status='ready',
                     error_code=NULL,updated_at=? WHERE id=?""",
                (
                    probe["duration_ms"],
                    probe["width"],
                    probe["height"],
                    probe.get("rotation_degrees", 0),
                    canonical_json(probe.get("codec", {})),
                    probe.get("captured_at"),
                    utc_now_iso(),
                    asset_id,
                ),
            )

    def mark_asset_failed(self, asset_id: str, error_code: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE assets SET probe_status='failed',error_code=?,updated_at=? WHERE id=?",
                (error_code, utc_now_iso(), asset_id),
            )

    def create_analysis_job(
        self,
        *,
        asset_id: str,
        analysis_profile_id: str = "adaptive-local-v1",
        checkpoint: dict[str, object] | None = None,
        reuse_active: bool = False,
    ) -> dict[str, object]:
        asset = self.get_asset(asset_id)
        if not asset or asset.get("kind") != "video":
            raise ValueError("Video asset does not exist.")
        now = utc_now_iso()
        job_id = new_id("job")
        run_id = new_id("arun")
        database_uuid = self.database_uuid
        with self.transaction(immediate=True) as connection:
            existing = (
                connection.execute(
                    """SELECT j.id FROM media_jobs j JOIN analysis_runs ar ON ar.id=j.analysis_run_id
                       WHERE j.asset_id=? AND j.status IN ('queued','running','cancelling')
                         AND ar.analysis_profile_id=? AND ar.input_asset_sha256=?
                       ORDER BY j.created_at LIMIT 1""",
                    (asset_id, analysis_profile_id, asset["sha256"]),
                ).fetchone()
                if reuse_active
                else None
            )
            if existing:
                existing_id = str(existing["id"])
            else:
                existing_id = ""
            if existing_id:
                # The outer transaction is released before the public lookup.
                pass
            else:
                row = connection.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 AS revision FROM analysis_runs WHERE asset_id=?",
                    (asset_id,),
                ).fetchone()
                revision = int(row["revision"])
                parent = connection.execute(
                    "SELECT analysis_run_id FROM asset_analysis_heads WHERE asset_id=?", (asset_id,)
                ).fetchone()
                connection.execute(
                    """INSERT INTO analysis_runs(id,asset_id,revision,run_kind,parent_run_id,
                         analysis_profile_id,analysis_profile_json,input_asset_sha256,status,
                         transcript_status,visual_status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,'queued','pending','pending',?)""",
                    (
                        run_id,
                        asset_id,
                        revision,
                        "initial" if revision == 1 else "reanalyze",
                        parent["analysis_run_id"] if parent else None,
                        analysis_profile_id,
                        canonical_json({"id": analysis_profile_id, "external_analysis": False}),
                        asset["sha256"],
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO media_jobs(id,database_uuid,kind,asset_id,analysis_run_id,
                         status,stage,progress,attempt,cancel_requested,checkpoint_json,created_at)
                       VALUES(?,?,'video_index',?,?,'queued','queued',0,1,0,?,?)""",
                    (job_id, database_uuid, asset_id, run_id, canonical_json(checkpoint or {}), now),
                )
        result = self.get_media_job(existing_id or job_id) or {}
        result["reused"] = bool(existing_id)
        return result

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, object]:
        value = dict(row)
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["checkpoint"] = _loads(value.pop("checkpoint_json", None), {})
        value["error"] = _loads(value.pop("error_json", None), None)
        value["partial_errors"] = []
        value["resumable"] = value["status"] in {"partial", "failed", "cancelled", "interrupted"}
        return value

    def get_media_job(self, job_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT j.*,ar.revision AS analysis_revision
                   FROM media_jobs j LEFT JOIN analysis_runs ar ON ar.id=j.analysis_run_id
                   WHERE j.id=?""",
                (job_id,),
            ).fetchone()
        return self._job_dict(row) if row else None

    def list_media_jobs(self, *, active: bool, limit: int = 50) -> list[dict[str, object]]:
        where = "WHERE j.status IN ('queued','running','cancelling','interrupted')" if active else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT j.*,ar.revision AS analysis_revision FROM media_jobs j
                     LEFT JOIN analysis_runs ar ON ar.id=j.analysis_run_id {where}
                     ORDER BY j.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def latest_media_job_for_asset(self, asset_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT j.*,ar.revision AS analysis_revision FROM media_jobs j
                   LEFT JOIN analysis_runs ar ON ar.id=j.analysis_run_id
                   WHERE j.asset_id=? ORDER BY j.created_at DESC LIMIT 1""",
                (asset_id,),
            ).fetchone()
        return self._job_dict(row) if row else None

    def update_media_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        checkpoint: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
        finished: bool = False,
    ) -> None:
        fields = ["heartbeat_at=?"]
        values: list[object] = [utc_now_iso()]
        if status is not None:
            fields.append("status=?")
            values.append(status)
            if status == "running":
                fields.append("started_at=COALESCE(started_at,?)")
                values.append(utc_now_iso())
        if stage is not None:
            fields.append("stage=?")
            values.append(stage)
        if progress is not None:
            fields.append("progress=?")
            values.append(max(0.0, min(float(progress), 1.0)))
        if checkpoint is not None:
            fields.append("checkpoint_json=?")
            values.append(canonical_json(checkpoint))
        if error is not None:
            fields.append("error_json=?")
            values.append(canonical_json(error))
        if finished:
            fields.append("finished_at=?")
            values.append(utc_now_iso())
        values.append(job_id)
        with self.transaction(immediate=True) as connection:
            connection.execute(f"UPDATE media_jobs SET {','.join(fields)} WHERE id=?", values)
            if status:
                run = connection.execute("SELECT analysis_run_id FROM media_jobs WHERE id=?", (job_id,)).fetchone()
                run_status = {
                    "running": "running",
                    "succeeded": "succeeded",
                    "partial": "partial",
                    "failed": "failed",
                    "cancelling": "cancelling",
                    "cancelled": "cancelled",
                    "interrupted": "interrupted",
                }.get(status)
                if run and run["analysis_run_id"] and run_status:
                    connection.execute(
                        """UPDATE analysis_runs SET status=?,started_at=CASE WHEN ?='running'
                             THEN COALESCE(started_at,?) ELSE started_at END,
                             error_json=COALESCE(?,error_json),finished_at=CASE WHEN ? IN
                             ('succeeded','partial','failed','cancelled','interrupted') THEN ? ELSE finished_at END
                           WHERE id=?""",
                        (
                            run_status,
                            run_status,
                            utc_now_iso(),
                            canonical_json(error) if error else None,
                            run_status,
                            utc_now_iso(),
                            run["analysis_run_id"],
                        ),
                    )

    def request_media_job_cancel(self, job_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE media_jobs SET cancel_requested=1,status=CASE
                     WHEN status='running' THEN 'cancelling' ELSE 'cancelled' END,
                     stage=CASE WHEN status='running' THEN stage ELSE 'cancelled' END,
                     finished_at=CASE WHEN status='running' THEN finished_at ELSE ? END,heartbeat_at=?
                   WHERE id=? AND status IN ('queued','running','interrupted')""",
                (utc_now_iso(), utc_now_iso(), job_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """UPDATE analysis_runs SET status=CASE WHEN status='running' THEN 'cancelling'
                               ELSE 'cancelled' END,
                           finished_at=CASE WHEN status='running' THEN finished_at ELSE ? END
                       WHERE id=(SELECT analysis_run_id FROM media_jobs WHERE id=?)""",
                    (utc_now_iso(), job_id),
                )
        return cursor.rowcount == 1

    def reset_media_job_for_resume(self, job_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE media_jobs SET status='queued',stage='queued',progress=0,
                     cancel_requested=0,error_json=NULL,attempt=attempt+1,started_at=NULL,
                     heartbeat_at=NULL,finished_at=NULL
                   WHERE id=? AND status IN ('partial','failed','cancelled','interrupted')""",
                (job_id,),
            )
            if cursor.rowcount:
                connection.execute(
                    """UPDATE analysis_runs SET status='queued',error_json=NULL,started_at=NULL,finished_at=NULL
                       WHERE id=(SELECT analysis_run_id FROM media_jobs WHERE id=?)""",
                    (job_id,),
                )
        return cursor.rowcount == 1

    def mark_running_jobs_interrupted(self) -> int:
        now = utc_now_iso()
        error = canonical_json({"code": "process_interrupted", "message": "Backend stopped during the job."})
        with self.transaction(immediate=True) as connection:
            job_runs = connection.execute(
                "SELECT analysis_run_id FROM media_jobs WHERE status IN ('queued','running','cancelling')"
            ).fetchall()
            cursor = connection.execute(
                "UPDATE media_jobs SET status='interrupted',stage='interrupted',error_json=?,finished_at=? WHERE status IN ('queued','running','cancelling')",
                (error, now),
            )
            for row in job_runs:
                if row["analysis_run_id"]:
                    connection.execute(
                        "UPDATE analysis_runs SET status='interrupted',error_json=?,finished_at=? WHERE id=?",
                        (error, now, row["analysis_run_id"]),
                    )
            connection.execute(
                "UPDATE render_jobs SET status='interrupted',stage='interrupted',error_json=?,finished_at=? WHERE status IN ('queued','running','cancelling')",
                (error, now),
            )
        return cursor.rowcount

    def commit_video_analysis(
        self,
        *,
        job_id: str,
        segments: Sequence[dict[str, object]],
        keyframes: Sequence[dict[str, object]],
        transcripts: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            job = connection.execute("SELECT * FROM media_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or not job["analysis_run_id"] or not job["asset_id"]:
                raise LookupError("Analysis job does not exist.")
            if job["status"] not in {"queued", "running"} or bool(job["cancel_requested"]):
                raise RuntimeError("analysis_commit_rejected")
            run = connection.execute("SELECT * FROM analysis_runs WHERE id=?", (job["analysis_run_id"],)).fetchone()
            asset = connection.execute("SELECT sha256 FROM assets WHERE id=?", (job["asset_id"],)).fetchone()
            if not run or not asset or run["input_asset_sha256"] != asset["sha256"]:
                raise ValueError("Analysis input no longer matches the indexed asset.")
            for transcript in transcripts:
                connection.execute(
                    """INSERT INTO transcript_segments(id,asset_id,analysis_run_id,start_ms,end_ms,
                         text,language,speaker_label,confidence,source,provider,model,payload_manifest_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        transcript["id"],
                        job["asset_id"],
                        run["id"],
                        transcript["start_ms"],
                        transcript["end_ms"],
                        transcript["text"],
                        transcript.get("language"),
                        transcript.get("speaker_label"),
                        transcript.get("confidence"),
                        transcript.get("source", "sidecar_subtitle"),
                        transcript.get("provider", "local_sidecar"),
                        transcript.get("model"),
                        transcript.get("payload_manifest_id"),
                        now,
                    ),
                )
            for segment in segments:
                connection.execute(
                    """INSERT INTO video_segments(id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                         boundary_reason,summary,semantic_json,visible_text,combined_text,text_embedding_model,
                         text_embedding,visual_status,transcript_status,confidence,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        segment["id"],
                        job["asset_id"],
                        run["id"],
                        segment["ordinal"],
                        segment["start_ms"],
                        segment["end_ms"],
                        segment["boundary_reason"],
                        segment.get("summary"),
                        canonical_json(segment.get("semantic", {})),
                        segment.get("visible_text"),
                        segment.get("combined_text", ""),
                        segment.get("text_embedding_model"),
                        segment.get("text_embedding"),
                        segment.get("visual_status", "local_fallback"),
                        segment.get("transcript_status", "unavailable"),
                        segment.get("confidence"),
                        now,
                    ),
                )
            for frame in keyframes:
                connection.execute(
                    """INSERT INTO keyframes(id,segment_id,timestamp_ms,cache_key,sha256,width,height,
                         selection_reason,clarity_score,novelty_score,is_representative,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        frame["id"],
                        frame["segment_id"],
                        frame["timestamp_ms"],
                        frame["cache_key"],
                        frame["sha256"],
                        frame["width"],
                        frame["height"],
                        frame["selection_reason"],
                        frame.get("clarity_score"),
                        frame.get("novelty_score"),
                        int(bool(frame.get("is_representative", True))),
                        now,
                    ),
                )
            representative_segments = {
                str(frame.get("segment_id")) for frame in keyframes if bool(frame.get("is_representative", True))
            }
            if not segments or any(str(segment["id"]) not in representative_segments for segment in segments):
                raise ValueError(
                    "A successful analysis requires segments and a representative keyframe for each segment."
                )
            transcript_status = "available" if transcripts else "unavailable"
            connection.execute(
                """UPDATE analysis_runs SET status='succeeded',transcript_status=?,visual_status='local_fallback',
                     finished_at=? WHERE id=?""",
                (transcript_status, now, run["id"]),
            )
            current_head = connection.execute(
                """SELECT ar.revision FROM asset_analysis_heads ah
                   JOIN analysis_runs ar ON ar.id=ah.analysis_run_id WHERE ah.asset_id=?""",
                (job["asset_id"],),
            ).fetchone()
            if current_head is None or int(run["revision"]) > int(current_head["revision"]):
                connection.execute(
                    """INSERT INTO asset_analysis_heads(asset_id,analysis_run_id,updated_at) VALUES(?,?,?)
                       ON CONFLICT(asset_id) DO UPDATE SET analysis_run_id=excluded.analysis_run_id,
                       updated_at=excluded.updated_at""",
                    (job["asset_id"], run["id"], now),
                )
            connection.execute(
                "UPDATE media_jobs SET status='succeeded',stage='completed',progress=1,heartbeat_at=?,finished_at=? WHERE id=?",
                (now, now, job_id),
            )
        return {
            "asset_id": str(job["asset_id"]),
            "analysis_run_id": str(run["id"]),
            "analysis_revision": int(run["revision"]),
            "segment_count": len(segments),
            "keyframe_count": len(keyframes),
            "transcript_count": len(transcripts),
            "external_analysis": False,
        }

    def mixed_candidates(self) -> tuple[list[dict[str, object]], dict[str, str]]:
        with self.transaction() as connection:
            images = connection.execute(
                """SELECT a.id,a.id AS asset_id,'image_asset' AS result_type,
                     s.id AS asset_source_id,s.display_filename AS filename,s.relative_path,
                     NULL AS start_ms,NULL AS end_ms,i.description AS summary,i.tags_json,i.combined_text,
                     NULL AS analysis_run_id,NULL AS analysis_revision,NULL AS confidence,i.taken_at AS captured_at,
                     NULL AS thumbnail_cache_key,s.availability AS source_availability,
                     a.width,a.height,a.duration_ms
                   FROM assets a JOIN asset_sources s ON s.asset_id=a.id AND s.is_preferred=1
                   LEFT JOIN image_index i ON i.id=a.id WHERE a.kind='image' AND s.availability='available'"""
            ).fetchall()
            videos = connection.execute(
                """SELECT vs.id,vs.asset_id,'video_segment' AS result_type,
                     s.id AS asset_source_id,s.display_filename AS filename,s.relative_path,
                     vs.start_ms,vs.end_ms,vs.summary,vs.semantic_json AS tags_json,vs.combined_text,
                     vs.analysis_run_id,vs.analysis_revision,vs.confidence,a.captured_at,
                     (SELECT k.cache_key FROM keyframes k WHERE k.segment_id=vs.id AND k.is_representative=1
                      ORDER BY k.timestamp_ms LIMIT 1) AS thumbnail_cache_key,
                     s.availability AS source_availability,a.width,a.height,a.duration_ms
                   FROM current_video_segments vs JOIN assets a ON a.id=vs.asset_id
                   JOIN asset_sources s ON s.asset_id=a.id AND s.is_preferred=1
                   WHERE s.availability='available'"""
            ).fetchall()
            heads = connection.execute("SELECT asset_id,analysis_run_id FROM asset_analysis_heads").fetchall()
        results: list[dict[str, object]] = []
        for row in [*images, *videos]:
            item = dict(row)
            raw_tags = _loads(item.pop("tags_json", None), [])
            if isinstance(raw_tags, dict):
                raw_tags = raw_tags.get("tags", [])
            item["tags"] = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
            results.append(item)
        return results, {str(row["asset_id"]): str(row["analysis_run_id"]) for row in heads}

    def get_segment(self, segment_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT vs.*,ar.revision AS analysis_revision,s.id AS asset_source_id,
                     s.display_filename AS filename,s.relative_path,s.availability AS source_availability,
                     a.duration_ms FROM video_segments vs JOIN analysis_runs ar ON ar.id=vs.analysis_run_id
                   JOIN assets a ON a.id=vs.asset_id JOIN asset_sources s ON s.asset_id=a.id AND s.is_preferred=1
                   WHERE vs.id=?""",
                (segment_id,),
            ).fetchone()
            if not row:
                return None
            frames = connection.execute(
                "SELECT * FROM keyframes WHERE segment_id=? ORDER BY timestamp_ms", (segment_id,)
            ).fetchall()
            transcripts = connection.execute(
                """SELECT * FROM transcript_segments WHERE analysis_run_id=? AND end_ms>? AND start_ms<? ORDER BY start_ms""",
                (row["analysis_run_id"], row["start_ms"], row["end_ms"]),
            ).fetchall()
        result = dict(row)
        result["semantic"] = _loads(result.pop("semantic_json", None), {})
        result["keyframes"] = [dict(frame) for frame in frames]
        result["transcripts"] = [dict(item) for item in transcripts]
        return result

    def create_project(self, title: str, brief: dict[str, object], provenance: dict[str, object]) -> dict[str, object]:
        project_id = new_id("proj")
        now = utc_now_iso()
        serialized = canonical_json(brief)
        with self.transaction(immediate=True) as connection:
            connection.execute("INSERT INTO creative_projects VALUES(?,?, 'active',?,?)", (project_id, title, now, now))
            connection.execute(
                "INSERT INTO creative_briefs VALUES(?,1,?,?,?,?)",
                (
                    project_id,
                    serialized,
                    hashlib.sha256(serialized.encode()).hexdigest(),
                    canonical_json(provenance),
                    now,
                ),
            )
        return self.get_project(project_id) or {}

    def get_brief(self, project_id: str, revision: int) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM creative_briefs WHERE project_id=? AND revision=?",
                (project_id, revision),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["brief"] = _loads(value.pop("brief_json"), {})
        value["provenance"] = _loads(value.pop("provenance_json"), {})
        return value

    def get_project(self, project_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            project = connection.execute("SELECT * FROM creative_projects WHERE id=?", (project_id,)).fetchone()
            brief = connection.execute(
                "SELECT * FROM creative_briefs WHERE project_id=? ORDER BY revision DESC LIMIT 1", (project_id,)
            ).fetchone()
            timeline = connection.execute(
                "SELECT id,revision FROM timelines WHERE project_id=? ORDER BY created_at DESC,revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not project or not brief:
            return None
        result = dict(project)
        result["brief_revision"] = int(brief["revision"])
        result["brief"] = _loads(brief["brief_json"], {})
        candidate_values = result["brief"].get("candidate_refs", [])
        result["candidates"] = [value for value in candidate_values if isinstance(value, dict)]
        result["candidate_ref_ids"] = [
            str(value.get("id")) for value in candidate_values if isinstance(value, dict) and value.get("id")
        ]
        result["brief_content_sha256"] = brief["content_sha256"]
        result["latest_timeline"] = dict(timeline) if timeline else None
        result["latest_timeline_id"] = str(timeline["id"]) if timeline else None
        result["latest_timeline_revision"] = int(timeline["revision"]) if timeline else None
        return result

    def save_timeline(
        self,
        *,
        timeline_id: str,
        project_id: str,
        revision: int,
        timeline: dict[str, object],
        provenance: dict[str, object],
        validation_status: str,
        validation_errors: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        serialized = canonical_json(timeline)
        brief_revision = int(provenance.get("brief_revision") or 1)
        parent_revision = revision - 1 if revision > 1 else None
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO timelines(id,project_id,revision,parent_revision,brief_revision,
                     schema_version,timeline_json,content_sha256,provenance_json,validation_status,
                     validation_errors_json,created_at) VALUES(?,?,?,?,?,'1.0',?,?,?,?,?,?)""",
                (
                    timeline_id,
                    project_id,
                    revision,
                    parent_revision,
                    brief_revision,
                    serialized,
                    hashlib.sha256(serialized.encode()).hexdigest(),
                    canonical_json(provenance),
                    validation_status,
                    canonical_json(validation_errors or []),
                    utc_now_iso(),
                ),
            )
        return self.get_timeline(timeline_id, revision) or {}

    def save_timeline_revision_cas(
        self,
        *,
        timeline_id: str,
        project_id: str,
        base_revision: int,
        expected_base_sha256: str,
        timeline: dict[str, object],
        provenance: dict[str, object],
        validation_status: str,
        validation_errors: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        revision = base_revision + 1
        serialized = canonical_json(timeline)
        brief_revision = int(provenance.get("brief_revision") or 1)
        with self.transaction(immediate=True) as connection:
            head = connection.execute(
                "SELECT revision,content_sha256 FROM timelines WHERE id=? ORDER BY revision DESC LIMIT 1",
                (timeline_id,),
            ).fetchone()
            if (
                not head
                or int(head["revision"]) != base_revision
                or str(head["content_sha256"]) != expected_base_sha256
            ):
                raise RuntimeError(f"revision_conflict:{int(head['revision']) if head else 0}")
            connection.execute(
                """INSERT INTO timelines(id,project_id,revision,parent_revision,brief_revision,
                     schema_version,timeline_json,content_sha256,provenance_json,validation_status,
                     validation_errors_json,created_at) VALUES(?,?,?,?,?,'1.0',?,?,?,?,?,?)""",
                (
                    timeline_id,
                    project_id,
                    revision,
                    base_revision,
                    brief_revision,
                    serialized,
                    hashlib.sha256(serialized.encode()).hexdigest(),
                    canonical_json(provenance),
                    validation_status,
                    canonical_json(validation_errors or []),
                    utc_now_iso(),
                ),
            )
        return self.get_timeline(timeline_id, revision) or {}

    def get_timeline(self, timeline_id: str, revision: int | None = None) -> dict[str, object] | None:
        sql = "SELECT * FROM timelines WHERE id=?"
        params: list[object] = [timeline_id]
        if revision is not None:
            sql += " AND revision=?"
            params.append(revision)
        sql += " ORDER BY revision DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(sql, params).fetchone()
        if not row:
            return None
        result = dict(row)
        result["timeline"] = _loads(result.pop("timeline_json"), {})
        result["provenance"] = _loads(result.pop("provenance_json"), {})
        result["validation_errors"] = _loads(result.pop("validation_errors_json"), [])
        return result

    def timeline_revisions(self, timeline_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id,id AS timeline_id,project_id,revision,parent_revision,brief_revision,
                          schema_version,content_sha256,validation_status,created_at
                   FROM timelines WHERE id=? ORDER BY revision DESC""",
                (timeline_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def register_preview_root(self, path: Path) -> dict[str, object]:
        requested = path.expanduser()
        current = Path(requested.anchor)
        for part in requested.parts[1:]:
            current /= part
            if current.exists() or current.is_symlink():
                if current.is_symlink():
                    raise ValueError("Symlinks are not accepted in the app preview root.")
        requested.mkdir(parents=True, exist_ok=True)
        canonical = requested.resolve(strict=True)
        root_id = f"out_{hashlib.sha256(str(canonical).encode()).hexdigest()[:24]}"
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO output_roots(id,kind,canonical_path,permission_fingerprint,status,created_at,updated_at)
                   VALUES(?,'app_preview',?,?,'active',?,?) ON CONFLICT(canonical_path) DO UPDATE SET
                   permission_fingerprint=excluded.permission_fingerprint,status='active',updated_at=excluded.updated_at""",
                (root_id, str(canonical), _permission_fingerprint(canonical), now, now),
            )
            row = connection.execute("SELECT id FROM output_roots WHERE canonical_path=?", (str(canonical),)).fetchone()
        registered_root_id = str(row["id"])
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
        previous = self._output_root_descriptors.get(registered_root_id)
        self._output_root_descriptors[registered_root_id] = descriptor
        if previous is not None:
            try:
                os.close(previous)
            except OSError:
                pass
        return {"id": registered_root_id, "kind": "app_preview"}

    def get_output_root(self, root_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM output_roots WHERE id=?", (root_id,)).fetchone()
        return dict(row) if row else None

    def validate_output_root(self, root_id: str) -> tuple[dict[str, object], Path]:
        root = self.get_output_root(root_id)
        if not root or root["status"] != "active":
            raise ValueError("Output root is unavailable.")
        stored = Path(str(root["canonical_path"]))
        current = Path(stored.anchor)
        for part in stored.parts[1:]:
            current /= part
            if current.is_symlink():
                self._set_output_root_status(root_id, "unavailable")
                raise ValueError("Output root identity changed.")
        try:
            resolved = stored.resolve(strict=True)
        except OSError as exc:
            self._set_output_root_status(root_id, "unavailable")
            raise ValueError("Output root is unavailable.") from exc
        held_descriptor = self._output_root_descriptors.get(root_id)
        if held_descriptor is not None:
            try:
                held_identity = os.fstat(held_descriptor)
                current_identity = resolved.stat()
            except OSError as exc:
                self._set_output_root_status(root_id, "unavailable")
                raise ValueError("Output root is unavailable.") from exc
            if (
                held_identity.st_dev != current_identity.st_dev
                or held_identity.st_ino != current_identity.st_ino
            ):
                self._set_output_root_status(root_id, "unavailable")
                raise ValueError("Output root identity changed.")
        if (
            str(resolved) != str(stored)
            or not resolved.is_dir()
            or _permission_fingerprint(resolved) != root["permission_fingerprint"]
        ):
            self._set_output_root_status(root_id, "unavailable")
            raise ValueError("Output root identity changed.")
        return root, resolved

    def open_output_root_fd(self, root_id: str) -> tuple[dict[str, object], Path, int]:
        root, path = self.validate_output_root(root_id)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        expected = path.stat()
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_dev != expected.st_dev
            or observed.st_ino != expected.st_ino
        ):
            os.close(descriptor)
            self._set_output_root_status(root_id, "unavailable")
            raise ValueError("Output root identity changed.")
        return root, path, descriptor

    def _set_output_root_status(self, root_id: str, status: str) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE output_roots SET status=?,updated_at=? WHERE id=?",
                (status, utc_now_iso(), root_id),
            )

    def create_render_job(
        self,
        *,
        timeline_id: str,
        timeline_revision: int,
        profile: str,
        output_root_id: str,
        output_relative_path: str,
        timeline_content_sha256: str,
        export_grant_id: str | None = None,
        reuse_active: bool = False,
    ) -> dict[str, object]:
        timeline = self.get_timeline(timeline_id, timeline_revision)
        if not timeline or timeline["content_sha256"] != timeline_content_sha256:
            raise ValueError("Timeline content hash does not match the saved revision.")
        root, _ = self.validate_output_root(output_root_id)
        if profile == "preview-low" and root["kind"] != "app_preview":
            raise ValueError("Preview renders must use the app-managed preview root.")
        if profile == "export-1080p" and export_grant_id is None:
            raise ValueError("Export requires an Electron-issued export grant.")
        job_id = new_id("render")
        now = utc_now_iso()
        database_uuid = self.database_uuid
        with self.transaction(immediate=True) as connection:
            existing = (
                connection.execute(
                    """SELECT id FROM render_jobs WHERE timeline_id=? AND timeline_revision=?
                         AND timeline_content_sha256=? AND profile=? AND output_root_id=?
                         AND status IN ('queued','running','cancelling') ORDER BY created_at LIMIT 1""",
                    (timeline_id, timeline_revision, timeline_content_sha256, profile, output_root_id),
                ).fetchone()
                if reuse_active
                else None
            )
            existing_id = str(existing["id"]) if existing else ""
            if not existing:
                connection.execute(
                """INSERT INTO render_jobs(id,database_uuid,timeline_id,timeline_revision,profile,
                     output_root_id,export_grant_id,output_relative_path,timeline_content_sha256,status,
                     stage,progress,attempt,cancel_requested,created_at)
                   VALUES(?,?,?,?,?,?,?, ?,?,'queued','queued',0,1,0,?)""",
                    (
                        job_id,
                        database_uuid,
                        timeline_id,
                        timeline_revision,
                        profile,
                        output_root_id,
                        export_grant_id,
                        output_relative_path,
                        timeline_content_sha256,
                        now,
                    ),
                )
        result = self.get_render_job(existing_id or job_id) or {}
        result["reused"] = bool(existing_id)
        return result

    @staticmethod
    def _render_dict(row: sqlite3.Row) -> dict[str, object]:
        value = dict(row)
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["ffmpeg_command"] = _loads(value.pop("ffmpeg_command_json", None), None)
        value["error"] = _loads(value.pop("error_json", None), None)
        return value

    def get_render_job(self, job_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM render_jobs WHERE id=?", (job_id,)).fetchone()
        return self._render_dict(row) if row else None

    def list_render_jobs(self, *, active: bool, limit: int = 50) -> list[dict[str, object]]:
        where = "WHERE status IN ('queued','running','cancelling','interrupted')" if active else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM render_jobs {where} ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._render_dict(row) for row in rows]

    def render_storage_records(self) -> list[dict[str, object]]:
        """Internal bindings used to reconcile app-owned artifacts after a crash."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,status,output_root_id,output_relative_path,output_sha256 FROM render_jobs"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_render_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        command: list[str] | None = None,
        ffmpeg_version: str | None = None,
        stderr_tail: str | None = None,
        output_sha256: str | None = None,
        size_bytes: int | None = None,
        duration_ms: int | None = None,
        error: dict[str, object] | None = None,
        finished: bool = False,
    ) -> None:
        fields = ["heartbeat_at=?"]
        values: list[object] = [utc_now_iso()]
        for name, value in (
            ("status", status),
            ("stage", stage),
            ("progress", progress),
            ("ffmpeg_version", ffmpeg_version),
            ("output_sha256", output_sha256),
            ("size_bytes", size_bytes),
            ("duration_ms", duration_ms),
        ):
            if value is not None:
                fields.append(f"{name}=?")
                values.append(max(0.0, min(float(value), 1.0)) if name == "progress" else value)
        if status == "running":
            fields.append("started_at=COALESCE(started_at,?)")
            values.append(utc_now_iso())
        if command is not None:
            fields.append("ffmpeg_command_json=?")
            values.append(canonical_json(command))
        if stderr_tail is not None:
            fields.append("stderr_tail=?")
            values.append(stderr_tail[-65536:])
        if error is not None:
            fields.append("error_json=?")
            values.append(canonical_json(error))
        if finished:
            fields.append("finished_at=?")
            values.append(utc_now_iso())
        values.append(job_id)
        with self.transaction(immediate=True) as connection:
            connection.execute(f"UPDATE render_jobs SET {','.join(fields)} WHERE id=?", values)

    def complete_render_job_success(
        self,
        job_id: str,
        *,
        ffmpeg_version: str,
        output_sha256: str,
        size_bytes: int,
        duration_ms: int,
    ) -> bool:
        now = utc_now_iso()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE render_jobs SET status='succeeded',stage='completed',progress=1,
                          ffmpeg_version=?,output_sha256=?,size_bytes=?,duration_ms=?,
                          heartbeat_at=?,finished_at=?
                   WHERE id=? AND status='running' AND cancel_requested=0""",
                (
                    ffmpeg_version,
                    output_sha256,
                    size_bytes,
                    duration_ms,
                    now,
                    now,
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def request_render_cancel(self, job_id: str) -> bool:
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE render_jobs SET cancel_requested=1,status=CASE WHEN status='running'
                     THEN 'cancelling' ELSE 'cancelled' END,
                     stage=CASE WHEN status='running' THEN stage ELSE 'cancelled' END,
                     finished_at=CASE WHEN status='running' THEN finished_at ELSE ? END,heartbeat_at=?
                   WHERE id=? AND status IN ('queued','running','interrupted')""",
                (utc_now_iso(), utc_now_iso(), job_id),
            )
        return cursor.rowcount == 1

    def resolve_render_artifact(self, job_id: str) -> tuple[dict[str, object], Path] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT j.*,r.canonical_path AS output_root_path FROM render_jobs j
                   JOIN output_roots r ON r.id=j.output_root_id WHERE j.id=? AND j.status='succeeded'""",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        try:
            _, root = self.validate_output_root(str(row["output_root_id"]))
        except ValueError:
            return None
        unresolved = root / str(row["output_relative_path"])
        if unresolved.is_symlink():
            return None
        artifact = unresolved.resolve()
        try:
            artifact.relative_to(root)
        except ValueError:
            return None
        if artifact.is_symlink() or not artifact.is_file() or sha256_path(artifact) != row["output_sha256"]:
            return None
        return self._render_dict(row), artifact

    def open_render_artifact(self, job_id: str):
        """Open and verify the exact app-owned artifact inode used by HTTP streaming."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM render_jobs WHERE id=? AND status='succeeded'""",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        filename = str(row["output_relative_path"])
        if not filename or filename in {".", ".."} or Path(filename).name != filename:
            return None
        try:
            _, _, root_fd = self.open_output_root_fd(str(row["output_root_id"]))
        except ValueError:
            return None
        file_fd: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(filename, flags, dir_fd=root_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != int(row["size_bytes"] or -1):
                os.close(file_fd)
                return None
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            with self._artifact_cache_lock:
                cached = self._artifact_cache.get(job_id)
            if cached != (*identity, str(row["output_sha256"])):
                digest = hashlib.sha256()
                with os.fdopen(os.dup(file_fd), "rb") as verification:
                    for chunk in iter(lambda: verification.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = os.fstat(file_fd)
                observed = (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                if observed != identity or digest.hexdigest() != row["output_sha256"]:
                    os.close(file_fd)
                    return None
                with self._artifact_cache_lock:
                    self._artifact_cache[job_id] = (*identity, str(row["output_sha256"]))
            handle = os.fdopen(file_fd, "rb")
            file_fd = None
            return self._render_dict(row), handle, before.st_size
        except OSError:
            if file_fd is not None:
                os.close(file_fd)
            return None
        finally:
            os.close(root_fd)

    def active_job_count(self) -> int:
        with self._connect() as connection:
            media = connection.execute(
                "SELECT COUNT(*) FROM media_jobs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
            renders = connection.execute(
                "SELECT COUNT(*) FROM render_jobs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
        return int(media) + int(renders)

    def claim_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_sha256: str,
        resource_type: str,
        resource_id: str | None,
    ) -> dict[str, object] | None:
        now = datetime.now(timezone.utc)
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM idempotency_records WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            if row:
                try:
                    expired = datetime.fromisoformat(str(row["expires_at"])) <= now
                except ValueError:
                    expired = True
                if expired:
                    connection.execute("DELETE FROM idempotency_records WHERE scope=? AND key=?", (scope, key))
                    row = None
            if row:
                if row["request_sha256"] != request_sha256:
                    raise IdempotencyConflictError("Idempotency key was reused with a different request.")
                value = dict(row)
                value["response"] = _loads(value.pop("response_json", None), None)
                return value
            connection.execute(
                """INSERT INTO idempotency_records(scope,key,request_sha256,state,resource_type,
                     resource_id,created_at,expires_at) VALUES(?,?,?,'in_progress',?,?,?,?)""",
                (
                    scope,
                    key,
                    request_sha256,
                    resource_type,
                    resource_id,
                    now.isoformat(),
                    (now + timedelta(hours=24)).isoformat(),
                ),
            )
        return None

    def complete_idempotency(
        self,
        *,
        scope: str,
        key: str,
        response_status: int,
        response: dict[str, object],
        failed: bool = False,
    ) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE idempotency_records SET state=?,response_status=?,response_json=?
                   WHERE scope=? AND key=?""",
                ("failed" if failed else "completed", response_status, canonical_json(response), scope, key),
            )


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
