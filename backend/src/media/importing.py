from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

from backend.src.media.video import (
    IMAGE_EXTENSIONS,
    MEDIA_JOB_KIND_REGISTRY,
    VIDEO_EXTENSIONS,
    mime_type_for,
)
from core.media_db import MediaRepository

from .import_plan import (
    ImageAnalysisTransactionEnqueuer,
    ImageImportEnqueueAuthority,
    MediaImportPlan,
    MediaImportResult,
    PreparedMediaAsset,
    apply_import_plan,
)
from .source_identity import (
    PinnedMediaSource,
    collect_media_relative_paths,
    open_pinned_media_source,
    pinned_root_permission_fingerprint,
)
from indexing.files import open_pinned_library_root


MAX_IMPORT_FILES = 500
MAX_IMPORT_BYTES = 20 * 1024 * 1024 * 1024
IMPORT_TIMEOUT_SECONDS = 30.0
SERVER_OWNED_IMAGE_AUTHORITY_FIELDS = frozenset(
    {
        "analysis_profile",
        "asset_id",
        "database_device",
        "database_file_identity",
        "database_inode",
        "enqueue_scope",
        "expected_head",
        "runtime_generation",
        "source_binding",
        "source_id",
    }
)


class MediaJobSubmitter(Protocol):
    def submit(self, job_id: str) -> None: ...


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
        plan = self.prepare_import(root_id=root_id, root=root, payload=payload)
        with self.repository.transaction(immediate=True) as connection:
            result = self.apply_prepared(connection, root_id=root_id, plan=plan)
        self.submit_jobs(result.jobs)
        return result

    def prepare_import(
        self,
        *,
        root_id: str,
        root: Path,
        payload: Mapping[str, object],
    ) -> MediaImportPlan:
        """Freeze filesystem observations without reading or writing SQLite."""

        options = self._parse_options(payload)
        deadline = self._clock() + IMPORT_TIMEOUT_SECONDS
        pinned_root = open_pinned_library_root(root)
        assets: list[PreparedMediaAsset] = []
        rejected: list[dict[str, object]] = []
        pinned_sources: list[PinnedMediaSource] = []

        try:
            root_record = self.repository.library_root(root_id)
            if (
                root_record is None
                or root_record.get("status") != "active"
                or str(root_record.get("canonical_path") or "") != str(pinned_root.root_path)
                or str(root_record.get("permission_fingerprint") or "")
                != pinned_root_permission_fingerprint(pinned_root)
            ):
                raise ValueError(
                    "Approved Library root does not match its persisted filesystem identity."
                )
            relative_paths = collect_media_relative_paths(
                pinned_root,
                recursive=options.recursive,
                files=options.relative_paths,
                extensions=self.supported_extensions(options.kinds),
                max_files=MAX_IMPORT_FILES,
                max_total_bytes=MAX_IMPORT_BYTES,
                deadline=deadline,
                clock=self._clock,
            )
            for relative in relative_paths:
                self._require_time_remaining(deadline)
                relative_path = relative.as_posix()
                source: PinnedMediaSource | None = None
                try:
                    source = open_pinned_media_source(
                        pinned_root=pinned_root,
                        relative_path=relative,
                        max_bytes=MAX_IMPORT_BYTES,
                    )
                    assets.append(
                        self._prepare_asset(
                            source,
                            relative_path=relative_path,
                            deadline=deadline,
                            probe_image=not options.dry_run,
                        )
                    )
                    pinned_sources.append(source)
                    source = None
                except (OSError, ValueError) as exc:
                    rejected.append(
                        {
                            "relative_path": relative_path,
                            "code": "import_rejected",
                            "message": str(exc),
                            "retryable": False,
                        }
                    )
                finally:
                    if source is not None:
                        source.close()

            return MediaImportPlan(
                dry_run=options.dry_run,
                kinds=options.kinds,
                assets=assets,
                rejected=rejected,
                root_permission_fingerprint=pinned_root_permission_fingerprint(
                    pinned_root
                ),
                pinned_root=pinned_root,
                pinned_sources=tuple(pinned_sources),
            )
        except Exception:
            for source in pinned_sources:
                source.close()
            pinned_root.close()
            raise

    @staticmethod
    def apply_prepared(
        connection: sqlite3.Connection,
        *,
        root_id: str,
        plan: MediaImportPlan,
        image_enqueue: ImageAnalysisTransactionEnqueuer | None = None,
        image_authority: ImageImportEnqueueAuthority | None = None,
        retain_source_bindings: bool = False,
    ) -> MediaImportResult:
        try:
            return apply_import_plan(
                connection,
                root_id=root_id,
                plan=plan,
                image_enqueue=image_enqueue,
                image_authority=image_authority,
            )
        finally:
            if not retain_source_bindings:
                plan.close()

    def submit_jobs(self, jobs: list[dict[str, object]]) -> None:
        """Dispatch only committed work that still needs a process-local runner."""

        submitted: set[str] = set()
        for job in jobs:
            job_id = str(job.get("id") or "")
            if not job_id or job_id in submitted:
                continue
            submitted.add(job_id)
            current = self.repository.get_media_job(job_id)
            kind = current.get("kind") if current is not None else None
            admitted_statuses = (
                {"queued"}
                if kind == "image_analysis"
                else {"queued", "interrupted"}
            )
            if (
                current is None
                or kind not in MEDIA_JOB_KIND_REGISTRY
                or current.get("status") not in admitted_statuses
                or bool(current.get("cancel_requested"))
            ):
                continue
            self.job_runner.submit(job_id)

    @staticmethod
    def _parse_options(payload: Mapping[str, object]) -> _ImportOptions:
        forbidden = sorted(SERVER_OWNED_IMAGE_AUTHORITY_FIELDS.intersection(payload))
        if forbidden:
            raise ValueError(
                "Image analysis authority fields are server-derived and cannot be supplied: "
                + ", ".join(forbidden)
                + "."
            )
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

    def _prepare_asset(
        self,
        source: PinnedMediaSource,
        *,
        relative_path: str,
        deadline: float,
        probe_image: bool,
    ) -> PreparedMediaAsset:
        digest = hashlib.sha256()
        before = os.fstat(source.descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Media source must be a regular non-symlink file.")
        with source.open_handle() as handle:
            while True:
                self._require_time_remaining(deadline)
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            kind = "video" if source.relative_path.suffix.casefold() in VIDEO_EXTENSIONS else "image"
            width: int | None = None
            height: int | None = None
            probe_error: str | None = None
            if kind == "image" and probe_image:
                handle.seek(0)
                try:
                    with Image.open(handle) as source_image:
                        width, height = ImageOps.exif_transpose(source_image).size
                except (OSError, UnidentifiedImageError) as exc:
                    probe_error = str(exc)
            self._require_time_remaining(deadline)
            after = os.fstat(handle.fileno())
        if source.identity != source.identity.from_stat(after):
            raise ValueError("Media source changed while the import manifest was prepared.")
        source.verify_current_identity()
        return PreparedMediaAsset(
            kind=kind,
            filename=source.relative_path.name,
            relative_path=relative_path,
            sha256=digest.hexdigest(),
            mime_type=mime_type_for(source.relative_path, kind),
            file_size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            source_file_id=str(before.st_ino),
            source_identity=source.identity,
            width=width,
            height=height,
            probe_error=probe_error,
        )

    def _require_time_remaining(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise ValueError(
                "import_manifest_timeout: synchronous import exceeded 30 seconds; use smaller relative_paths batches."
            )
