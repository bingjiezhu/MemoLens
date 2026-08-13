from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from core.media_db import MediaRepository, canonical_json, new_id, utc_now_iso


ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling"})
ANALYSIS_PROFILE_ID = "adaptive-local-v1"


@dataclass(frozen=True)
class PreparedMediaAsset:
    """Stable file metadata collected before the database transaction starts."""

    kind: str
    filename: str
    relative_path: str
    sha256: str
    mime_type: str
    file_size: int
    mtime_ns: int
    source_file_id: str
    width: int | None = None
    height: int | None = None
    probe_error: str | None = None


@dataclass(frozen=True)
class MediaImportPlan:
    dry_run: bool
    kinds: list[str]
    assets: list[PreparedMediaAsset]
    rejected: list[dict[str, object]]


@dataclass(frozen=True)
class MediaImportResult:
    dry_run: bool
    kinds: list[str]
    assets: list[dict[str, object]]
    jobs: list[dict[str, object]]
    imported: int
    skipped: int
    rejected: list[dict[str, object]]

    @property
    def status(self) -> str:
        if self.dry_run:
            return "dry_run"
        if self.rejected:
            return "partial"
        return "queued" if self.jobs else "succeeded"


def apply_import_plan(
    connection: sqlite3.Connection,
    *,
    root_id: str,
    plan: MediaImportPlan,
) -> MediaImportResult:
    """Apply one prepared manifest using only the caller-owned transaction."""

    assets: list[dict[str, object]] = []
    jobs_by_id: dict[str, dict[str, object]] = {}
    rejected = [dict(item) for item in plan.rejected]
    imported = 0
    skipped = 0
    database_uuid = _database_uuid(connection) if not plan.dry_run else ""

    for item in plan.assets:
        action = _source_action(connection, root_id=root_id, item=item)
        if action == "unchanged":
            skipped += 1
        else:
            imported += 1

        if plan.dry_run:
            assets.append(
                {
                    "kind": item.kind,
                    "filename": item.filename,
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "action": action,
                }
            )
            continue

        asset = _upsert_asset_source(
            connection,
            root_id=root_id,
            item=item,
        )
        asset["action"] = action
        if item.kind == "image":
            rejection = _apply_image_probe(connection, item=item, asset=asset)
            if rejection is not None:
                rejected.append(rejection)
        else:
            job = _schedule_video_job(
                connection,
                asset_id=str(asset["id"]),
                action=action,
                database_uuid=database_uuid,
            )
            if job is not None:
                jobs_by_id.setdefault(str(job["id"]), job)
        assets.append(asset)

    return MediaImportResult(
        dry_run=plan.dry_run,
        kinds=list(plan.kinds),
        assets=assets,
        jobs=list(jobs_by_id.values()),
        imported=imported,
        skipped=skipped,
        rejected=rejected,
    )


def _database_uuid(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT database_uuid FROM database_meta WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("database_meta is missing.")
    return str(row["database_uuid"])


def _source_action(
    connection: sqlite3.Connection,
    *,
    root_id: str,
    item: PreparedMediaAsset,
) -> str:
    row = connection.execute(
        """SELECT asset.sha256
             FROM asset_sources source
             JOIN assets asset ON asset.id=source.asset_id
            WHERE source.library_root_id=? AND source.relative_path=?""",
        (root_id, item.relative_path),
    ).fetchone()
    if row is None:
        return "imported"
    return "unchanged" if row["sha256"] == item.sha256 else "rebound"


def _upsert_asset_source(
    connection: sqlite3.Connection,
    *,
    root_id: str,
    item: PreparedMediaAsset,
) -> dict[str, object]:
    asset_id = f"asset_{item.sha256[:24]}"
    source_id = MediaRepository.source_id(root_id, item.relative_path)
    now = utc_now_iso()
    connection.execute(
        """INSERT INTO assets(id,kind,sha256,mime_type,file_size,probe_status,created_at,updated_at)
           VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(sha256) DO NOTHING""",
        (
            asset_id,
            item.kind,
            item.sha256,
            item.mime_type,
            item.file_size,
            now,
            now,
        ),
    )
    asset_row = connection.execute(
        "SELECT id,kind,probe_status FROM assets WHERE sha256=?",
        (item.sha256,),
    ).fetchone()
    if asset_row is None:
        raise RuntimeError("Imported asset could not be resolved.")
    if asset_row["kind"] != item.kind:
        raise ValueError("Identical bytes already belong to a different media kind.")
    asset_id = str(asset_row["id"])

    prior = connection.execute(
        """SELECT id,asset_id,is_preferred
             FROM asset_sources
            WHERE library_root_id=? AND relative_path=?""",
        (root_id, item.relative_path),
    ).fetchone()
    if prior is not None and prior["asset_id"] != asset_id:
        old_asset_id = str(prior["asset_id"])
        connection.execute("DELETE FROM asset_sources WHERE id=?", (prior["id"],))
        if bool(prior["is_preferred"]):
            replacement = connection.execute(
                """SELECT id FROM asset_sources
                    WHERE asset_id=? AND availability='available'
                    ORDER BY created_at LIMIT 1""",
                (old_asset_id,),
            ).fetchone()
            if replacement is not None:
                connection.execute(
                    "UPDATE asset_sources SET is_preferred=1,updated_at=? WHERE id=?",
                    (now, replacement["id"]),
                )

    preferred = not connection.execute(
        "SELECT 1 FROM asset_sources WHERE asset_id=? AND is_preferred=1",
        (asset_id,),
    ).fetchone()
    connection.execute(
        """INSERT INTO asset_sources(
               id,asset_id,library_root_id,relative_path,display_filename,
               observed_size,observed_mtime_ns,source_file_id,availability,
               is_preferred,last_verified_at,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'available',?,?,?,?)
           ON CONFLICT(library_root_id,relative_path) DO UPDATE SET
             display_filename=excluded.display_filename,
             observed_size=excluded.observed_size,
             observed_mtime_ns=excluded.observed_mtime_ns,
             source_file_id=excluded.source_file_id,
             availability='available',
             last_verified_at=excluded.last_verified_at,
             updated_at=excluded.updated_at""",
        (
            source_id,
            asset_id,
            root_id,
            item.relative_path,
            item.filename,
            item.file_size,
            item.mtime_ns,
            item.source_file_id,
            int(preferred),
            now,
            now,
            now,
        ),
    )
    return {
        "id": asset_id,
        "asset_source_id": source_id,
        "kind": item.kind,
        "filename": item.filename,
        "relative_path": item.relative_path,
        "probe_status": str(asset_row["probe_status"]),
    }


def _apply_image_probe(
    connection: sqlite3.Connection,
    *,
    item: PreparedMediaAsset,
    asset: dict[str, object],
) -> dict[str, object] | None:
    now = utc_now_iso()
    if item.probe_error is not None:
        connection.execute(
            "UPDATE assets SET probe_status='failed',error_code='invalid_image',updated_at=? WHERE id=?",
            (now, asset["id"]),
        )
        asset["probe_status"] = "failed"
        return {
            "relative_path": item.relative_path,
            "code": "invalid_image",
            "message": item.probe_error,
            "retryable": False,
        }
    if item.width is None or item.height is None:
        raise RuntimeError("Prepared image dimensions are missing.")
    connection.execute(
        """UPDATE assets SET width=?,height=?,rotation_degrees=0,codec_json='{}',
                  probe_status='ready',error_code=NULL,updated_at=?
           WHERE id=? AND kind='image'""",
        (item.width, item.height, now, asset["id"]),
    )
    asset["probe_status"] = "ready"
    return None


def _schedule_video_job(
    connection: sqlite3.Connection,
    *,
    asset_id: str,
    action: str,
    database_uuid: str,
) -> dict[str, object] | None:
    asset = connection.execute(
        "SELECT id,kind,sha256,probe_status FROM assets WHERE id=?",
        (asset_id,),
    ).fetchone()
    if asset is None or asset["kind"] != "video":
        raise ValueError("Video asset does not exist.")
    if asset["probe_status"] == "ready" and connection.execute(
        "SELECT 1 FROM asset_analysis_heads WHERE asset_id=?",
        (asset_id,),
    ).fetchone():
        return None

    prior = connection.execute(
        "SELECT status FROM media_jobs WHERE asset_id=? ORDER BY created_at DESC LIMIT 1",
        (asset_id,),
    ).fetchone()
    if action == "unchanged" and prior is not None and prior["status"] not in ACTIVE_JOB_STATES:
        return None

    existing = connection.execute(
        """SELECT job.id
             FROM media_jobs job
             JOIN analysis_runs run ON run.id=job.analysis_run_id
            WHERE job.asset_id=? AND job.status IN ('queued','running','cancelling')
              AND run.analysis_profile_id=? AND run.input_asset_sha256=?
            ORDER BY job.created_at LIMIT 1""",
        (asset_id, ANALYSIS_PROFILE_ID, asset["sha256"]),
    ).fetchone()
    reused = existing is not None
    if reused:
        job_id = str(existing["id"])
    else:
        job_id = new_id("job")
        run_id = new_id("arun")
        revision_row = connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 AS revision FROM analysis_runs WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if revision_row is None:
            raise RuntimeError("Analysis revision could not be allocated.")
        revision = int(revision_row["revision"])
        parent = connection.execute(
            "SELECT analysis_run_id FROM asset_analysis_heads WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        now = utc_now_iso()
        connection.execute(
            """INSERT INTO analysis_runs(
                   id,asset_id,revision,run_kind,parent_run_id,analysis_profile_id,
                   analysis_profile_json,input_asset_sha256,status,transcript_status,
                   visual_status,created_at)
               VALUES(?,?,?,?,?,?,?,?,'queued','pending','pending',?)""",
            (
                run_id,
                asset_id,
                revision,
                "initial" if revision == 1 else "reanalyze",
                parent["analysis_run_id"] if parent is not None else None,
                ANALYSIS_PROFILE_ID,
                canonical_json({"id": ANALYSIS_PROFILE_ID, "external_analysis": False}),
                asset["sha256"],
                now,
            ),
        )
        connection.execute(
            """INSERT INTO media_jobs(
                   id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                   progress,attempt,cancel_requested,checkpoint_json,created_at)
               VALUES(?,?,'video_index',?,?,'queued','queued',0,1,0,'{}',?)""",
            (job_id, database_uuid, asset_id, run_id, now),
        )

    job_row = connection.execute(
        """SELECT job.*,run.revision AS analysis_revision
             FROM media_jobs job
             LEFT JOIN analysis_runs run ON run.id=job.analysis_run_id
            WHERE job.id=?""",
        (job_id,),
    ).fetchone()
    if job_row is None:
        raise RuntimeError("Analysis job could not be resolved.")
    job = MediaRepository._job_dict(job_row)
    job["reused"] = reused
    return job
