from __future__ import annotations

import hashlib
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from backend.src.media.video import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    discover_media,
    mime_type_for,
)
from core.media_db import MediaRepository


MAX_IMPORT_FILES = 500
MAX_IMPORT_BYTES = 20 * 1024 * 1024 * 1024
IMPORT_TIMEOUT_SECONDS = 30.0
ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancelling"})


class MediaJobSubmitter(Protocol):
    def submit(self, job_id: str) -> None: ...


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


@dataclass(frozen=True)
class _ImportOptions:
    recursive: bool
    dry_run: bool
    relative_paths: list[str] | None
    kinds: list[str]


class MediaImportService:
    """Discover and persist local media without depending on Flask request state."""

    def __init__(
        self,
        repository: MediaRepository,
        job_runner: MediaJobSubmitter,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository
        self.job_runner = job_runner
        self._clock = clock or time.monotonic

    @staticmethod
    def supported_extensions(kinds: list[str]) -> set[str]:
        extensions: set[str] = set()
        if "image" in kinds:
            extensions.update(IMAGE_EXTENSIONS)
        if "video" in kinds:
            extensions.update(VIDEO_EXTENSIONS)
        return extensions

    def import_assets(
        self,
        *,
        root_id: str,
        root: Path,
        payload: Mapping[str, object],
    ) -> MediaImportResult:
        options = self._parse_options(payload)
        deadline = self._clock() + IMPORT_TIMEOUT_SECONDS
        paths = discover_media(
            root,
            recursive=options.recursive,
            files=options.relative_paths,
            extensions=self.supported_extensions(options.kinds),
            max_files=MAX_IMPORT_FILES,
            max_total_bytes=MAX_IMPORT_BYTES,
            deadline=deadline,
        )
        assets: list[dict[str, object]] = []
        jobs: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        imported_count = 0
        skipped_count = 0

        for path in paths:
            self._require_time_remaining(deadline)
            relative_path = path.relative_to(root).as_posix()
            try:
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Media source must be a regular non-symlink file.")
                kind = "video" if path.suffix.casefold() in VIDEO_EXTENSIONS else "image"
                digest = self._hash(path, deadline)
                action = self._source_action(root_id, relative_path, digest)
                if action == "unchanged":
                    skipped_count += 1
                else:
                    imported_count += 1

                if options.dry_run:
                    assets.append(
                        {
                            "kind": kind,
                            "filename": path.name,
                            "relative_path": relative_path,
                            "sha256": digest,
                            "action": action,
                        }
                    )
                    continue

                asset = self.repository.upsert_asset_source(
                    root_id=root_id,
                    relative_path=relative_path,
                    filename=path.name,
                    kind=kind,
                    sha256=digest,
                    mime_type=mime_type_for(path, kind),
                    file_size=metadata.st_size,
                    mtime_ns=metadata.st_mtime_ns,
                    source_file_id=str(metadata.st_ino),
                )
                asset["action"] = action
                assets.append(asset)
                if kind == "image":
                    rejection = self._probe_image(path, relative_path, asset)
                    if rejection:
                        rejected.append(rejection)
                else:
                    job = self._schedule_video(str(asset["id"]), action)
                    if job:
                        jobs.append(job)
                        self.job_runner.submit(str(job["id"]))
            except (OSError, ValueError) as exc:
                rejected.append(
                    {
                        "relative_path": relative_path,
                        "code": "import_rejected",
                        "message": str(exc),
                        "retryable": False,
                    }
                )

        return MediaImportResult(
            dry_run=options.dry_run,
            kinds=options.kinds,
            assets=assets,
            jobs=jobs,
            imported=imported_count,
            skipped=skipped_count,
            rejected=rejected,
        )

    @staticmethod
    def _parse_options(payload: Mapping[str, object]) -> _ImportOptions:
        recursive = payload.get("recursive", True)
        dry_run = payload.get("dry_run", False)
        if not isinstance(recursive, bool) or not isinstance(dry_run, bool):
            raise ValueError("`recursive` and `dry_run` must be booleans.")

        raw_paths = payload.get("relative_paths")
        if raw_paths is not None and (
            not isinstance(raw_paths, list)
            or not raw_paths
            or any(not isinstance(value, str) or not value.strip() for value in raw_paths)
        ):
            raise ValueError("`relative_paths` must be a non-empty string array when set.")

        raw_kinds = payload.get("kinds", ["image", "video"])
        if (
            not isinstance(raw_kinds, list)
            or not raw_kinds
            or any(not isinstance(value, str) or value not in {"image", "video"} for value in raw_kinds)
        ):
            raise ValueError("`kinds` must be a non-empty array containing only image and/or video.")
        return _ImportOptions(
            recursive=recursive,
            dry_run=dry_run,
            relative_paths=raw_paths,
            kinds=list(dict.fromkeys(raw_kinds)),
        )

    def _hash(self, path: Path, deadline: float) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                self._require_time_remaining(deadline)
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _source_action(self, root_id: str, relative_path: str, digest: str) -> str:
        source_id = self.repository.source_id(root_id, relative_path)
        existing_source = self.repository.get_asset_source(source_id)
        if existing_source and existing_source.get("sha256") == digest:
            return "unchanged"
        return "rebound" if existing_source else "imported"

    def _probe_image(
        self,
        path: Path,
        relative_path: str,
        asset: dict[str, object],
    ) -> dict[str, object] | None:
        try:
            with Image.open(path) as source_image:
                width, height = ImageOps.exif_transpose(source_image).size
            self.repository.update_image_probe(str(asset["id"]), width=width, height=height)
            asset["probe_status"] = "ready"
        except (OSError, UnidentifiedImageError) as exc:
            self.repository.mark_asset_failed(str(asset["id"]), "invalid_image")
            return {
                "relative_path": relative_path,
                "code": "invalid_image",
                "message": str(exc),
                "retryable": False,
            }
        return None

    def _schedule_video(self, asset_id: str, action: str) -> dict[str, object] | None:
        current = self.repository.get_asset(asset_id)
        has_head = bool(
            current
            and current.get("probe_status") == "ready"
            and current.get("id")
            and self.repository.has_analysis_head(asset_id)
        )
        if has_head:
            return None

        prior_job = self.repository.latest_media_job_for_asset(asset_id)
        if action == "unchanged" and prior_job is not None and prior_job.get("status") not in ACTIVE_JOB_STATES:
            return None
        return self.repository.create_analysis_job(asset_id=asset_id, reuse_active=True)

    def _require_time_remaining(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise ValueError(
                "import_manifest_timeout: synchronous import exceeded 30 seconds; "
                "use smaller relative_paths batches."
            )
