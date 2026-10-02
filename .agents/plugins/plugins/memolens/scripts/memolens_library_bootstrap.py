#!/usr/bin/env python3
"""Bounded, path-free spool for native MemoLens Library bootstrap intent.

This is deliberately outside the Core database and Agent credential stores.
Writing an intent neither contacts the backend nor creates a Library, project,
Blueprint, scan, Timeline, or editor capability.  Electron main is the only
consumer allowed to turn the intent into a native folder confirmation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]

from memolens_contracts import MemoLensError
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json


LIBRARY_BOOTSTRAP_SCHEMA_VERSION = "1"
LIBRARY_BOOTSTRAP_TTL_MS = 5 * 60 * 1000
LIBRARY_BOOTSTRAP_MAX_BYTES = 2_048
LIBRARY_BOOTSTRAP_MAX_ENTRIES = 64
LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES = LIBRARY_BOOTSTRAP_MAX_ENTRIES * 2
LIBRARY_BOOTSTRAP_SPOOL_NAME = "library-bootstrap-spool"
LIBRARY_BOOTSTRAP_TEMP_STALE_MS = LIBRARY_BOOTSTRAP_TTL_MS

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_REQUEST_ID = re.compile(r"^lb_[0-9a-f]{64}$")
_ENTRY_NAME = re.compile(
    r"^lb_[0-9a-f]{64}\.(?:request|claim|receipt)\.json$"
)
_CONTROLLED_TEMP_NAME = re.compile(
    r"^\.(lb_[0-9a-f]{64})\.[0-9a-f]{32}\.tmp$"
)
_REQUEST_KEYS = frozenset(
    {"schema_version", "kind", "request_id", "created_at_ms", "expires_at_ms"}
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "request_id",
        "state",
        "completed_at_ms",
        "expires_at_ms",
    }
)
_RECEIPT_STATES = frozenset(
    {
        "library_authority_staged",
        "library_authority_committed",
        "native_cancelled",
        "expired",
    }
)
_JSON_LIMITS = StrictJsonLimits(
    max_bytes=LIBRARY_BOOTSTRAP_MAX_BYTES,
    max_depth=4,
    max_nodes=32,
    max_object_items=8,
    max_array_items=1,
    max_string_chars=128,
    max_integer_bits=63,
)


def _error(message: str, code: str) -> MemoLensError:
    return MemoLensError(message, code=code)


def _app_state_dir() -> Path:
    configured = os.getenv("MEMOLENS_APP_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MemoLens"
    if os.name == "nt":
        appdata = os.getenv("APPDATA", "").strip()
        return Path(appdata).expanduser() / "MemoLens" if appdata else (
            Path.home() / "AppData" / "Roaming" / "MemoLens"
        )
    state_home = os.getenv("XDG_STATE_HOME", "").strip()
    return Path(state_home).expanduser() / "MemoLens" if state_home else (
        Path.home() / ".local" / "state" / "MemoLens"
    )


def _spool_dir() -> Path:
    return _app_state_dir() / LIBRARY_BOOTSTRAP_SPOOL_NAME


def _open_spool_dir(*, create: bool) -> tuple[Path, int]:
    spool = _spool_dir()
    try:
        if create:
            spool.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = spool.lstat()
    except FileNotFoundError as exc:
        raise _error(
            "MemoLens bootstrap request was not found.",
            "bootstrap_request_not_found",
        ) from exc
    except OSError as exc:
        raise _error(
            "MemoLens could not establish its private bootstrap spool.",
            "bootstrap_spool_unavailable",
        ) from exc
    if not stat_is_directory(details.st_mode) or stat_is_symlink(details.st_mode):
        raise _error(
            "MemoLens rejected an unsafe bootstrap spool directory.",
            "bootstrap_spool_unsafe",
        )
    if details.st_mode & 0o777 != 0o700:
        raise _error(
            "MemoLens rejected bootstrap spool permissions broader than 0700.",
            "bootstrap_spool_unsafe",
        )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(spool, flags)
    except OSError as exc:
        raise _error(
            "MemoLens could not securely open its bootstrap spool.",
            "bootstrap_spool_unsafe",
        ) from exc
    opened = os.fstat(directory_fd)
    if (
        opened.st_dev != details.st_dev
        or opened.st_ino != details.st_ino
        or not stat_is_directory(opened.st_mode)
        or opened.st_mode & 0o777 != 0o700
    ):
        os.close(directory_fd)
        raise _error(
            "MemoLens rejected a replaced bootstrap spool directory.",
            "bootstrap_spool_unsafe",
        )
    return spool, directory_fd


def stat_is_directory(mode: int) -> bool:
    return (mode & 0o170000) == 0o040000


def stat_is_symlink(mode: int) -> bool:
    return (mode & 0o170000) == 0o120000


def _validate_entry_details(name: str, details: os.stat_result) -> None:
    if stat_is_symlink(details.st_mode) or (details.st_mode & 0o170000) != 0o100000:
        raise _error(
            "MemoLens rejected a non-regular bootstrap spool entry.",
            "bootstrap_spool_unsafe",
        )
    if details.st_size > LIBRARY_BOOTSTRAP_MAX_BYTES:
        raise _error(
            "MemoLens rejected an oversized bootstrap spool entry.",
            "bootstrap_request_oversize",
        )
    if details.st_mode & 0o777 != 0o600:
        raise _error(
            "MemoLens rejected bootstrap request permissions broader than 0600.",
            "bootstrap_spool_unsafe",
        )
    if len(name) > 160:
        raise _error(
            "MemoLens rejected an unsafe bootstrap spool entry.",
            "bootstrap_spool_unsafe",
        )


def _census(directory_fd: int) -> list[str]:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise _error(
            "MemoLens could not inspect the bounded bootstrap spool.",
            "bootstrap_spool_unsafe",
        ) from exc
    if len(names) > LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES:
        raise _error(
            "MemoLens bootstrap spool reached its bounded raw census.",
            "bootstrap_spool_limit",
        )
    verified_names: list[str] = []
    for name in names:
        controlled_temp = (
            isinstance(name, str) and _CONTROLLED_TEMP_NAME.fullmatch(name) is not None
        )
        if (
            not isinstance(name, str)
            or len(name) > 160
            or (
                _ENTRY_NAME.fullmatch(name) is None
                and not controlled_temp
            )
        ):
            raise _error(
                "MemoLens rejected an unsafe bootstrap spool entry.",
                "bootstrap_spool_unsafe",
            )
        try:
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            # A verified controlled temp may disappear after listdir when its
            # concurrent writer publishes or cleans it. Final artifacts do not
            # receive this exception because they carry protocol state.
            if controlled_temp:
                continue
            raise _error(
                "MemoLens could not verify a bootstrap spool entry.",
                "bootstrap_spool_unsafe",
            ) from exc
        except OSError as exc:
            raise _error(
                "MemoLens could not verify a bootstrap spool entry.",
                "bootstrap_spool_unsafe",
            ) from exc
        _validate_entry_details(name, details)
        verified_names.append(name)
    return verified_names


def _protocol_entry_names(names: list[str] | frozenset[str]) -> list[str]:
    protocol_names = [name for name in names if _ENTRY_NAME.fullmatch(name) is not None]
    if len(protocol_names) > LIBRARY_BOOTSTRAP_MAX_ENTRIES:
        raise _error(
            "MemoLens bootstrap spool reached its bounded protocol census.",
            "bootstrap_spool_limit",
        )
    return protocol_names


def _lock_writer(directory_fd: int) -> None:
    if fcntl is None or os.name != "posix":
        raise _error(
            "MemoLens bootstrap writes require a supported cross-process writer lock.",
            "bootstrap_writer_lock_unsupported",
        )
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
    except OSError as exc:
        raise _error(
            "MemoLens could not acquire its bootstrap writer lock.",
            "bootstrap_spool_unavailable",
        ) from exc


def _unlock_writer(directory_fd: int) -> None:
    if fcntl is None or os.name != "posix":
        return
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_UN)
    except OSError as exc:
        raise _error(
            "MemoLens could not release its bootstrap writer lock.",
            "bootstrap_spool_unavailable",
        ) from exc


def _cleanup_stale_controlled_temps(directory_fd: int, names: list[str]) -> None:
    cutoff_ns = time.time_ns() - LIBRARY_BOOTSTRAP_TEMP_STALE_MS * 1_000_000
    removed = False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        if _CONTROLLED_TEMP_NAME.fullmatch(name) is None:
            continue
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _error(
                "MemoLens could not verify a controlled bootstrap temp.",
                "bootstrap_spool_unsafe",
            ) from exc
        _validate_entry_details(name, before)
        if before.st_mtime_ns > cutoff_ns:
            continue
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _error(
                "MemoLens could not securely open a controlled bootstrap temp.",
                "bootstrap_spool_unsafe",
            ) from exc
        try:
            opened = os.fstat(descriptor)
            _validate_entry_details(name, opened)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise _error(
                    "MemoLens rejected a replaced bootstrap temp.",
                    "bootstrap_spool_unsafe",
                )
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _error(
                    "MemoLens could not clean a stale bootstrap temp.",
                    "bootstrap_spool_unavailable",
                ) from exc
            removed = True
        finally:
            os.close(descriptor)
    if removed:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise _error(
                "MemoLens could not durably clean stale bootstrap temps.",
                "bootstrap_spool_unavailable",
            ) from exc


def _read_entry(directory_fd: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise _error(
            "MemoLens could not securely read the bootstrap request.",
            "bootstrap_spool_unsafe",
        ) from exc
    try:
        details = os.fstat(descriptor)
        if (
            (details.st_mode & 0o170000) != 0o100000
            or details.st_mode & 0o777 != 0o600
        ):
            raise _error(
                "MemoLens rejected an unsafe bootstrap request file.",
                "bootstrap_spool_unsafe",
            )
        if details.st_size > LIBRARY_BOOTSTRAP_MAX_BYTES:
            raise _error(
                "MemoLens rejected an oversized bootstrap request.",
                "bootstrap_request_oversize",
            )
        payload = os.read(descriptor, LIBRARY_BOOTSTRAP_MAX_BYTES + 1)
        if len(payload) > LIBRARY_BOOTSTRAP_MAX_BYTES:
            raise _error(
                "MemoLens rejected an oversized bootstrap request.",
                "bootstrap_request_oversize",
            )
        return payload
    finally:
        os.close(descriptor)


def _strict_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > LIBRARY_BOOTSTRAP_MAX_BYTES:
        raise _error(
            "MemoLens rejected an oversized bootstrap request.",
            "bootstrap_request_oversize",
        )
    try:
        decoded = decode_strict_json(raw, require_object=True, limits=_JSON_LIMITS)
    except StrictJsonError as exc:
        code = (
            "bootstrap_request_oversize"
            if exc.code == "input_too_large"
            else "bootstrap_request_invalid"
        )
        raise _error("MemoLens rejected a malformed bootstrap request.", code) from exc
    if type(decoded) is not dict:
        raise _error(
            "MemoLens rejected a malformed bootstrap request.",
            "bootstrap_request_invalid",
        )
    return decoded


def decode_library_bootstrap_request(raw: bytes) -> dict[str, Any]:
    decoded = _strict_object(raw)
    if frozenset(decoded) != _REQUEST_KEYS:
        raise _error(
            "MemoLens rejected a non-closed bootstrap request schema.",
            "bootstrap_request_invalid",
        )
    request_id = decoded.get("request_id")
    created_at_ms = decoded.get("created_at_ms")
    expires_at_ms = decoded.get("expires_at_ms")
    if (
        decoded.get("schema_version") != LIBRARY_BOOTSTRAP_SCHEMA_VERSION
        or decoded.get("kind") != "library_bootstrap_intent"
        or type(request_id) is not str
        or _REQUEST_ID.fullmatch(request_id) is None
        or type(created_at_ms) is not int
        or type(expires_at_ms) is not int
        or created_at_ms < 0
        or expires_at_ms != created_at_ms + LIBRARY_BOOTSTRAP_TTL_MS
    ):
        raise _error(
            "MemoLens rejected an invalid bootstrap request contract.",
            "bootstrap_request_invalid",
        )
    return decoded


def _decode_receipt(raw: bytes) -> dict[str, Any]:
    decoded = _strict_object(raw)
    if frozenset(decoded) != _RECEIPT_KEYS:
        raise _error(
            "MemoLens rejected a non-closed bootstrap receipt schema.",
            "bootstrap_receipt_invalid",
        )
    if (
        decoded.get("schema_version") != LIBRARY_BOOTSTRAP_SCHEMA_VERSION
        or decoded.get("kind") != "library_bootstrap_receipt"
        or type(decoded.get("request_id")) is not str
        or _REQUEST_ID.fullmatch(decoded["request_id"]) is None
        or decoded.get("state") not in _RECEIPT_STATES
        or type(decoded.get("completed_at_ms")) is not int
        or decoded["completed_at_ms"] < 0
        or type(decoded.get("expires_at_ms")) is not int
        or decoded["expires_at_ms"] < 0
    ):
        raise _error(
            "MemoLens rejected an invalid bootstrap receipt contract.",
            "bootstrap_receipt_invalid",
        )
    return decoded


def _derive_request_id(request_idempotency_key: str) -> str:
    if (
        type(request_idempotency_key) is not str
        or _IDEMPOTENCY_KEY.fullmatch(request_idempotency_key) is None
    ):
        raise _error(
            "Bootstrap idempotency key must be 1-200 bounded ASCII characters.",
            "invalid_argument",
        )
    digest = hashlib.sha256(
        b"memolens-library-bootstrap-v1\0" + request_idempotency_key.encode("ascii")
    ).hexdigest()
    return f"lb_{digest}"


def _request_name(request_id: str) -> str:
    return f"{request_id}.request.json"


def _claim_name(request_id: str) -> str:
    return f"{request_id}.claim.json"


def _receipt_name(request_id: str) -> str:
    return f"{request_id}.receipt.json"


def _atomic_publish_noreplace(
    spool: Path,
    directory_fd: int,
    temporary_name: str,
    target_name: str,
) -> bool:
    """Publish one fsynced file atomically without replacing a conflicting entry."""

    del spool  # Publication is intentionally anchored only to the verified directory fd.
    try:
        # A hard-link publish is atomic and O_EXCL-like: it cannot replace an
        # existing name, and both names stay anchored to the already verified
        # directory descriptor.  Removing the temp name leaves the fully
        # fsynced inode available under the final name.
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return True
    except FileExistsError:
        # A same-key concurrent writer may have won the no-replace publish.
        # The caller must strictly read and validate that winner before it can
        # report convergence.
        return False
    except OSError as exc:
        raise _error(
            "MemoLens could not atomically publish the bootstrap request without replacement.",
            "bootstrap_spool_unavailable",
        ) from exc


def _write_request(
    spool: Path,
    directory_fd: int,
    payload: dict[str, Any],
) -> bool:
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(body) > LIBRARY_BOOTSTRAP_MAX_BYTES:
        raise _error(
            "MemoLens refused to write an oversized bootstrap request.",
            "bootstrap_request_oversize",
        )
    temporary_name = f".{payload['request_id']}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        raise _error(
            "MemoLens could not create a private bootstrap request.",
            "bootstrap_spool_unavailable",
        ) from exc
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        return _atomic_publish_noreplace(
            spool,
            directory_fd,
            temporary_name,
            _request_name(payload["request_id"]),
        )
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise _error(
                "MemoLens could not clean its controlled bootstrap temp.",
                "bootstrap_spool_unavailable",
            ) from exc
        else:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                raise _error(
                    "MemoLens could not durably clean its bootstrap temp.",
                    "bootstrap_spool_unavailable",
                ) from exc


def _status_result(
    request_id: str,
    status: str,
    expires_at_ms: int,
) -> dict[str, Any]:
    next_action = {
        "queued_open_memolens": "open_memolens",
        "awaiting_native_confirmation": "finish_native_confirmation",
        "library_authority_staged": "open_memolens",
        "library_authority_committed": "wait_for_library_scan",
        "native_cancelled": "start_with_new_idempotency_key",
        "expired": "start_with_new_idempotency_key",
    }[status]
    core_committed = status == "library_authority_committed"
    return {
        "object": "memolens.library_bootstrap_status",
        "schema_version": LIBRARY_BOOTSTRAP_SCHEMA_VERSION,
        "request_id": request_id,
        "expires_at_ms": expires_at_ms,
        "status": status,
        "next_action": next_action,
        "core_library_created": core_committed,
        "database_created": core_committed,
        "project_created": core_committed,
        "scan_started": False,
        "editor_ready": False,
        "timeline_edit_granted": False,
        "backend_contacted_by_plugin": False,
    }


def library_bootstrap_status(
    request_id: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        raise _error("Bootstrap request ID is invalid.", "invalid_argument")
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if type(current_ms) is not int or current_ms < 0:
        raise _error("Bootstrap clock is invalid.", "bootstrap_clock_invalid")
    _spool, directory_fd = _open_spool_dir(create=False)
    try:
        names = frozenset(_census(directory_fd))
        _protocol_entry_names(names)
        request_name = _request_name(request_id)
        claim_name = _claim_name(request_id)
        receipt_name = _receipt_name(request_id)
        present = names.intersection({request_name, claim_name, receipt_name})
        if receipt_name in present:
            receipt = _decode_receipt(_read_entry(directory_fd, receipt_name))
            if receipt["request_id"] != request_id:
                raise _error(
                    "MemoLens rejected a conflicting bootstrap receipt.",
                    "bootstrap_request_conflict",
                )
            # A terminal receipt is authoritative over an exact same-ID request
            # or claim left by a crash between receipt publication and cleanup.
            # The read-only Agent status path never performs that cleanup.
            for stale_name in (request_name, claim_name):
                if stale_name not in present:
                    continue
                stale = decode_library_bootstrap_request(
                    _read_entry(directory_fd, stale_name)
                )
                if stale["request_id"] != request_id:
                    raise _error(
                        "MemoLens rejected a conflicting bootstrap artifact.",
                        "bootstrap_request_conflict",
                    )
                if stale["expires_at_ms"] != receipt["expires_at_ms"]:
                    raise _error(
                        "MemoLens rejected conflicting bootstrap expiry data.",
                        "bootstrap_request_conflict",
                    )
            return _status_result(
                request_id,
                receipt["state"],
                receipt["expires_at_ms"],
            )
        if request_name in present and claim_name in present:
            request = decode_library_bootstrap_request(
                _read_entry(directory_fd, request_name)
            )
            claim = decode_library_bootstrap_request(
                _read_entry(directory_fd, claim_name)
            )
            if request != claim:
                raise _error(
                    "MemoLens rejected conflicting bootstrap request and claim data.",
                    "bootstrap_request_conflict",
                )
            return _status_result(
                request_id,
                "awaiting_native_confirmation",
                request["expires_at_ms"],
            )
        if claim_name in present:
            request = decode_library_bootstrap_request(
                _read_entry(directory_fd, claim_name)
            )
            if request["request_id"] != request_id:
                raise _error(
                    "MemoLens rejected a conflicting bootstrap claim.",
                    "bootstrap_request_conflict",
                )
            return _status_result(
                request_id,
                "awaiting_native_confirmation",
                request["expires_at_ms"],
            )
        if request_name in present:
            request = decode_library_bootstrap_request(
                _read_entry(directory_fd, request_name)
            )
            if request["request_id"] != request_id:
                raise _error(
                    "MemoLens rejected a conflicting bootstrap request.",
                    "bootstrap_request_conflict",
                )
            if request["expires_at_ms"] <= current_ms:
                return _status_result(
                    request_id,
                    "expired",
                    request["expires_at_ms"],
                )
            return _status_result(
                request_id,
                "queued_open_memolens",
                request["expires_at_ms"],
            )
    finally:
        os.close(directory_fd)
    raise _error(
        "MemoLens bootstrap request was not found.",
        "bootstrap_request_not_found",
    )


def start_library_bootstrap(
    request_idempotency_key: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    request_id = _derive_request_id(request_idempotency_key)
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if type(current_ms) is not int or current_ms < 0:
        raise _error("Bootstrap clock is invalid.", "bootstrap_clock_invalid")
    spool, directory_fd = _open_spool_dir(create=True)
    try:
        _lock_writer(directory_fd)
        try:
            names_before_cleanup = _census(directory_fd)
            _cleanup_stale_controlled_temps(directory_fd, names_before_cleanup)
            names = frozenset(_census(directory_fd))
            protocol_names = _protocol_entry_names(names)
            relevant = names.intersection(
                {
                    _request_name(request_id),
                    _claim_name(request_id),
                    _receipt_name(request_id),
                }
            )
            if relevant:
                # Re-enter the closed reader after releasing this descriptor. A
                # duplicate call returns the same opaque request instead of writing.
                pass
            else:
                if len(protocol_names) >= LIBRARY_BOOTSTRAP_MAX_ENTRIES:
                    raise _error(
                        "MemoLens bootstrap spool reached its bounded census.",
                        "bootstrap_spool_limit",
                    )
                payload = {
                    "schema_version": LIBRARY_BOOTSTRAP_SCHEMA_VERSION,
                    "kind": "library_bootstrap_intent",
                    "request_id": request_id,
                    "created_at_ms": current_ms,
                    "expires_at_ms": current_ms + LIBRARY_BOOTSTRAP_TTL_MS,
                }
                published = _write_request(spool, directory_fd, payload)
                if not published:
                    # A no-replace loser never trusts its own payload. After
                    # releasing the writer lock, the common strict status path
                    # validates the winner's closed schema and exact ID.
                    pass
        finally:
            _unlock_writer(directory_fd)
    finally:
        os.close(directory_fd)
    return library_bootstrap_status(request_id, now_ms=current_ms)


__all__ = [
    "LIBRARY_BOOTSTRAP_MAX_BYTES",
    "LIBRARY_BOOTSTRAP_MAX_ENTRIES",
    "LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES",
    "LIBRARY_BOOTSTRAP_SCHEMA_VERSION",
    "LIBRARY_BOOTSTRAP_SPOOL_NAME",
    "LIBRARY_BOOTSTRAP_TEMP_STALE_MS",
    "LIBRARY_BOOTSTRAP_TTL_MS",
    "decode_library_bootstrap_request",
    "library_bootstrap_status",
    "start_library_bootstrap",
]
