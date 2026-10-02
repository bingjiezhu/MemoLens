"""Dedicated durable state machine for V20 plugin-first Library scans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat


CHECKPOINT_OBJECT = "memolens.library_scan_checkpoint"
CONTEXT_OBJECT = "memolens.library_scan_context"
ERROR_OBJECT = "memolens.library_scan_error"
MAX_COUNT = 9_007_199_254_740_991
MAX_CURSOR_BYTES = 4_096
MAX_BATCH_ENTRIES = 500
CHECKPOINT_KEYS = frozenset(
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
ERROR_KEYS = frozenset(
    {"object", "schema_version", "code", "stage", "retryable", "detail_sha256"}
)
CONTEXT_KEYS = frozenset(
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


class LibraryScanValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LibraryScanConflictError(RuntimeError):
    def __init__(self, code: str = "library_scan_state_conflict") -> None:
        super().__init__(code)
        self.code = code


class LibraryScanIntegrityError(RuntimeError):
    def __init__(self, code: str = "library_scan_integrity_error") -> None:
        super().__init__(code)
        self.code = code


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and not any(character not in "0123456789abcdef" for character in value)
    )


def _require_count(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise LibraryScanValidationError(f"library_scan_{field}_invalid")
    return value


def _relative_path_bytes(value: object) -> bytes:
    if type(value) is not str or not value or "\x00" in value:
        raise LibraryScanValidationError("library_scan_relative_path_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise LibraryScanValidationError("library_scan_relative_path_invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise LibraryScanValidationError("unsupported_platform") from exc
    if not 1 <= len(encoded) <= MAX_CURSOR_BYTES:
        raise LibraryScanValidationError("library_scan_relative_path_invalid")
    return encoded


def validate_library_scan_checkpoint(
    value: object,
    *,
    allow_initial: bool = True,
) -> dict[str, object]:
    """Return a detached exact checkpoint or fail closed."""

    if not isinstance(value, Mapping):
        raise LibraryScanValidationError("checkpoint_invalid")
    checkpoint = dict(value)
    if checkpoint == {}:
        if allow_initial:
            return {}
        raise LibraryScanValidationError("checkpoint_invalid")
    if set(checkpoint) != CHECKPOINT_KEYS:
        raise LibraryScanValidationError("checkpoint_invalid")
    if (
        checkpoint["object"] != CHECKPOINT_OBJECT
        or checkpoint["schema_version"] != "1"
        or not _is_lower_hex(checkpoint["root_permission_fingerprint"], 64)
    ):
        raise LibraryScanValidationError("checkpoint_invalid")
    cursor = checkpoint["last_relative_path"]
    if cursor is not None:
        try:
            _relative_path_bytes(cursor)
        except LibraryScanValidationError as exc:
            raise LibraryScanValidationError("checkpoint_invalid") from exc
    counts = {
        key: _require_count(checkpoint[key], key)
        for key in (
            "processed_count",
            "imported_count",
            "skipped_count",
            "rejected_count",
            "child_job_count",
            "batch_count",
        )
    }
    processed = counts["processed_count"]
    if not (
        counts["imported_count"] + counts["skipped_count"] <= processed
        and counts["rejected_count"] <= processed
        and counts["child_job_count"] <= processed
        and counts["batch_count"] <= processed
        and (processed == 0) == (cursor is None)
        and (processed == 0) == (counts["batch_count"] == 0)
    ):
        raise LibraryScanValidationError("checkpoint_invalid")
    return checkpoint


def validate_library_scan_error(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LibraryScanIntegrityError("library_scan_error_invalid")
    error = dict(value)
    if (
        set(error) != ERROR_KEYS
        or error["object"] != ERROR_OBJECT
        or error["schema_version"] != "1"
        or error["code"]
        not in {
            "worker_interrupted",
            "user_cancelled",
            "batch_failed",
            "root_binding_changed",
            "checkpoint_invalid",
            "authority_invalid",
            "unsupported_platform",
        }
        or error["stage"] not in {"awaiting_scan_worker", "scanning"}
        or type(error["retryable"]) is not bool
        or not _is_lower_hex(error["detail_sha256"], 64)
    ):
        raise LibraryScanIntegrityError("library_scan_error_invalid")
    expected_retryable = error["code"] in {
        "worker_interrupted",
        "user_cancelled",
        "batch_failed",
    }
    if error["retryable"] is not expected_retryable:
        raise LibraryScanIntegrityError("library_scan_error_invalid")
    return error


def _library_scan_error(*, code: str, stage: str, detail: str) -> dict[str, object]:
    retryable = code in {"worker_interrupted", "user_cancelled", "batch_failed"}
    return {
        "object": ERROR_OBJECT,
        "schema_version": "1",
        "code": code,
        "stage": stage,
        "retryable": retryable,
        "detail_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
    }


def _decode_json_object(
    raw: object,
    code: str,
    *,
    max_bytes: int,
) -> dict[str, object]:
    if type(raw) is not str or len(raw.encode("utf-8")) > max_bytes:
        raise LibraryScanIntegrityError(code)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LibraryScanIntegrityError(code) from exc
    if not isinstance(value, dict) or _canonical_json(value) != raw:
        raise LibraryScanIntegrityError(code)
    return value


def _parse_utc_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("+00:00"):
        raise LibraryScanIntegrityError("library_scan_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LibraryScanIntegrityError("library_scan_timestamp_invalid") from exc
    if parsed.tzinfo != timezone.utc or parsed.isoformat() != value:
        raise LibraryScanIntegrityError("library_scan_timestamp_invalid")
    return parsed


class LibraryScanPersistenceMixin:
    """Mixed into ``MediaRepository``; methods are added in closed blocks below."""

    @staticmethod
    def _library_scan_runtime_generation(value: object) -> str:
        prefix = "runtime_generation_"
        if (
            type(value) is not str
            or not value.startswith(prefix)
            or not _is_lower_hex(value[len(prefix) :], 64)
        ):
            raise LibraryScanValidationError("authority_invalid")
        return value

    @staticmethod
    def _library_scan_expected_attempt(value: object) -> int:
        if type(value) is not int or not 1 <= value <= MAX_COUNT:
            raise LibraryScanValidationError("authority_invalid")
        return value

    def _reserve_library_scan_runtime_lease(
        self,
        *,
        job_id: str,
        attempt: int,
        runtime_generation_id: str,
    ) -> bool:
        with self._library_scan_runtime_leases_lock:
            key = (job_id, attempt)
            observed = self._library_scan_runtime_leases.get(key)
            if observed is not None:
                raise LibraryScanConflictError("library_scan_runtime_generation_conflict")
            self._library_scan_runtime_leases[key] = runtime_generation_id
            return True

    def _require_library_scan_runtime_lease(
        self,
        *,
        job_id: str,
        attempt: int,
        runtime_generation_id: str,
    ) -> None:
        generation = self._library_scan_runtime_generation(runtime_generation_id)
        with self._library_scan_runtime_leases_lock:
            observed = self._library_scan_runtime_leases.get((job_id, attempt))
        if observed != generation:
            raise LibraryScanConflictError("library_scan_runtime_generation_conflict")

    def _clear_library_scan_runtime_lease(self, job_id: str, attempt: int) -> None:
        with self._library_scan_runtime_leases_lock:
            self._library_scan_runtime_leases.pop((job_id, attempt), None)

    def _library_scan_row(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> sqlite3.Row:
        receipts = connection.execute(
            "SELECT * FROM library_bootstrap_receipts WHERE scan_job_id=?",
            (job_id,),
        ).fetchall()
        if len(receipts) != 1:
            raise LibraryScanIntegrityError("library_scan_receipt_binding_invalid")
        try:
            self._validate_library_bootstrap_receipt(connection, receipts[0])
        except Exception as exc:
            if isinstance(exc, LibraryScanIntegrityError):
                raise
            raise LibraryScanIntegrityError(
                "library_scan_receipt_binding_invalid"
            ) from exc
        rows = connection.execute(
            """SELECT
                 receipt.request_id,receipt.scan_job_id,receipt.database_uuid,
                 receipt.library_root_id,receipt.library_root_fingerprint,
                 receipt.library_root_device,receipt.library_root_inode,
                 root.canonical_path,root.permission_fingerprint,root.status AS root_status,
                 job.kind,job.asset_id,job.analysis_run_id,job.status,job.stage,
                 job.progress,job.attempt,job.cancel_requested,job.checkpoint_json,
                 job.error_json,job.created_at,job.started_at,job.heartbeat_at,job.finished_at
               FROM library_bootstrap_receipts receipt
               JOIN library_roots root ON root.id=receipt.library_root_id
               JOIN media_jobs job
                 ON job.id=receipt.scan_job_id
                AND job.database_uuid=receipt.database_uuid
              WHERE receipt.scan_job_id=?""",
            (job_id,),
        ).fetchall()
        if len(rows) != 1:
            raise LibraryScanIntegrityError("library_scan_receipt_binding_invalid")
        row = rows[0]
        if (
            row["scan_job_id"] != job_id
            or row["kind"] != "library_scan"
            or row["asset_id"] is not None
            or row["analysis_run_id"] is not None
            or row["root_status"] != "active"
            or row["permission_fingerprint"] != row["library_root_fingerprint"]
            or not _is_lower_hex(row["library_root_fingerprint"], 64)
            or type(row["library_root_device"]) is not int
            or type(row["library_root_inode"]) is not int
            or int(row["library_root_device"]) < 0
            or int(row["library_root_inode"]) < 0
            or type(row["canonical_path"]) is not str
            or not os.path.isabs(str(row["canonical_path"]))
        ):
            raise LibraryScanIntegrityError("library_scan_receipt_binding_invalid")
        return row

    @staticmethod
    def _library_scan_checkpoint_for_row(row: sqlite3.Row) -> dict[str, object]:
        checkpoint = _decode_json_object(
            row["checkpoint_json"],
            "library_scan_checkpoint_invalid",
            max_bytes=16_384,
        )
        try:
            checkpoint = validate_library_scan_checkpoint(checkpoint)
        except LibraryScanValidationError as exc:
            raise LibraryScanIntegrityError("library_scan_checkpoint_invalid") from exc
        if checkpoint and (
            checkpoint["root_permission_fingerprint"]
            != row["library_root_fingerprint"]
        ):
            raise LibraryScanIntegrityError("library_scan_checkpoint_invalid")
        return checkpoint

    @staticmethod
    def _library_scan_error_for_row(row: sqlite3.Row) -> dict[str, object] | None:
        raw = row["error_json"]
        error = None if raw is None else _decode_json_object(
            raw,
            "library_scan_error_invalid",
            max_bytes=2_048,
        )
        return validate_library_scan_error(error)

    @classmethod
    def _validate_library_scan_state(
        cls,
        row: sqlite3.Row,
        checkpoint: dict[str, object],
        error: dict[str, object] | None,
    ) -> None:
        pair = (row["status"], row["stage"])
        allowed = {
            ("queued", "awaiting_scan_worker"),
            ("running", "scanning"),
            ("cancelling", "scanning"),
            ("interrupted", "interrupted"),
            ("cancelled", "cancelled"),
            ("failed", "failed"),
            ("succeeded", "scan_complete"),
            ("succeeded", "no_supported_media"),
        }
        cancel_requested = row["cancel_requested"]
        progress = row["progress"]
        status = str(row["status"])
        if (
            pair not in allowed
            or type(row["attempt"]) is not int
            or not 1 <= int(row["attempt"]) <= MAX_COUNT
            or cancel_requested not in {0, 1}
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(float(progress))
            or not 0.0 <= float(progress) <= 1.0
            or (status == "succeeded" and float(progress) != 1.0)
            or (status != "succeeded" and float(progress) >= 1.0)
        ):
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        stage = str(row["stage"])
        if (status in {"cancelling", "cancelled"}) != bool(cancel_requested):
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        if status in {"queued", "running", "cancelling", "succeeded"}:
            if error is not None:
                raise LibraryScanIntegrityError("library_scan_state_invalid")
        elif error is None:
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        if status == "interrupted" and error is not None and (
            error["code"] != "worker_interrupted" or error["retryable"] is not True
        ):
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        if status == "cancelled" and error is not None and (
            error["code"] != "user_cancelled" or error["retryable"] is not True
        ):
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        if status == "failed" and error is not None and error["code"] not in {
            "batch_failed",
            "root_binding_changed",
            "checkpoint_invalid",
            "authority_invalid",
            "unsupported_platform",
        }:
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        if checkpoint == {} and pair != ("queued", "awaiting_scan_worker"):
            raise LibraryScanIntegrityError("library_scan_checkpoint_invalid")
        if stage == "no_supported_media" and checkpoint != {} and any(
            checkpoint[key] != 0
            for key in (
                "processed_count",
                "imported_count",
                "skipped_count",
                "rejected_count",
                "child_job_count",
                "batch_count",
            )
        ):
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        if stage == "scan_complete" and checkpoint != {} and (
            checkpoint["processed_count"] == 0
        ):
            raise LibraryScanIntegrityError("library_scan_state_invalid")
        created = _parse_utc_timestamp(row["created_at"])
        started = row["started_at"]
        heartbeat = row["heartbeat_at"]
        finished = row["finished_at"]
        if status == "queued":
            if started is not None or heartbeat is not None or finished is not None:
                raise LibraryScanIntegrityError("library_scan_state_invalid")
        elif status in {"running", "cancelling"}:
            if started is None or heartbeat is None or finished is not None:
                raise LibraryScanIntegrityError("library_scan_state_invalid")
            if not (
                created <= _parse_utc_timestamp(started)
                <= _parse_utc_timestamp(heartbeat)
            ):
                raise LibraryScanIntegrityError("library_scan_timestamp_invalid")
        elif status in {"interrupted", "cancelled", "failed", "succeeded"}:
            preclaim_terminal = started is None and status in {"cancelled", "failed"}
            if heartbeat is None or finished is None or (
                not preclaim_terminal and started is None
            ):
                raise LibraryScanIntegrityError("library_scan_state_invalid")
            heartbeat_at = _parse_utc_timestamp(heartbeat)
            finished_at = _parse_utc_timestamp(finished)
            if preclaim_terminal:
                if not created <= heartbeat_at <= finished_at:
                    raise LibraryScanIntegrityError("library_scan_timestamp_invalid")
            elif not (
                created
                <= _parse_utc_timestamp(started)
                <= heartbeat_at
                <= finished_at
            ):
                raise LibraryScanIntegrityError("library_scan_timestamp_invalid")
        if error is not None:
            expected_error_stage = (
                "awaiting_scan_worker"
                if started is None and status in {"cancelled", "failed"}
                else "scanning"
            )
            if error["stage"] != expected_error_stage:
                raise LibraryScanIntegrityError("library_scan_state_invalid")

    @classmethod
    def _library_scan_context_from_row(cls, row: sqlite3.Row) -> dict[str, object]:
        checkpoint = cls._library_scan_checkpoint_for_row(row)
        error = cls._library_scan_error_for_row(row)
        cls._validate_library_scan_state(row, checkpoint, error)
        context: dict[str, object] = {
            "object": CONTEXT_OBJECT,
            "schema_version": "1",
            "request_id": str(row["request_id"]),
            "job_id": str(row["scan_job_id"]),
            "database_uuid": str(row["database_uuid"]),
            "library_root_id": str(row["library_root_id"]),
            "canonical_root": str(row["canonical_path"]),
            "expected_library_root_device": int(row["library_root_device"]),
            "expected_library_root_inode": int(row["library_root_inode"]),
            "root_permission_fingerprint": str(row["library_root_fingerprint"]),
            "status": str(row["status"]),
            "stage": str(row["stage"]),
            "attempt": int(row["attempt"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "checkpoint": checkpoint,
            "error": error,
        }
        if set(context) != CONTEXT_KEYS:
            raise LibraryScanIntegrityError("library_scan_context_invalid")
        return context

    def get_library_scan_context(self, job_id: str) -> dict[str, object]:
        with self._managed_connection() as connection:
            row = self._library_scan_row(connection, job_id)
            return self._library_scan_context_from_row(row)

    def list_recoverable_library_scans(self) -> list[dict[str, object]]:
        with self._managed_connection() as connection:
            ids = connection.execute(
                """SELECT receipt.scan_job_id
                     FROM library_bootstrap_receipts receipt
                     JOIN media_jobs job ON job.id=receipt.scan_job_id
                    WHERE job.status IN ('queued','running','cancelling','interrupted')
                    ORDER BY job.created_at,job.id"""
            ).fetchall()
            return [
                self._library_scan_context_from_row(
                    self._library_scan_row(connection, str(item["scan_job_id"]))
                )
                for item in ids
            ]

    @staticmethod
    def _require_expected_checkpoint(
        row: sqlite3.Row,
        expected_checkpoint: Mapping[str, object],
    ) -> dict[str, object]:
        actual = LibraryScanPersistenceMixin._library_scan_checkpoint_for_row(row)
        try:
            expected = validate_library_scan_checkpoint(expected_checkpoint)
        except LibraryScanValidationError as exc:
            raise LibraryScanValidationError("checkpoint_invalid") from exc
        if _canonical_json(actual) != _canonical_json(expected):
            raise LibraryScanConflictError("library_scan_checkpoint_conflict")
        return actual

    @staticmethod
    def _open_bound_root(path: Path) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory is None:
            raise LibraryScanValidationError("unsupported_platform")
        flags = os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)
        current = os.open(path.anchor, flags)
        try:
            for component in path.parts[1:]:
                following = os.open(component, flags, dir_fd=current)
                os.close(current)
                current = following
            return current
        except Exception:
            os.close(current)
            raise

    @classmethod
    def _verify_library_scan_root_fd(cls, row: sqlite3.Row, library_root_fd: int) -> None:
        if type(library_root_fd) is not int:
            raise LibraryScanValidationError("root_binding_changed")
        current: int | None = None
        try:
            held = os.fstat(library_root_fd)
            path = Path(str(row["canonical_path"]))
            current = cls._open_bound_root(path)
            named = os.fstat(current)
            expected = (int(row["library_root_device"]), int(row["library_root_inode"]))
            fingerprint = hashlib.sha256(
                f"{path}\0{held.st_dev}\0{held.st_ino}\0{held.st_mode}".encode()
            ).hexdigest()
            if not (
                stat.S_ISDIR(held.st_mode)
                and stat.S_ISDIR(named.st_mode)
                and (int(held.st_dev), int(held.st_ino)) == expected
                and (int(named.st_dev), int(named.st_ino)) == expected
                and fingerprint == row["library_root_fingerprint"]
            ):
                raise LibraryScanValidationError("root_binding_changed")
        except LibraryScanValidationError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise LibraryScanValidationError("root_binding_changed") from exc
        finally:
            if current is not None:
                os.close(current)

    @staticmethod
    def _zero_library_scan_checkpoint(root_fingerprint: str) -> dict[str, object]:
        return {
            "object": CHECKPOINT_OBJECT,
            "schema_version": "1",
            "root_permission_fingerprint": root_fingerprint,
            "last_relative_path": None,
            "processed_count": 0,
            "imported_count": 0,
            "skipped_count": 0,
            "rejected_count": 0,
            "child_job_count": 0,
            "batch_count": 0,
        }

    def claim_library_scan(
        self,
        *,
        job_id: str,
        runtime_generation_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
    ) -> dict[str, object]:
        generation = self._library_scan_runtime_generation(runtime_generation_id)
        attempt = self._library_scan_expected_attempt(expected_attempt)
        reserved = self._reserve_library_scan_runtime_lease(
            job_id=job_id,
            attempt=attempt,
            runtime_generation_id=generation,
        )
        context: dict[str, object]
        try:
            with self.transaction(immediate=True) as connection:
                row = self._library_scan_row(connection, job_id)
                checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
                if int(row["attempt"]) != attempt:
                    raise LibraryScanConflictError("library_scan_attempt_conflict")
                if (
                    (row["status"], row["stage"])
                    != ("queued", "awaiting_scan_worker")
                    or bool(row["cancel_requested"])
                    or row["error_json"] is not None
                ):
                    raise LibraryScanConflictError()
                self._verify_library_scan_root_fd(row, library_root_fd)
                claimed = checkpoint or self._zero_library_scan_checkpoint(
                    str(row["library_root_fingerprint"])
                )
                now = _utc_now_iso()
                cursor = connection.execute(
                    """UPDATE media_jobs
                          SET status='running',stage='scanning',checkpoint_json=?,
                              heartbeat_at=?,started_at=COALESCE(started_at,?)
                        WHERE id=? AND kind='library_scan' AND status='queued'
                          AND stage='awaiting_scan_worker' AND attempt=?
                          AND checkpoint_json=? AND cancel_requested=0""",
                    (
                        _canonical_json(claimed),
                        now,
                        now,
                        job_id,
                        attempt,
                        _canonical_json(checkpoint),
                    ),
                )
                if cursor.rowcount != 1:
                    raise LibraryScanConflictError()
                context = self._library_scan_context_from_row(
                    self._library_scan_row(connection, job_id)
                )
        except Exception:
            if reserved:
                self._clear_library_scan_runtime_lease(job_id, attempt)
            raise
        return context

    @staticmethod
    def _library_scan_batch_paths(
        relative_paths: Sequence[str],
        checkpoint: Mapping[str, object],
    ) -> tuple[tuple[str, ...], tuple[bytes, ...]]:
        if (
            isinstance(relative_paths, (str, bytes))
            or not 1 <= len(relative_paths) <= MAX_BATCH_ENTRIES
        ):
            raise LibraryScanValidationError("library_scan_batch_invalid")
        values = tuple(relative_paths)
        encoded = tuple(_relative_path_bytes(value) for value in values)
        if any(left >= right for left, right in zip(encoded, encoded[1:])):
            raise LibraryScanValidationError("library_scan_batch_invalid")
        cursor = checkpoint["last_relative_path"]
        if cursor is not None and _relative_path_bytes(cursor) >= encoded[0]:
            raise LibraryScanConflictError("library_scan_cursor_conflict")
        return values, encoded

    @staticmethod
    def _library_scan_batch_result(
        value: object,
        *,
        processed_count: int,
    ) -> tuple[int, int, int, tuple[str, ...]]:
        keys = {
            "processed_count",
            "imported_count",
            "skipped_count",
            "rejected_count",
            "child_job_ids",
        }
        if not isinstance(value, Mapping) or set(value) != keys:
            raise LibraryScanValidationError("library_scan_batch_result_invalid")
        observed_processed = _require_count(value["processed_count"], "processed_count")
        imported = _require_count(value["imported_count"], "imported_count")
        skipped = _require_count(value["skipped_count"], "skipped_count")
        rejected = _require_count(value["rejected_count"], "rejected_count")
        raw_ids = value["child_job_ids"]
        if (
            observed_processed != processed_count
            or imported + skipped > processed_count
            or rejected > processed_count
            or not isinstance(raw_ids, (list, tuple))
            or len(raw_ids) > processed_count
        ):
            raise LibraryScanValidationError("library_scan_batch_result_invalid")
        child_ids = tuple(raw_ids)
        if len(set(child_ids)) != len(child_ids) or any(
            type(job_id) is not str
            or not job_id.startswith("job_")
            or not _is_lower_hex(job_id[4:], 32)
            for job_id in child_ids
        ):
            raise LibraryScanValidationError("library_scan_batch_result_invalid")
        return imported, skipped, rejected, child_ids

    @staticmethod
    def _validate_library_scan_child_jobs(
        connection: sqlite3.Connection,
        *,
        child_job_ids: Sequence[str],
        database_uuid: str,
        library_root_id: str,
        relative_paths: Sequence[str],
        previous_max_rowid: int,
    ) -> None:
        admitted_paths = set(relative_paths)
        for job_id in child_job_ids:
            job = connection.execute(
                """SELECT rowid,database_uuid,kind,status,cancel_requested,asset_id
                     FROM media_jobs WHERE id=?""",
                (job_id,),
            ).fetchone()
            source_bound = (
                connection.execute(
                    """SELECT 1 FROM asset_sources
                        WHERE asset_id=? AND library_root_id=? AND relative_path=?""",
                    (job["asset_id"], library_root_id, relative_path),
                ).fetchone()
                if job is not None
                else None
                for relative_path in admitted_paths
            )
            if job is None or not (
                int(job["rowid"]) > previous_max_rowid
                and job["database_uuid"] == database_uuid
                and job["kind"] in {"image_analysis", "video_index"}
                and job["status"] == "queued"
                and not bool(job["cancel_requested"])
                and any(source_bound)
            ):
                raise LibraryScanIntegrityError("library_scan_child_job_invalid")

    def commit_library_scan_batch(
        self,
        *,
        job_id: str,
        runtime_generation_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
        relative_paths: Sequence[str],
        apply_batch: Callable[[sqlite3.Connection], Mapping[str, object]],
    ) -> dict[str, object]:
        generation = self._library_scan_runtime_generation(runtime_generation_id)
        attempt = self._library_scan_expected_attempt(expected_attempt)
        if not callable(apply_batch):
            raise LibraryScanValidationError("library_scan_batch_invalid")
        self._require_library_scan_runtime_lease(
            job_id=job_id,
            attempt=attempt,
            runtime_generation_id=generation,
        )
        child_job_ids: tuple[str, ...]
        context: dict[str, object]
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            if int(row["attempt"]) != attempt:
                raise LibraryScanConflictError("library_scan_attempt_conflict")
            if (
                (row["status"], row["stage"]) != ("running", "scanning")
                or bool(row["cancel_requested"])
                or not checkpoint
            ):
                raise LibraryScanConflictError()
            paths, encoded = self._library_scan_batch_paths(relative_paths, checkpoint)
            self._verify_library_scan_root_fd(row, library_root_fd)
            before = connection.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM media_jobs"
            ).fetchone()
            previous_max_rowid = int(before[0]) if before is not None else 0
            result = apply_batch(connection)
            imported, skipped, rejected, child_job_ids = (
                self._library_scan_batch_result(result, processed_count=len(paths))
            )
            self._validate_library_scan_child_jobs(
                connection,
                child_job_ids=child_job_ids,
                database_uuid=str(row["database_uuid"]),
                library_root_id=str(row["library_root_id"]),
                relative_paths=paths,
                previous_max_rowid=previous_max_rowid,
            )
            self._verify_library_scan_root_fd(row, library_root_fd)
            advanced = dict(checkpoint)
            increments = {
                "processed_count": len(paths),
                "imported_count": imported,
                "skipped_count": skipped,
                "rejected_count": rejected,
                "child_job_count": len(child_job_ids),
                "batch_count": 1,
            }
            for key, increment in increments.items():
                current = int(advanced[key])
                if current > MAX_COUNT - increment:
                    raise LibraryScanValidationError("library_scan_count_overflow")
                advanced[key] = current + increment
            advanced["last_relative_path"] = paths[-1]
            validate_library_scan_checkpoint(advanced, allow_initial=False)
            now = _utc_now_iso()
            changed = connection.execute(
                """UPDATE media_jobs SET checkpoint_json=?,heartbeat_at=?
                    WHERE id=? AND kind='library_scan' AND status='running'
                      AND stage='scanning' AND attempt=? AND cancel_requested=0
                      AND checkpoint_json=?""",
                (
                    _canonical_json(advanced),
                    now,
                    job_id,
                    attempt,
                    _canonical_json(checkpoint),
                ),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        return {"scan": context, "child_job_ids": list(child_job_ids)}

    def request_library_scan_cancel(
        self,
        *,
        job_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
        runtime_generation_id: str | None = None,
    ) -> dict[str, object]:
        attempt = self._library_scan_expected_attempt(expected_attempt)
        clear_lease = False
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            if int(row["attempt"]) != attempt:
                raise LibraryScanConflictError("library_scan_attempt_conflict")
            status = str(row["status"])
            if status == "running":
                if runtime_generation_id is None:
                    raise LibraryScanValidationError("authority_invalid")
                self._require_library_scan_runtime_lease(
                    job_id=job_id,
                    attempt=attempt,
                    runtime_generation_id=runtime_generation_id,
                )
            elif status != "queued":
                raise LibraryScanConflictError()
            self._verify_library_scan_root_fd(row, library_root_fd)
            now = _utc_now_iso()
            if status == "running":
                changed = connection.execute(
                    """UPDATE media_jobs SET status='cancelling',cancel_requested=1,
                              heartbeat_at=?
                        WHERE id=? AND kind='library_scan' AND status='running'
                          AND stage='scanning' AND attempt=? AND checkpoint_json=?
                          AND cancel_requested=0""",
                    (now, job_id, attempt, _canonical_json(checkpoint)),
                )
            else:
                terminal_checkpoint = checkpoint or self._zero_library_scan_checkpoint(
                    str(row["library_root_fingerprint"])
                )
                error = _library_scan_error(
                    code="user_cancelled",
                    stage=("awaiting_scan_worker" if status == "queued" else "scanning"),
                    detail="user_cancel_requested",
                )
                changed = connection.execute(
                    """UPDATE media_jobs SET status='cancelled',stage='cancelled',
                              checkpoint_json=?,cancel_requested=1,error_json=?,
                              heartbeat_at=?,finished_at=?
                        WHERE id=? AND kind='library_scan' AND status=? AND attempt=?
                          AND checkpoint_json=?""",
                    (
                        _canonical_json(terminal_checkpoint),
                        _canonical_json(error),
                        now,
                        now,
                        job_id,
                        status,
                        attempt,
                        _canonical_json(checkpoint),
                    ),
                )
                clear_lease = True
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        if clear_lease:
            self._clear_library_scan_runtime_lease(job_id, attempt)
        return context

    def finish_library_scan_cancel(
        self,
        *,
        job_id: str,
        runtime_generation_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
    ) -> dict[str, object]:
        attempt = self._library_scan_expected_attempt(expected_attempt)
        self._require_library_scan_runtime_lease(
            job_id=job_id,
            attempt=attempt,
            runtime_generation_id=runtime_generation_id,
        )
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            if int(row["attempt"]) != attempt or (
                row["status"], row["stage"], bool(row["cancel_requested"])
            ) != ("cancelling", "scanning", True):
                raise LibraryScanConflictError()
            self._verify_library_scan_root_fd(row, library_root_fd)
            now = _utc_now_iso()
            error = _library_scan_error(
                code="user_cancelled",
                stage="scanning",
                detail="user_cancel_requested",
            )
            changed = connection.execute(
                """UPDATE media_jobs SET status='cancelled',stage='cancelled',
                          error_json=?,heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='library_scan' AND status='cancelling'
                      AND stage='scanning' AND attempt=? AND checkpoint_json=?
                      AND cancel_requested=1""",
                (
                    _canonical_json(error),
                    now,
                    now,
                    job_id,
                    attempt,
                    _canonical_json(checkpoint),
                ),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        self._clear_library_scan_runtime_lease(job_id, attempt)
        return context

    def interrupt_library_scan(
        self,
        *,
        job_id: str,
        runtime_generation_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
        detail: str = "process_interrupted",
    ) -> dict[str, object]:
        attempt = self._library_scan_expected_attempt(expected_attempt)
        if type(detail) is not str or not detail:
            raise LibraryScanValidationError("library_scan_error_invalid")
        self._require_library_scan_runtime_lease(
            job_id=job_id,
            attempt=attempt,
            runtime_generation_id=runtime_generation_id,
        )
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            if int(row["attempt"]) != attempt or (
                row["status"], row["stage"]
            ) not in {("running", "scanning"), ("cancelling", "scanning")}:
                raise LibraryScanConflictError()
            self._verify_library_scan_root_fd(row, library_root_fd)
            cancelling = bool(row["cancel_requested"])
            now = _utc_now_iso()
            code = "user_cancelled" if cancelling else "worker_interrupted"
            status = "cancelled" if cancelling else "interrupted"
            stage = "cancelled" if cancelling else "interrupted"
            error = _library_scan_error(code=code, stage="scanning", detail=detail)
            changed = connection.execute(
                """UPDATE media_jobs SET status=?,stage=?,error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='library_scan' AND status=?
                      AND stage='scanning' AND attempt=? AND checkpoint_json=?
                      AND cancel_requested=?""",
                (
                    status,
                    stage,
                    _canonical_json(error),
                    now,
                    now,
                    job_id,
                    row["status"],
                    attempt,
                    _canonical_json(checkpoint),
                    int(cancelling),
                ),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        self._clear_library_scan_runtime_lease(job_id, attempt)
        return context

    def resume_library_scan(
        self,
        *,
        job_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
    ) -> dict[str, object]:
        attempt = self._library_scan_expected_attempt(expected_attempt)
        if attempt >= MAX_COUNT:
            raise LibraryScanValidationError("library_scan_attempt_overflow")
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            error = self._library_scan_error_for_row(row)
            if (
                int(row["attempt"]) != attempt
                or row["status"] not in {"interrupted", "cancelled", "failed"}
                or error is None
                or error["retryable"] is not True
                or not checkpoint
            ):
                raise LibraryScanConflictError()
            self._verify_library_scan_root_fd(row, library_root_fd)
            changed = connection.execute(
                """UPDATE media_jobs SET status='queued',stage='awaiting_scan_worker',
                          attempt=attempt+1,cancel_requested=0,error_json=NULL,
                          started_at=NULL,heartbeat_at=NULL,finished_at=NULL
                    WHERE id=? AND kind='library_scan' AND status=? AND attempt=?
                      AND checkpoint_json=?""",
                (job_id, row["status"], attempt, _canonical_json(checkpoint)),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            return self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )

    def complete_library_scan(
        self,
        *,
        job_id: str,
        runtime_generation_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
        no_supported_media: bool,
    ) -> dict[str, object]:
        attempt = self._library_scan_expected_attempt(expected_attempt)
        if type(no_supported_media) is not bool:
            raise LibraryScanValidationError("library_scan_completion_invalid")
        self._require_library_scan_runtime_lease(
            job_id=job_id,
            attempt=attempt,
            runtime_generation_id=runtime_generation_id,
        )
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            if (
                int(row["attempt"]) != attempt
                or (row["status"], row["stage"]) != ("running", "scanning")
                or bool(row["cancel_requested"])
                or not checkpoint
            ):
                raise LibraryScanConflictError()
            zero = int(checkpoint["processed_count"]) == 0
            if no_supported_media != zero:
                raise LibraryScanValidationError("library_scan_completion_invalid")
            self._verify_library_scan_root_fd(row, library_root_fd)
            now = _utc_now_iso()
            stage = "no_supported_media" if no_supported_media else "scan_complete"
            changed = connection.execute(
                """UPDATE media_jobs SET status='succeeded',stage=?,progress=1,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='library_scan' AND status='running'
                      AND stage='scanning' AND attempt=? AND checkpoint_json=?
                      AND cancel_requested=0""",
                (stage, now, now, job_id, attempt, _canonical_json(checkpoint)),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        self._clear_library_scan_runtime_lease(job_id, attempt)
        return context

    def fail_library_scan(
        self,
        *,
        job_id: str,
        runtime_generation_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        library_root_fd: int,
        detail: str,
    ) -> dict[str, object]:
        attempt = self._library_scan_expected_attempt(expected_attempt)
        if type(detail) is not str or not detail:
            raise LibraryScanValidationError("library_scan_error_invalid")
        self._require_library_scan_runtime_lease(
            job_id=job_id,
            attempt=attempt,
            runtime_generation_id=runtime_generation_id,
        )
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._require_expected_checkpoint(row, expected_checkpoint)
            if (
                int(row["attempt"]) != attempt
                or (row["status"], row["stage"]) != ("running", "scanning")
                or bool(row["cancel_requested"])
                or not checkpoint
            ):
                raise LibraryScanConflictError()
            self._verify_library_scan_root_fd(row, library_root_fd)
            now = _utc_now_iso()
            error = _library_scan_error(
                code="batch_failed",
                stage="scanning",
                detail=detail,
            )
            changed = connection.execute(
                """UPDATE media_jobs SET status='failed',stage='failed',error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='library_scan' AND status='running'
                      AND stage='scanning' AND attempt=? AND checkpoint_json=?
                      AND cancel_requested=0""",
                (
                    _canonical_json(error),
                    now,
                    now,
                    job_id,
                    attempt,
                    _canonical_json(checkpoint),
                ),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        self._clear_library_scan_runtime_lease(job_id, attempt)
        return context

    def invalidate_library_scan(
        self,
        *,
        job_id: str,
        expected_attempt: int,
        expected_checkpoint: Mapping[str, object],
        code: str,
        detail: str,
        expected_checkpoint_raw_sha256: str | None = None,
    ) -> dict[str, object]:
        closed_codes = {
            "root_binding_changed",
            "checkpoint_invalid",
            "authority_invalid",
            "unsupported_platform",
        }
        attempt = self._library_scan_expected_attempt(expected_attempt)
        if code not in closed_codes or type(detail) is not str or not detail:
            raise LibraryScanValidationError("library_scan_error_invalid")
        expected: dict[str, object] | None = None
        if code == "checkpoint_invalid":
            if not isinstance(expected_checkpoint, Mapping):
                raise LibraryScanValidationError("checkpoint_invalid")
            if expected_checkpoint_raw_sha256 is not None and not _is_lower_hex(
                expected_checkpoint_raw_sha256,
                64,
            ):
                raise LibraryScanValidationError("checkpoint_invalid")
        else:
            try:
                expected = validate_library_scan_checkpoint(expected_checkpoint)
            except LibraryScanValidationError as exc:
                raise LibraryScanValidationError("checkpoint_invalid") from exc
        with self.transaction(immediate=True) as connection:
            row = self._library_scan_row(connection, job_id)
            if int(row["attempt"]) != attempt or row["status"] not in {
                "queued",
                "running",
                "interrupted",
            }:
                raise LibraryScanConflictError()
            if code == "checkpoint_invalid":
                raw_checkpoint = str(row["checkpoint_json"])
                supplied_matches = False
                try:
                    supplied_matches = (
                        _canonical_json(dict(expected_checkpoint)) == raw_checkpoint
                    )
                except (TypeError, ValueError):
                    supplied_matches = False
                if expected_checkpoint_raw_sha256 is not None:
                    supplied_matches = supplied_matches or (
                        hashlib.sha256(raw_checkpoint.encode("utf-8")).hexdigest()
                        == expected_checkpoint_raw_sha256
                    )
                if not supplied_matches:
                    raise LibraryScanConflictError(
                        "library_scan_checkpoint_conflict"
                    )
                checkpoint = self._zero_library_scan_checkpoint(
                    str(row["library_root_fingerprint"])
                )
            else:
                checkpoint = self._library_scan_checkpoint_for_row(row)
                assert expected is not None
                if _canonical_json(checkpoint) != _canonical_json(expected):
                    raise LibraryScanConflictError("library_scan_checkpoint_conflict")
            terminal_checkpoint = checkpoint or self._zero_library_scan_checkpoint(
                str(row["library_root_fingerprint"])
            )
            validate_library_scan_checkpoint(terminal_checkpoint, allow_initial=False)
            causal_stage = (
                "awaiting_scan_worker" if row["status"] == "queued" else "scanning"
            )
            error = _library_scan_error(code=code, stage=causal_stage, detail=detail)
            now = _utc_now_iso()
            changed = connection.execute(
                """UPDATE media_jobs SET status='failed',stage='failed',
                          checkpoint_json=?,cancel_requested=0,error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='library_scan' AND status=? AND attempt=?
                      AND checkpoint_json=?""",
                (
                    _canonical_json(terminal_checkpoint),
                    _canonical_json(error),
                    now,
                    now,
                    job_id,
                    row["status"],
                    attempt,
                    row["checkpoint_json"],
                ),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            context = self._library_scan_context_from_row(
                self._library_scan_row(connection, job_id)
            )
        self._clear_library_scan_runtime_lease(job_id, attempt)
        return context

    def _recover_library_scans_in_transaction(
        self,
        connection: sqlite3.Connection,
    ) -> list[tuple[str, int]]:
        ids = connection.execute(
            """SELECT receipt.scan_job_id
                 FROM library_bootstrap_receipts receipt
                 JOIN media_jobs job ON job.id=receipt.scan_job_id
                WHERE job.status IN ('running','cancelling')
                ORDER BY job.id"""
        ).fetchall()
        recovered: list[tuple[str, int]] = []
        for item in ids:
            job_id = str(item["scan_job_id"])
            row = self._library_scan_row(connection, job_id)
            checkpoint = self._library_scan_checkpoint_for_row(row)
            error = self._library_scan_error_for_row(row)
            self._validate_library_scan_state(row, checkpoint, error)
            attempt = int(row["attempt"])
            cancelling = row["status"] == "cancelling"
            now = _utc_now_iso()
            code = "user_cancelled" if cancelling else "worker_interrupted"
            status = "cancelled" if cancelling else "interrupted"
            stage = "cancelled" if cancelling else "interrupted"
            typed_error = _library_scan_error(
                code=code,
                stage="scanning",
                detail=("user_cancel_requested" if cancelling else "process_interrupted"),
            )
            changed = connection.execute(
                """UPDATE media_jobs SET status=?,stage=?,error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='library_scan' AND status=?
                      AND stage='scanning' AND attempt=? AND checkpoint_json=?
                      AND cancel_requested=?""",
                (
                    status,
                    stage,
                    _canonical_json(typed_error),
                    now,
                    now,
                    job_id,
                    row["status"],
                    attempt,
                    _canonical_json(checkpoint),
                    int(cancelling),
                ),
            )
            if changed.rowcount != 1:
                raise LibraryScanConflictError()
            self._library_scan_context_from_row(self._library_scan_row(connection, job_id))
            recovered.append((job_id, attempt))
        return recovered

    @staticmethod
    def _library_scan_job_projection(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": str(row["scan_job_id"]),
            "status": str(row["status"]),
            "stage": str(row["stage"]),
            "progress": float(row["progress"]),
            "attempt": int(row["attempt"]),
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": str(row["created_at"]),
            "started_at": (
                str(row["started_at"]) if row["started_at"] is not None else None
            ),
            "heartbeat_at": (
                str(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None
            ),
            "finished_at": (
                str(row["finished_at"]) if row["finished_at"] is not None else None
            ),
        }

    def get_library_scan_public_job(self, job_id: str) -> dict[str, object]:
        """Return a path-free row only after the exact receipt/context validates."""

        with self._managed_connection() as connection:
            row = self._library_scan_row(connection, job_id)
            context = self._library_scan_context_from_row(row)
            return {
                "context": context,
                "job": self._library_scan_job_projection(row),
            }

    def list_library_scan_public_jobs(
        self,
        *,
        active: bool,
        limit: int,
    ) -> list[dict[str, object]]:
        if type(active) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise LibraryScanValidationError("library_scan_list_invalid")
        where = (
            "AND job.status IN ('queued','running','cancelling','interrupted')"
            if active
            else ""
        )
        with self._managed_connection() as connection:
            ids = connection.execute(
                f"""SELECT receipt.scan_job_id
                      FROM library_bootstrap_receipts receipt
                      JOIN media_jobs job ON job.id=receipt.scan_job_id
                     WHERE 1=1 {where}
                     ORDER BY job.created_at DESC,job.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            result: list[dict[str, object]] = []
            for item in ids:
                row = self._library_scan_row(connection, str(item["scan_job_id"]))
                context = self._library_scan_context_from_row(row)
                result.append(
                    {"context": context, "job": self._library_scan_job_projection(row)}
                )
            return result
