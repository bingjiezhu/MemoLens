from __future__ import annotations

import heapq
import hashlib
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from core.media_db import (
    LibraryScanConflictError as CoreLibraryScanConflictError,
    LibraryScanIntegrityError as CoreLibraryScanIntegrityError,
    LibraryScanValidationError as CoreLibraryScanValidationError,
    MediaRepository,
)
from indexing.files import PinnedLibraryRoot, open_pinned_library_root

from . import importing
from .import_plan import ImageImportEnqueueAuthority, MediaImportPlan
from .importing import MediaImportService
from .source_identity import normalized_relative_path, pinned_root_permission_fingerprint


LIBRARY_SCAN_BATCH_SIZE = 16
LIBRARY_SCAN_CHECKPOINT_OBJECT = "memolens.library_scan_checkpoint"
LIBRARY_SCAN_CHECKPOINT_SCHEMA_VERSION = "1"
LIBRARY_SCAN_MAX_COUNT = 9_007_199_254_740_991
LIBRARY_SCAN_MAX_CURSOR_BYTES = 4_096
LIBRARY_SCAN_SUPPORTED_EXTENSIONS = frozenset(
    {
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mov",
        ".mp4",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }
)

_ROOT_ID = re.compile(r"\Aroot_[0-9a-f]{24}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"\Alb_[0-9a-f]{64}\Z")
_SCAN_JOB_ID = re.compile(r"\Ajob_[0-9a-f]{32}\Z")
_DATABASE_UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_CONTEXT_KEYS = frozenset(
    {
        "object",
        "schema_version",
        "request_id",
        "job_id",
        "database_uuid",
        "library_root_id",
        "canonical_root",
        "expected_library_root_device",
        "expected_library_root_inode",
        "root_permission_fingerprint",
        "status",
        "stage",
        "attempt",
        "cancel_requested",
        "checkpoint",
        "error",
    }
)
_CHECKPOINT_KEYS = frozenset(
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
_PUBLIC_ERROR_KEYS = frozenset(
    {
        "object",
        "schema_version",
        "code",
        "stage",
        "retryable",
        "detail_sha256",
    }
)
_PUBLIC_JOB_KEYS = (
    "id",
    "kind",
    "status",
    "stage",
    "progress",
    "attempt",
    "cancel_requested",
    "created_at",
    "started_at",
    "heartbeat_at",
    "finished_at",
)
_ERROR_RETRYABILITY = {
    "worker_interrupted": True,
    "user_cancelled": True,
    "batch_failed": True,
    "root_binding_changed": False,
    "checkpoint_invalid": False,
    "authority_invalid": False,
    "unsupported_platform": False,
}
_STATUS_STAGE_CANCEL = {
    ("queued", "awaiting_scan_worker", False),
    ("running", "scanning", False),
    ("cancelling", "scanning", True),
    ("interrupted", "interrupted", False),
    ("cancelled", "cancelled", True),
    ("failed", "failed", False),
    ("succeeded", "scan_complete", False),
    ("succeeded", "no_supported_media", False),
}


class LibraryScanContractError(RuntimeError):
    """A closed library-scan boundary rejected ambiguous or stale state."""


@dataclass(frozen=True)
class LibraryScanContext:
    request_id: str
    job_id: str
    database_uuid: str
    library_root_id: str
    canonical_root: Path
    expected_library_root_device: int
    expected_library_root_inode: int
    root_permission_fingerprint: str
    status: str
    stage: str
    attempt: int
    cancel_requested: bool
    checkpoint: Mapping[str, object]
    error: Mapping[str, object] | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LibraryScanContext:
        if (
            set(value) != _CONTEXT_KEYS
            or value.get("object") != "memolens.library_scan_context"
            or value.get("schema_version") != "1"
            or not isinstance(value.get("request_id"), str)
            or _REQUEST_ID.fullmatch(str(value["request_id"])) is None
            or not isinstance(value.get("job_id"), str)
            or _SCAN_JOB_ID.fullmatch(str(value["job_id"])) is None
            or not isinstance(value.get("database_uuid"), str)
            or _DATABASE_UUID.fullmatch(str(value["database_uuid"])) is None
            or not isinstance(value.get("library_root_id"), str)
            or _ROOT_ID.fullmatch(str(value["library_root_id"])) is None
            or not isinstance(value.get("canonical_root"), str)
            or not value["canonical_root"]
            or type(value.get("expected_library_root_device")) is not int
            or int(value["expected_library_root_device"]) < 0
            or type(value.get("expected_library_root_inode")) is not int
            or int(value["expected_library_root_inode"]) < 0
            or not isinstance(value.get("root_permission_fingerprint"), str)
            or _SHA256.fullmatch(str(value["root_permission_fingerprint"])) is None
            or type(value.get("attempt")) is not int
            or not 1 <= int(value["attempt"]) <= LIBRARY_SCAN_MAX_COUNT
            or not isinstance(value.get("cancel_requested"), bool)
            or not isinstance(value.get("checkpoint"), Mapping)
            or (value.get("error") is not None and not isinstance(value.get("error"), Mapping))
        ):
            raise LibraryScanContractError("library_scan_authority_invalid")
        canonical_root = Path(str(value["canonical_root"]))
        if not canonical_root.is_absolute() or "\x00" in str(canonical_root):
            raise LibraryScanContractError("library_scan_authority_invalid")
        context = cls(
            request_id=str(value["request_id"]),
            job_id=str(value["job_id"]),
            database_uuid=str(value["database_uuid"]),
            library_root_id=str(value["library_root_id"]),
            canonical_root=canonical_root,
            expected_library_root_device=int(value["expected_library_root_device"]),
            expected_library_root_inode=int(value["expected_library_root_inode"]),
            root_permission_fingerprint=str(value["root_permission_fingerprint"]),
            status=str(value.get("status") or ""),
            stage=str(value.get("stage") or ""),
            attempt=int(value["attempt"]),
            cancel_requested=bool(value["cancel_requested"]),
            checkpoint=value["checkpoint"],
            error=value["error"],
        )
        _validate_library_scan_state(
            status=context.status,
            stage=context.stage,
            cancel_requested=context.cancel_requested,
            checkpoint=context.checkpoint,
            error=context.error,
        )
        return context

    def checkpoint_value(self) -> dict[str, object]:
        return dict(self.checkpoint)

    def open_root(self) -> PinnedLibraryRoot:
        try:
            root = open_pinned_library_root(
                self.canonical_root,
                expected_identity=(
                    self.expected_library_root_device,
                    self.expected_library_root_inode,
                ),
            )
        except (OSError, PermissionError, RuntimeError, ValueError) as exc:
            raise LibraryScanContractError("library_scan_root_binding_changed") from exc
        try:
            if (
                root.root_path != self.canonical_root
                or pinned_root_permission_fingerprint(root) != self.root_permission_fingerprint
            ):
                raise LibraryScanContractError("library_scan_root_binding_changed")
            root.verify_current_path_identity()
            return root
        except Exception:
            root.close()
            raise


@dataclass(frozen=True)
class LibraryScanCheckpoint:
    root_permission_fingerprint: str
    last_relative_path: str | None
    processed_count: int
    imported_count: int
    skipped_count: int
    rejected_count: int
    child_job_count: int
    batch_count: int

    @classmethod
    def initial(cls, *, root_permission_fingerprint: str) -> LibraryScanCheckpoint:
        if _SHA256.fullmatch(root_permission_fingerprint) is None:
            raise LibraryScanContractError("library_scan_root_fingerprint_invalid")
        return cls(
            root_permission_fingerprint=root_permission_fingerprint,
            last_relative_path=None,
            processed_count=0,
            imported_count=0,
            skipped_count=0,
            rejected_count=0,
            child_job_count=0,
            batch_count=0,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LibraryScanCheckpoint:
        if set(value) != _CHECKPOINT_KEYS:
            raise LibraryScanContractError("library_scan_checkpoint_invalid")
        if (
            value.get("object") != LIBRARY_SCAN_CHECKPOINT_OBJECT
            or value.get("schema_version") != LIBRARY_SCAN_CHECKPOINT_SCHEMA_VERSION
            or not isinstance(value.get("root_permission_fingerprint"), str)
            or _SHA256.fullmatch(str(value["root_permission_fingerprint"])) is None
        ):
            raise LibraryScanContractError("library_scan_checkpoint_invalid")
        last_relative_path = value.get("last_relative_path")
        if last_relative_path is not None:
            if not isinstance(last_relative_path, str):
                raise LibraryScanContractError("library_scan_checkpoint_invalid")
            try:
                normalized = normalized_relative_path(last_relative_path).as_posix()
                _relative_sort_key(normalized)
            except (LibraryScanContractError, UnicodeEncodeError, ValueError) as exc:
                raise LibraryScanContractError("library_scan_checkpoint_invalid") from exc
            if normalized != last_relative_path:
                raise LibraryScanContractError("library_scan_checkpoint_invalid")
        count_names = (
            "processed_count",
            "imported_count",
            "skipped_count",
            "rejected_count",
            "child_job_count",
            "batch_count",
        )
        if any(
            type(value.get(name)) is not int or not 0 <= int(value[name]) <= LIBRARY_SCAN_MAX_COUNT
            for name in count_names
        ):
            raise LibraryScanContractError("library_scan_checkpoint_invalid")
        checkpoint = cls(
            root_permission_fingerprint=str(value["root_permission_fingerprint"]),
            last_relative_path=last_relative_path,
            processed_count=int(value["processed_count"]),
            imported_count=int(value["imported_count"]),
            skipped_count=int(value["skipped_count"]),
            rejected_count=int(value["rejected_count"]),
            child_job_count=int(value["child_job_count"]),
            batch_count=int(value["batch_count"]),
        )
        if (
            checkpoint.imported_count + checkpoint.skipped_count > checkpoint.processed_count
            or checkpoint.rejected_count > checkpoint.processed_count
            or checkpoint.child_job_count > checkpoint.processed_count
            or checkpoint.batch_count > checkpoint.processed_count
            or (checkpoint.processed_count == 0) != (checkpoint.last_relative_path is None)
        ):
            raise LibraryScanContractError("library_scan_checkpoint_invalid")
        return checkpoint

    def to_dict(self) -> dict[str, object]:
        return {
            "object": LIBRARY_SCAN_CHECKPOINT_OBJECT,
            "schema_version": LIBRARY_SCAN_CHECKPOINT_SCHEMA_VERSION,
            "root_permission_fingerprint": self.root_permission_fingerprint,
            "last_relative_path": self.last_relative_path,
            "processed_count": self.processed_count,
            "imported_count": self.imported_count,
            "skipped_count": self.skipped_count,
            "rejected_count": self.rejected_count,
            "child_job_count": self.child_job_count,
            "batch_count": self.batch_count,
        }


@dataclass(frozen=True)
class LibraryScanDiscoveryBatch:
    entries: tuple[LibraryScanDiscoveryEntry, ...]
    has_more: bool

    @property
    def relative_paths(self) -> tuple[str, ...]:
        return tuple(entry.relative_path for entry in self.entries)

    @property
    def importable_relative_paths(self) -> tuple[str, ...]:
        return tuple(entry.relative_path for entry in self.entries if entry.importable)


@dataclass(frozen=True)
class LibraryScanDiscoveryEntry:
    relative_path: str
    importable: bool


def _relative_sort_key(relative_path: str) -> bytes:
    try:
        encoded = relative_path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LibraryScanContractError("library_scan_unsupported_platform") from exc
    if not 1 <= len(encoded) <= LIBRARY_SCAN_MAX_CURSOR_BYTES:
        raise LibraryScanContractError("library_scan_unsupported_platform")
    return encoded


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise LibraryScanContractError("library_scan_unsupported_platform")
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _open_relative_directory(root: PinnedLibraryRoot, relative: Path) -> int:
    descriptor = root.duplicate_fd()
    try:
        for component in relative.parts:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def discover_library_scan_batch(
    root: PinnedLibraryRoot,
    *,
    last_relative_path: str | None,
    batch_size: int = LIBRARY_SCAN_BATCH_SIZE,
    max_total_bytes: int | None = None,
) -> LibraryScanDiscoveryBatch:
    """Return one UTF-8-byte-ordered batch without following any symlink.

    Directory placeholders are expanded through descriptor-relative
    ``O_NOFOLLOW`` opens.  Keeping them in the same heap as files preserves one
    stable global relative-path order while allowing discovery to stop after a
    count- and byte-bounded prefix. These sizes only schedule batches; import
    preparation still independently pins and validates each source.
    """

    if type(batch_size) is not int or batch_size <= 0 or batch_size > 128:
        raise ValueError("library_scan_batch_size_invalid")
    if max_total_bytes is None:
        max_total_bytes = importing.MAX_IMPORT_BYTES
    if type(max_total_bytes) is not int or max_total_bytes <= 0:
        raise ValueError("library_scan_byte_budget_invalid")
    cursor_key: bytes | None = None
    if last_relative_path is not None:
        try:
            normalized = normalized_relative_path(last_relative_path).as_posix()
        except ValueError as exc:
            raise LibraryScanContractError("library_scan_checkpoint_invalid") from exc
        if normalized != last_relative_path:
            raise LibraryScanContractError("library_scan_checkpoint_invalid")
        cursor_key = _relative_sort_key(normalized)

    root.verify_current_path_identity()
    # (sort key, relative path, entry kind, observed bytes). Directories are
    # expanded even when their own path sorts before the persisted cursor. A directory whose name
    # has a supported-media suffix is also one rejected, processed entry.
    pending: list[tuple[bytes, str, str, int]] = []

    def add_directory_entries(relative_directory: Path) -> None:
        descriptor = _open_relative_directory(root, relative_directory)
        try:
            for name in os.listdir(descriptor):
                if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                    raise LibraryScanContractError("library_scan_entry_name_invalid")
                relative = relative_directory / name
                relative_text = relative.as_posix()
                key = _relative_sort_key(relative_text)
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    heapq.heappush(pending, (key, relative_text, "directory", 0))
                elif relative.suffix.casefold() in LIBRARY_SCAN_SUPPORTED_EXTENSIONS:
                    kind = "regular" if stat.S_ISREG(metadata.st_mode) else "rejected"
                    heapq.heappush(pending, (key, relative_text, kind, int(metadata.st_size)))
                # Unsupported symlinks and special files are outside the scan
                # authority. Supported ones are processed as rejected entries
                # so the durable cursor cannot retry them forever.
        finally:
            os.close(descriptor)

    add_directory_entries(Path())
    accepted: list[LibraryScanDiscoveryEntry] = []
    total_bytes = 0
    has_more = False
    while pending:
        key, relative_text, entry_kind, observed_bytes = heapq.heappop(pending)
        if entry_kind == "directory":
            add_directory_entries(Path(relative_text))
            if Path(relative_text).suffix.casefold() not in LIBRARY_SCAN_SUPPORTED_EXTENSIONS:
                continue
        if cursor_key is not None and key <= cursor_key:
            continue
        # Individually oversized files still reach the existing import rejection
        # path, with no budget charge. They must not stall the durable cursor.
        budget_bytes = observed_bytes if entry_kind == "regular" and observed_bytes <= max_total_bytes else 0
        if len(accepted) == batch_size or total_bytes + budget_bytes > max_total_bytes:
            has_more = True
            break
        total_bytes += budget_bytes
        accepted.append(
            LibraryScanDiscoveryEntry(
                relative_path=relative_text,
                importable=entry_kind == "regular",
            )
        )
    root.verify_current_path_identity()
    return LibraryScanDiscoveryBatch(tuple(accepted), has_more)


def public_library_scan_job(job: Mapping[str, object]) -> dict[str, object]:
    """Project one path-free, closed library-scan job for public routes."""

    if job.get("kind") != "library_scan":
        raise LibraryScanContractError("library_scan_authority_invalid")
    job_id = job.get("id")
    if not isinstance(job_id, str) or _SCAN_JOB_ID.fullmatch(job_id) is None:
        raise LibraryScanContractError("library_scan_authority_invalid")
    checkpoint_value = job.get("checkpoint")
    if checkpoint_value == {}:
        counts = {
            "processed": 0,
            "imported": 0,
            "skipped": 0,
            "rejected": 0,
            "child_jobs": 0,
            "batches": 0,
        }
    elif isinstance(checkpoint_value, Mapping):
        checkpoint = LibraryScanCheckpoint.from_mapping(checkpoint_value)
        counts = {
            "processed": checkpoint.processed_count,
            "imported": checkpoint.imported_count,
            "skipped": checkpoint.skipped_count,
            "rejected": checkpoint.rejected_count,
            "child_jobs": checkpoint.child_job_count,
            "batches": checkpoint.batch_count,
        }
    else:
        raise LibraryScanContractError("library_scan_checkpoint_invalid")

    status = str(job.get("status") or "")
    stage = str(job.get("stage") or "")
    cancel_requested = job.get("cancel_requested")
    raw_error = job.get("error")
    _validate_library_scan_state(
        status=status,
        stage=stage,
        cancel_requested=cancel_requested,
        checkpoint=checkpoint_value,
        error=raw_error,
    )
    public_error = (
        {
            "code": str(raw_error["code"]),
            "retryable": bool(raw_error["retryable"]),
        }
        if isinstance(raw_error, Mapping)
        else None
    )
    retryable = bool(public_error and public_error["retryable"])
    progress = job.get("progress")
    attempt = job.get("attempt")
    if (
        isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or not math.isfinite(float(progress))
        or not 0.0 <= float(progress) <= 1.0
        or type(attempt) is not int
        or not 1 <= attempt <= LIBRARY_SCAN_MAX_COUNT
    ):
        raise LibraryScanContractError("library_scan_authority_invalid")
    _validate_public_timestamps(job, status=status)

    public = {key: job.get(key) for key in _PUBLIC_JOB_KEYS}
    public.update(
        {
            "object": "memolens.library_scan_job",
            "schema_version": "1",
            "counts": counts,
            "no_supported_media": status == "succeeded" and stage == "no_supported_media",
            "eta_seconds": None,
            "error": public_error,
            "resumable": status in {"interrupted", "cancelled", "failed"} and retryable,
        }
    )
    return public


def public_library_scan_snapshot(value: Mapping[str, object]) -> dict[str, object]:
    """Validate and project one receipt-anchored Core read snapshot."""

    if set(value) != {"context", "job"}:
        raise LibraryScanContractError("library_scan_authority_invalid")
    raw_context = value.get("context")
    raw_job = value.get("job")
    if not isinstance(raw_context, Mapping) or not isinstance(raw_job, Mapping):
        raise LibraryScanContractError("library_scan_authority_invalid")
    context = LibraryScanContext.from_mapping(raw_context)
    if set(raw_job) != {
        "id",
        "status",
        "stage",
        "progress",
        "attempt",
        "cancel_requested",
        "created_at",
        "started_at",
        "heartbeat_at",
        "finished_at",
    }:
        raise LibraryScanContractError("library_scan_authority_invalid")
    if (
        raw_job.get("id") != context.job_id
        or raw_job.get("status") != context.status
        or raw_job.get("stage") != context.stage
        or raw_job.get("attempt") != context.attempt
        or raw_job.get("cancel_requested") is not context.cancel_requested
    ):
        raise LibraryScanContractError("library_scan_authority_invalid")
    return public_library_scan_job(
        {
            **dict(raw_job),
            "kind": "library_scan",
            "checkpoint": context.checkpoint,
            "error": context.error,
        }
    )


def _validate_library_scan_state(
    *,
    status: object,
    stage: object,
    cancel_requested: object,
    checkpoint: object,
    error: object,
) -> None:
    if (status, stage, cancel_requested) not in _STATUS_STAGE_CANCEL:
        raise LibraryScanContractError("library_scan_authority_invalid")
    if checkpoint == {}:
        if status != "queued":
            raise LibraryScanContractError("library_scan_checkpoint_invalid")
    elif isinstance(checkpoint, Mapping):
        LibraryScanCheckpoint.from_mapping(checkpoint)
    else:
        raise LibraryScanContractError("library_scan_checkpoint_invalid")

    if status in {"queued", "running", "cancelling", "succeeded"}:
        if error is not None:
            raise LibraryScanContractError("library_scan_authority_invalid")
        return
    if (
        not isinstance(error, Mapping)
        or set(error) != _PUBLIC_ERROR_KEYS
        or error.get("object") != "memolens.library_scan_error"
        or error.get("schema_version") != "1"
        or not isinstance(error.get("code"), str)
        or error.get("code") not in _ERROR_RETRYABILITY
        or error.get("retryable") is not _ERROR_RETRYABILITY[error["code"]]
        or error.get("stage") not in {"awaiting_scan_worker", "scanning"}
        or not isinstance(error.get("detail_sha256"), str)
        or _SHA256.fullmatch(str(error["detail_sha256"])) is None
    ):
        raise LibraryScanContractError("library_scan_authority_invalid")
    code = str(error["code"])
    if (
        (status == "interrupted" and code != "worker_interrupted")
        or (status == "cancelled" and code != "user_cancelled")
        or (
            status == "failed"
            and code
            not in {
                "batch_failed",
                "root_binding_changed",
                "checkpoint_invalid",
                "authority_invalid",
                "unsupported_platform",
            }
        )
    ):
        raise LibraryScanContractError("library_scan_authority_invalid")


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
        or not value.endswith("+00:00")
        or parsed.isoformat() != value
    ):
        return None
    return parsed


def _validate_public_timestamps(job: Mapping[str, object], *, status: str) -> None:
    created = _timestamp(job.get("created_at"))
    started = _timestamp(job.get("started_at"))
    heartbeat = _timestamp(job.get("heartbeat_at"))
    finished = _timestamp(job.get("finished_at"))
    if created is None:
        raise LibraryScanContractError("library_scan_authority_invalid")
    if status == "queued":
        valid = job.get("started_at") is None and job.get("heartbeat_at") is None and job.get("finished_at") is None
    elif status in {"running", "cancelling"}:
        valid = (
            started is not None
            and heartbeat is not None
            and job.get("finished_at") is None
            and created <= started <= heartbeat
        )
    elif status == "cancelled":
        valid = (
            heartbeat is not None
            and finished is not None
            and created <= heartbeat <= finished
            and (started is None or created <= started <= heartbeat)
        )
    elif status == "failed":
        valid = (
            heartbeat is not None
            and finished is not None
            and created <= heartbeat <= finished
            and (started is None or created <= started <= heartbeat)
        )
    else:
        valid = (
            started is not None
            and heartbeat is not None
            and finished is not None
            and created <= started <= heartbeat <= finished
        )
    if not valid:
        raise LibraryScanContractError("library_scan_authority_invalid")


def require_library_scan_root_id(value: object) -> str:
    if not isinstance(value, str) or _ROOT_ID.fullmatch(value) is None:
        raise LibraryScanContractError("library_scan_authority_invalid")
    return value


class _NoopJobSubmitter:
    def submit(self, _job_id: str) -> None:
        raise RuntimeError("library_scan_child_submit_before_commit")


class LibraryScanRunner:
    """Durably advance exactly one V20 bootstrap scan in bounded ordered batches."""

    def __init__(
        self,
        repository: MediaRepository,
        *,
        submit_child: Callable[[str], None],
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.repository = repository
        self._submit_child = submit_child
        self._fault_injector = fault_injector
        self._import_service = MediaImportService(repository, _NoopJobSubmitter())

    def _fault(self, boundary: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(boundary)

    def context(self, job_id: str) -> LibraryScanContext | None:
        try:
            raw = self.repository.get_library_scan_context(job_id)
        except CoreLibraryScanIntegrityError as exc:
            code = "library_scan_checkpoint_invalid" if "checkpoint" in str(exc) else "library_scan_authority_invalid"
            raise LibraryScanContractError(code) from exc
        if raw is None:
            return None
        context = LibraryScanContext.from_mapping(raw)
        if context.job_id != job_id or context.database_uuid != self.repository.database_uuid:
            raise LibraryScanContractError("library_scan_authority_invalid")
        return context

    @staticmethod
    def _require_active_binding(
        context: LibraryScanContext,
        *,
        active_library_root_id: str,
    ) -> None:
        if context.library_root_id != require_library_scan_root_id(active_library_root_id):
            raise LibraryScanContractError("library_scan_authority_invalid")

    def recover(
        self,
        *,
        runtime_generation_id: str,
        active_library_root_id: str,
    ) -> list[str]:
        admitted: list[str] = []
        for raw in self.repository.list_recoverable_library_scans():
            job_id = str(raw.get("job_id") or "") if isinstance(raw, Mapping) else ""
            try:
                context = LibraryScanContext.from_mapping(raw)
                self._require_active_binding(
                    context,
                    active_library_root_id=active_library_root_id,
                )
                if context.database_uuid != self.repository.database_uuid:
                    raise LibraryScanContractError("library_scan_authority_invalid")
                if context.status == "interrupted":
                    context = self.resume(
                        context.job_id,
                        active_library_root_id=active_library_root_id,
                    )
                if context.status == "queued" and not context.cancel_requested and context.job_id not in admitted:
                    admitted.append(context.job_id)
            except LibraryScanContractError as exc:
                self._invalidate_if_supported(None, job_id=job_id, code=str(exc))
                continue
            except (OSError, RuntimeError, ValueError):
                # Recovery is an admission filter. A stale/foreign context is
                # never rewritten or submitted by this runtime.
                continue
        return admitted

    def resume(
        self,
        job_id: str,
        *,
        active_library_root_id: str,
    ) -> LibraryScanContext:
        context = self.context(job_id)
        if context is None:
            raise LibraryScanContractError("library_scan_authority_invalid")
        self._require_active_binding(
            context,
            active_library_root_id=active_library_root_id,
        )
        with context.open_root() as root:
            resumed = self.repository.resume_library_scan(
                job_id=job_id,
                expected_attempt=context.attempt,
                expected_checkpoint=context.checkpoint_value(),
                library_root_fd=root.root_fd,
            )
        return LibraryScanContext.from_mapping(resumed)

    def cancel(
        self,
        job_id: str,
        *,
        runtime_generation_id: str,
        active_library_root_id: str,
    ) -> LibraryScanContext:
        context = self.context(job_id)
        if context is None:
            raise LibraryScanContractError("library_scan_authority_invalid")
        self._require_active_binding(
            context,
            active_library_root_id=active_library_root_id,
        )
        generation = runtime_generation_id if context.status in {"running", "cancelling"} else None
        with context.open_root() as root:
            cancelled = self.repository.request_library_scan_cancel(
                job_id=job_id,
                expected_attempt=context.attempt,
                expected_checkpoint=context.checkpoint_value(),
                library_root_fd=root.root_fd,
                runtime_generation_id=generation,
            )
        return LibraryScanContext.from_mapping(cancelled)

    def interrupt(
        self,
        job_id: str,
        *,
        runtime_generation_id: str,
        active_library_root_id: str,
    ) -> LibraryScanContext | None:
        context = self.context(job_id)
        if context is None or context.status not in {"running", "cancelling"}:
            return context
        self._require_active_binding(
            context,
            active_library_root_id=active_library_root_id,
        )
        with context.open_root() as root:
            interrupted = self.repository.interrupt_library_scan(
                job_id=job_id,
                runtime_generation_id=runtime_generation_id,
                expected_attempt=context.attempt,
                expected_checkpoint=context.checkpoint_value(),
                library_root_fd=root.root_fd,
                detail="process_interrupted",
            )
        return LibraryScanContext.from_mapping(interrupted)

    def run(
        self,
        job_id: str,
        *,
        runtime_generation_id: str,
        active_library_root_id: str,
        should_stop: Callable[[], bool],
    ) -> None:
        context: LibraryScanContext | None = None
        try:
            context = self.context(job_id)
            if context is None:
                return
            self._require_active_binding(
                context,
                active_library_root_id=active_library_root_id,
            )
            with context.open_root() as root:
                claimed = self.repository.claim_library_scan(
                    job_id=job_id,
                    runtime_generation_id=runtime_generation_id,
                    expected_attempt=context.attempt,
                    expected_checkpoint=context.checkpoint_value(),
                    library_root_fd=root.root_fd,
                )
                context = LibraryScanContext.from_mapping(claimed)
                self._run_claimed(
                    context,
                    root=root,
                    runtime_generation_id=runtime_generation_id,
                    should_stop=should_stop,
                )
        except LibraryScanContractError as exc:
            self._invalidate_if_supported(context, job_id=job_id, code=str(exc))
        except CoreLibraryScanConflictError:
            # Another valid state transition won the exact checkpoint/attempt
            # CAS. This worker has no authority to rewrite the winner.
            return
        except CoreLibraryScanValidationError as exc:
            code = str(getattr(exc, "code", exc))
            normalized = {
                "root_binding_changed": "library_scan_root_binding_changed",
                "checkpoint_invalid": "library_scan_checkpoint_invalid",
                "authority_invalid": "library_scan_authority_invalid",
                "unsupported_platform": "library_scan_unsupported_platform",
            }.get(code)
            if normalized is not None:
                self._invalidate_if_supported(
                    context,
                    job_id=job_id,
                    code=normalized,
                )
            elif context is not None:
                self._finish_after_exception(
                    context,
                    runtime_generation_id=runtime_generation_id,
                    detail=type(exc).__name__,
                )
        except CoreLibraryScanIntegrityError as exc:
            code = "library_scan_checkpoint_invalid" if "checkpoint" in str(exc) else "library_scan_authority_invalid"
            self._invalidate_if_supported(context, job_id=job_id, code=code)
        except Exception as exc:
            if context is not None:
                self._finish_after_exception(
                    context,
                    runtime_generation_id=runtime_generation_id,
                    detail=type(exc).__name__,
                )

    def _run_claimed(
        self,
        context: LibraryScanContext,
        *,
        root: PinnedLibraryRoot,
        runtime_generation_id: str,
        should_stop: Callable[[], bool],
    ) -> None:
        while True:
            context = self._latest_running_context(
                context,
                root=root,
                runtime_generation_id=runtime_generation_id,
                should_stop=should_stop,
            )
            if context.status != "running":
                return
            checkpoint = LibraryScanCheckpoint.from_mapping(context.checkpoint)
            batch = discover_library_scan_batch(
                root,
                last_relative_path=checkpoint.last_relative_path,
            )
            if not batch.entries:
                self.repository.complete_library_scan(
                    job_id=context.job_id,
                    runtime_generation_id=runtime_generation_id,
                    expected_attempt=context.attempt,
                    expected_checkpoint=context.checkpoint_value(),
                    library_root_fd=root.root_fd,
                    no_supported_media=checkpoint.processed_count == 0,
                )
                return

            plan: MediaImportPlan | None = None
            all_child_job_ids: list[str] = []
            new_child_job_ids: list[str] = []
            try:
                if batch.importable_relative_paths:
                    plan = self._import_service.prepare_import(
                        root_id=context.library_root_id,
                        root=context.canonical_root,
                        payload={
                            "recursive": False,
                            "dry_run": False,
                            "relative_paths": list(batch.importable_relative_paths),
                            "kinds": ["image", "video"],
                        },
                    )
                self._fault("before_batch_commit")

                def apply_batch(connection):
                    self._fault("inside_batch_transaction")
                    nonimportable_count = len(batch.entries) - len(batch.importable_relative_paths)
                    if plan is None:
                        return {
                            "processed_count": len(batch.entries),
                            "imported_count": 0,
                            "skipped_count": 0,
                            "rejected_count": nonimportable_count,
                            "child_job_ids": [],
                        }
                    authority = ImageImportEnqueueAuthority(
                        runtime_generation=runtime_generation_id,
                        database_file_identity=self.repository.bind_image_database_identity(),
                        operation_id=self._batch_operation_id(
                            context=context,
                            relative_paths=batch.relative_paths,
                        ),
                    )
                    prepared_paths = {item.relative_path for item in plan.assets}
                    rejected_paths = {str(item.get("relative_path") or "") for item in plan.rejected}
                    missing_count = len(set(batch.importable_relative_paths) - prepared_paths - rejected_paths)
                    result = MediaImportService.apply_prepared(
                        connection,
                        root_id=context.library_root_id,
                        plan=plan,
                        image_enqueue=self.repository.enqueue_image_analysis_in_transaction,
                        image_authority=authority,
                    )
                    all_child_job_ids.extend(str(job.get("id") or "") for job in result.jobs if job.get("id"))
                    new_child_job_ids.extend(
                        str(job.get("id") or "") for job in result.jobs if job.get("id") and job.get("reused") is False
                    )
                    return {
                        "processed_count": len(batch.entries),
                        "imported_count": result.imported,
                        "skipped_count": result.skipped,
                        "rejected_count": (nonimportable_count + missing_count + len(result.rejected)),
                        "child_job_ids": list(new_child_job_ids),
                    }

                committed = self.repository.commit_library_scan_batch(
                    job_id=context.job_id,
                    runtime_generation_id=runtime_generation_id,
                    expected_attempt=context.attempt,
                    expected_checkpoint=context.checkpoint_value(),
                    library_root_fd=root.root_fd,
                    relative_paths=list(batch.relative_paths),
                    apply_batch=apply_batch,
                )
            finally:
                if plan is not None:
                    plan.close()
            if set(committed) != {"scan", "child_job_ids"}:
                raise LibraryScanContractError("library_scan_authority_invalid")
            context = LibraryScanContext.from_mapping(committed["scan"])
            committed_child_ids = committed["child_job_ids"]
            if not isinstance(committed_child_ids, list) or committed_child_ids != new_child_job_ids:
                raise LibraryScanContractError("library_scan_authority_invalid")
            self._fault("after_batch_commit")
            for child_job_id in dict.fromkeys(all_child_job_ids):
                self._submit_child(child_job_id)
            if not batch.has_more:
                self.repository.complete_library_scan(
                    job_id=context.job_id,
                    runtime_generation_id=runtime_generation_id,
                    expected_attempt=context.attempt,
                    expected_checkpoint=context.checkpoint_value(),
                    library_root_fd=root.root_fd,
                    no_supported_media=False,
                )
                return

    def _latest_running_context(
        self,
        context: LibraryScanContext,
        *,
        root: PinnedLibraryRoot,
        runtime_generation_id: str,
        should_stop: Callable[[], bool],
    ) -> LibraryScanContext:
        latest = self.context(context.job_id)
        if latest is None:
            raise LibraryScanContractError("library_scan_authority_invalid")
        if latest.attempt != context.attempt:
            raise LibraryScanContractError("library_scan_authority_invalid")
        if latest.status == "cancelling":
            cancelled = self.repository.finish_library_scan_cancel(
                job_id=latest.job_id,
                runtime_generation_id=runtime_generation_id,
                expected_attempt=latest.attempt,
                expected_checkpoint=latest.checkpoint_value(),
                library_root_fd=root.root_fd,
            )
            return LibraryScanContext.from_mapping(cancelled)
        if should_stop() and latest.status == "running":
            interrupted = self.repository.interrupt_library_scan(
                job_id=latest.job_id,
                runtime_generation_id=runtime_generation_id,
                expected_attempt=latest.attempt,
                expected_checkpoint=latest.checkpoint_value(),
                library_root_fd=root.root_fd,
                detail="process_interrupted",
            )
            return LibraryScanContext.from_mapping(interrupted)
        return latest

    def _finish_after_exception(
        self,
        context: LibraryScanContext,
        *,
        runtime_generation_id: str,
        detail: str,
    ) -> None:
        try:
            latest = self.context(context.job_id)
            if latest is None or latest.attempt != context.attempt:
                return
            with latest.open_root() as root:
                if latest.status == "cancelling":
                    self.repository.finish_library_scan_cancel(
                        job_id=latest.job_id,
                        runtime_generation_id=runtime_generation_id,
                        expected_attempt=latest.attempt,
                        expected_checkpoint=latest.checkpoint_value(),
                        library_root_fd=root.root_fd,
                    )
                elif latest.status == "running":
                    self.repository.fail_library_scan(
                        job_id=latest.job_id,
                        runtime_generation_id=runtime_generation_id,
                        expected_attempt=latest.attempt,
                        expected_checkpoint=latest.checkpoint_value(),
                        library_root_fd=root.root_fd,
                        detail=detail,
                    )
        except Exception:
            # A failed terminal write never authorizes a generic fallback.
            return

    def _invalidate_if_supported(
        self,
        context: LibraryScanContext | None,
        *,
        job_id: str,
        code: str,
    ) -> None:
        invalidator = getattr(self.repository, "invalidate_library_scan", None)
        if not callable(invalidator):
            return
        normalized = {
            "library_scan_root_binding_changed": "root_binding_changed",
            "library_scan_checkpoint_invalid": "checkpoint_invalid",
            "library_scan_authority_invalid": "authority_invalid",
            "library_scan_unsupported_platform": "unsupported_platform",
        }.get(code)
        if normalized is None:
            return
        try:
            if context is None:
                job = self.repository.get_media_job(job_id)
                if (
                    job is None
                    or job.get("kind") != "library_scan"
                    or type(job.get("attempt")) is not int
                    or not isinstance(job.get("checkpoint"), Mapping)
                ):
                    return
                expected_attempt = int(job["attempt"])
                expected_checkpoint = dict(job["checkpoint"])
            else:
                expected_attempt = context.attempt
                expected_checkpoint = context.checkpoint_value()
            invalidator(
                job_id=job_id,
                expected_attempt=expected_attempt,
                expected_checkpoint=expected_checkpoint,
                code=normalized,
                detail=normalized,
            )
        except Exception:
            return

    @staticmethod
    def _batch_operation_id(
        *,
        context: LibraryScanContext,
        relative_paths: tuple[str, ...],
    ) -> str:
        material = (
            "memolens.library_scan_batch.v1\0"
            + context.job_id
            + "\0"
            + str(context.attempt)
            + "\0"
            + "\0".join(relative_paths)
        ).encode("utf-8")
        return f"library_scan_{hashlib.sha256(material).hexdigest()}"
