from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.media_db import MediaRepository, sha256_path

from .video import MediaCancelled, MediaCapabilityError


HashPath = Callable[[Path], str]


@dataclass(frozen=True)
class OutputTarget:
    root_id: str
    root: dict[str, object]
    directory: Path
    filename: str
    path: Path


def _validate_export_grant(
    repository: MediaRepository,
    job: dict[str, object],
) -> None:
    """Read-only worker admission for the dormant legacy export profile.

    Electron does not expose grant issuance for this lane.  Still, a durable
    row that reaches the dispatcher must not turn a foreign, expired, revoked,
    consumed, or scope-mismatched grant into filesystem/process authority.
    """

    grant_id = job.get("export_grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise MediaCapabilityError(
            "export_grant_required",
            "Final export requires an Electron-issued export grant.",
        )
    try:
        with repository.transaction() as connection:
            current = connection.execute(
                """SELECT j.*,t.project_id AS timeline_project_id
                     FROM render_jobs AS j
                     JOIN timelines AS t
                       ON t.id=j.timeline_id AND t.revision=j.timeline_revision
                    WHERE j.id=?""",
                (str(job.get("id") or ""),),
            ).fetchone()
            grant = connection.execute(
                "SELECT * FROM export_grants WHERE id=?",
                (grant_id,),
            ).fetchone()
    except MediaCapabilityError:
        raise
    except Exception as exc:
        raise MediaCapabilityError(
            "export_grant_unavailable",
            "The export grant cannot be verified by the active database.",
        ) from exc

    if current is None:
        raise MediaCapabilityError(
            "render_scope_changed",
            "The durable render job binding changed before dispatch.",
        )
    immutable_fields = (
        "id",
        "database_uuid",
        "timeline_id",
        "timeline_revision",
        "timeline_content_sha256",
        "profile",
        "output_root_id",
        "export_grant_id",
        "output_relative_path",
        "attempt",
        "created_at",
    )
    if any(current[field] != job.get(field) for field in immutable_fields):
        raise MediaCapabilityError(
            "render_scope_changed",
            "The durable render job binding changed before dispatch.",
        )
    if grant is None:
        raise MediaCapabilityError(
            "export_grant_unavailable",
            "The export grant is missing or no longer active.",
        )
    try:
        expires_at = datetime.fromisoformat(str(grant["expires_at"]))
    except (TypeError, ValueError) as exc:
        raise MediaCapabilityError(
            "export_grant_unavailable",
            "The export grant is missing or no longer active.",
        ) from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if grant["status"] != "active" or expires_at <= datetime.now(timezone.utc):
        raise MediaCapabilityError(
            "export_grant_unavailable",
            "The export grant is missing or no longer active.",
        )
    if not (
        grant["output_root_id"] == job.get("output_root_id")
        and grant["project_id"] == current["timeline_project_id"]
        and grant["filename"] == job.get("output_relative_path")
    ):
        raise MediaCapabilityError(
            "export_grant_scope_mismatch",
            "The export grant is not bound to this timeline and native destination.",
        )


class _PublicationTransaction:
    def __init__(self, repository: MediaRepository, job_id: str, target: OutputTarget, directory_fd: int):
        self.repository = repository
        self.job_id = job_id
        self.target = target
        self.directory_fd = directory_fd
        self.linked = False
        self.committed = False

    def link(self, temporary: Path) -> None:
        try:
            os.link(temporary, self.target.filename, dst_dir_fd=self.directory_fd, follow_symlinks=False)
            self.linked = True
        except FileExistsError as exc:
            raise MediaCapabilityError("output_exists", "The app artifact filename already exists.") from exc

    def commit(
        self,
        *,
        ffmpeg_version: str,
        output_sha256: str,
        output_size: int,
        duration_ms: int,
    ) -> None:
        if self.repository.complete_render_job_success(
            self.job_id,
            ffmpeg_version=ffmpeg_version,
            output_sha256=output_sha256,
            size_bytes=output_size,
            duration_ms=duration_ms,
        ):
            self.committed = True
            return
        if self.linked:
            os.unlink(self.target.filename, dir_fd=self.directory_fd)
            self.linked = False
        raise MediaCancelled("Render was cancelled during publication.")

    def rollback(self) -> None:
        if not self.linked or self.committed:
            return
        try:
            os.unlink(self.target.filename, dir_fd=self.directory_fd)
            self.linked = False
        except FileNotFoundError:
            pass

    def close(self) -> None:
        try:
            os.close(self.directory_fd)
        except OSError:
            if not self.committed:
                raise


def validate_output_target(repository: MediaRepository, job: dict[str, object]) -> OutputTarget:
    root_id = str(job["output_root_id"])
    profile = str(job.get("profile") or "preview-low")
    if profile == "export-1080p":
        _validate_export_grant(repository, job)
    try:
        root, output_root = repository.validate_output_root(root_id)
    except ValueError as exc:
        message = (
            "The native export root identity changed."
            if profile == "export-1080p"
            else "The app preview root identity changed."
        )
        raise MediaCapabilityError("output_root_changed", message) from exc
    expected_kind = "user_export" if profile == "export-1080p" else "app_preview"
    if root["kind"] != expected_kind:
        message = (
            "Final export requires the native user-selected output root."
            if profile == "export-1080p"
            else "Preview requires the app output root."
        )
        raise MediaCapabilityError("output_root_unavailable", message)
    filename = str(job["output_relative_path"])
    if not filename or filename in {".", ".."} or any(value in filename for value in ("/", "\\", "\x00")):
        raise MediaCapabilityError("invalid_output_name", "Output must be one safe filename.")
    final_output = output_root / filename
    if final_output.exists() or final_output.is_symlink():
        raise MediaCapabilityError("output_exists", "The app artifact filename already exists.")
    return OutputTarget(root_id=root_id, root=root, directory=output_root, filename=filename, path=final_output)


def publish_render(
    repository: MediaRepository,
    *,
    job_id: str,
    target: OutputTarget,
    temporary: Path,
    duration_ms: int,
    ffmpeg_version: Callable[[], str],
    cancelled: Callable[[], bool],
    hash_path: HashPath = sha256_path,
) -> None:
    if cancelled():
        raise MediaCancelled("Render was cancelled before publication.")

    _, _, directory_fd = repository.open_output_root_fd(target.root_id)
    output_sha256 = hash_path(temporary)
    output_size = temporary.stat().st_size
    publication = _PublicationTransaction(repository, job_id, target, directory_fd)
    try:
        if cancelled():
            raise MediaCancelled("Render was cancelled before publication.")
        publication.link(temporary)
        publication.commit(
            ffmpeg_version=ffmpeg_version(),
            output_sha256=output_sha256,
            output_size=output_size,
            duration_ms=duration_ms,
        )
    except BaseException:
        publication.rollback()
        raise
    finally:
        publication.close()

    # A committed artifact is authoritative. Cleanup must not downgrade success.
    try:
        temporary.unlink()
    except OSError:
        pass


def _remove_uncommitted_artifact(repository: MediaRepository, job: dict[str, object]) -> None:
    if job["status"] == "succeeded":
        return
    filename = str(job["output_relative_path"])
    if not filename or filename in {".", ".."} or Path(filename).name != filename:
        return
    try:
        _, _, descriptor = repository.open_output_root_fd(str(job["output_root_id"]))
    except ValueError:
        return
    try:
        metadata = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
        if metadata.st_mode & 0o170000 == 0o100000:
            os.unlink(filename, dir_fd=descriptor)
    except FileNotFoundError:
        pass
    finally:
        os.close(descriptor)


def _remove_owned_workspaces(cache_root: Path, job_ids: set[str]) -> None:
    jobs_root = cache_root / "render-jobs"
    if not jobs_root.is_dir() or jobs_root.is_symlink():
        return
    for entry in jobs_root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        job_id = next((identifier for identifier in job_ids if entry.name.startswith(f"{identifier}-")), None)
        if job_id is not None:
            shutil.rmtree(entry, ignore_errors=True)


def reconcile_interrupted_storage(repository: MediaRepository, cache_root: Path) -> None:
    """Remove only DB-owned leftovers for jobs that cannot be successful."""
    jobs = repository.render_storage_records()
    for job in jobs:
        _remove_uncommitted_artifact(repository, job)
    _remove_owned_workspaces(cache_root, {str(job["id"]) for job in jobs})
