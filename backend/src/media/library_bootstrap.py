"""Exact native Library bootstrap candidate admission.

The Agent-facing request never reaches this module.  Electron main starts a
dedicated candidate process with one opaque request id; this module opens the
fixed, private binding envelope, pins the selected directory, and exposes only
the frozen Core command arguments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import MediaRepository, canonical_json
from core.sqlite_runtime import require_safe_sqlite_runtime


BOOTSTRAP_REQUEST_ENV = "MEMOLENS_BOOTSTRAP_REQUEST_ID"
BOOTSTRAP_BINDING_DIRECTORY = "library-bootstrap-bindings"
BOOTSTRAP_BINDING_SCHEMA_VERSION = "1"
BOOTSTRAP_BINDING_KIND = "library_bootstrap_candidate_binding"
BOOTSTRAP_COMMAND_OBJECT = "memolens.main_library_bootstrap_command"
BOOTSTRAP_COMMAND_SCHEMA_VERSION = "1"
BOOTSTRAP_BINDING_DOMAIN = b"memolens.library_bootstrap_candidate_binding.v1\0"
BOOTSTRAP_BINDING_MAX_BYTES = 4_096
BOOTSTRAP_INTENT_TTL_MS = 5 * 60 * 1_000
BOOTSTRAP_IDEMPOTENCY_HEADER = "Idempotency-Key"

_REQUEST_ID = re.compile(r"\Alb_[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_DECIMAL_IDENTITY = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")
_BINDING_KEYS = frozenset(
    {
        "candidate_binding_sha256",
        "canonical_root",
        "database_path",
        "expected_library_root_device",
        "expected_library_root_inode",
        "intent_created_at_ms",
        "intent_expires_at_ms",
        "kind",
        "native_confirmed_at_ms",
        "request_id",
        "schema_version",
    }
)
BOOTSTRAP_COMMAND_KEYS = frozenset(
    {
        "candidate_binding_sha256",
        "object",
        "request_id",
        "schema_version",
    }
)
BOOTSTRAP_RESPONSE_KEYS = frozenset(
    {
        "blueprint_authority",
        "editor_state",
        "object",
        "request_id",
        "scan_stage",
        "scan_started",
        "schema_version",
        "status",
        "timeline_edit_granted",
    }
)


class LibraryBootstrapError(RuntimeError):
    """Closed error surfaced by the main-only bootstrap route."""

    def __init__(self, code: str, message: str, *, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _duplicate_key_rejected(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LibraryBootstrapError(
                "duplicate_json_key",
                "Bootstrap binding JSON contains a duplicate key.",
                status=409,
            )
        result[key] = value
    return result


def _canonical_nfc_path(value: object, *, field: str, must_exist: bool) -> Path:
    if type(value) is not str or not value or "\0" in value:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            f"Bootstrap binding `{field}` is invalid.",
            status=409,
        )
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            f"Bootstrap binding `{field}` is not canonical NFC.",
            status=409,
        )
    path = Path(value)
    if not path.is_absolute():
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            f"Bootstrap binding `{field}` must be absolute.",
            status=409,
        )
    try:
        canonical = path.resolve(strict=must_exist)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_unavailable",
            f"Bootstrap binding `{field}` is unavailable.",
            status=409,
        ) from exc
    if unicodedata.normalize("NFC", str(canonical)) != value:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_changed",
            f"Bootstrap binding `{field}` changed identity.",
            status=409,
        )
    return canonical


def _decimal_identity(value: object, *, field: str) -> tuple[str, int]:
    if type(value) is not str or _DECIMAL_IDENTITY.fullmatch(value) is None:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            f"Bootstrap binding `{field}` is invalid.",
            status=409,
        )
    parsed = int(value)
    if parsed < 0:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            f"Bootstrap binding `{field}` is invalid.",
            status=409,
        )
    return value, parsed


def _safe_timestamp(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 9_007_199_254_740_991:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            f"Bootstrap binding `{field}` is invalid.",
            status=409,
        )
    return value


def candidate_binding_sha256(
    *,
    canonical_root: str,
    database_path: str,
    expected_library_root_device: str,
    expected_library_root_inode: str,
) -> str:
    material = {
        "canonical_root": canonical_root,
        "database_path": database_path,
        "expected_library_root_device": expected_library_root_device,
        "expected_library_root_inode": expected_library_root_inode,
    }
    return hashlib.sha256(
        BOOTSTRAP_BINDING_DOMAIN + canonical_json(material).encode("utf-8")
    ).hexdigest()


def resolve_bootstrap_database_path(app_state_dir: Path, canonical_root: str) -> Path:
    digest = hashlib.sha256(canonical_root.encode("utf-8")).hexdigest()[:24]
    return app_state_dir / "storage" / f"photo-index-{digest}.db"


def _binding_directory_handle(app_state_dir: Path) -> tuple[int, Path]:
    binding_dir = app_state_dir / BOOTSTRAP_BINDING_DIRECTORY
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = os.stat(binding_dir, follow_symlinks=False)
        directory_fd = os.open(binding_dir, flags)
        opened = os.fstat(directory_fd)
    except OSError as exc:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_unavailable",
            "The sealed bootstrap binding directory is unavailable.",
            status=503,
        ) from exc
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
    ):
        os.close(directory_fd)
        raise LibraryBootstrapError(
            "library_bootstrap_binding_unsafe",
            "The sealed bootstrap binding directory is unsafe.",
            status=503,
        )
    return directory_fd, binding_dir


def _read_binding_envelope(app_state_dir: Path, request_id: str) -> dict[str, Any]:
    directory_fd, _binding_dir = _binding_directory_handle(app_state_dir)
    filename = f"{request_id}.binding.json"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd: int | None = None
    try:
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > BOOTSTRAP_BINDING_MAX_BYTES
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
        ):
            raise LibraryBootstrapError(
                "library_bootstrap_binding_unsafe",
                "The sealed bootstrap binding file is unsafe.",
                status=503,
            )
        payload = bytearray()
        while len(payload) <= BOOTSTRAP_BINDING_MAX_BYTES:
            chunk = os.read(file_fd, min(1024, BOOTSTRAP_BINDING_MAX_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(payload) > BOOTSTRAP_BINDING_MAX_BYTES or identity_after != identity_before:
            raise LibraryBootstrapError(
                "library_bootstrap_binding_changed",
                "The sealed bootstrap binding changed while it was read.",
                status=409,
            )
    except LibraryBootstrapError:
        raise
    except OSError as exc:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_unavailable",
            "The sealed bootstrap binding file is unavailable.",
            status=503,
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)
    try:
        raw = payload.decode("utf-8", errors="strict")
        value = json.loads(raw, object_pairs_hook=_duplicate_key_rejected)
    except LibraryBootstrapError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            "The sealed bootstrap binding is not valid JSON.",
            status=409,
        ) from exc
    if type(value) is not dict or frozenset(value) != _BINDING_KEYS:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            "The sealed bootstrap binding has an unsupported shape.",
            status=409,
        )
    return value


@dataclass
class BootstrapCandidateBinding:
    request_id: str
    intent_created_at_ms: int
    intent_expires_at_ms: int
    native_confirmed_at_ms: int
    canonical_root: Path
    database_path: Path
    expected_library_root_device: int
    expected_library_root_inode: int
    candidate_binding_sha256: str
    root_fd: int
    settings: Settings

    def verify_current_identity(self) -> None:
        try:
            held = os.fstat(self.root_fd)
            current = os.stat(self.canonical_root, follow_symlinks=False)
            resolved = self.canonical_root.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LibraryBootstrapError(
                "library_bootstrap_binding_changed",
                "The selected Library root is no longer available.",
                status=409,
            ) from exc
        expected = (self.expected_library_root_device, self.expected_library_root_inode)
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (held.st_dev, held.st_ino) != expected
            or (current.st_dev, current.st_ino) != expected
            or unicodedata.normalize("NFC", str(resolved)) != str(self.canonical_root)
        ):
            raise LibraryBootstrapError(
                "library_bootstrap_binding_changed",
                "The selected Library root changed identity.",
                status=409,
            )

    def close(self) -> None:
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1


def load_bootstrap_candidate_binding(
    *,
    app_state_dir: Path,
    request_id: str,
) -> BootstrapCandidateBinding:
    if _REQUEST_ID.fullmatch(request_id) is None:
        raise LibraryBootstrapError(
            "library_bootstrap_request_invalid",
            "Bootstrap candidate request identity is invalid.",
            status=503,
        )
    envelope = _read_binding_envelope(app_state_dir, request_id)
    if (
        envelope["schema_version"] != BOOTSTRAP_BINDING_SCHEMA_VERSION
        or envelope["kind"] != BOOTSTRAP_BINDING_KIND
        or envelope["request_id"] != request_id
    ):
        raise LibraryBootstrapError(
            "library_bootstrap_binding_invalid",
            "The sealed bootstrap binding identity is invalid.",
            status=409,
        )
    created = _safe_timestamp(envelope["intent_created_at_ms"], field="intent_created_at_ms")
    expires = _safe_timestamp(envelope["intent_expires_at_ms"], field="intent_expires_at_ms")
    confirmed = _safe_timestamp(envelope["native_confirmed_at_ms"], field="native_confirmed_at_ms")
    if expires != created + BOOTSTRAP_INTENT_TTL_MS or not created <= confirmed < expires:
        raise LibraryBootstrapError(
            "library_bootstrap_intent_expired",
            "The sealed bootstrap intent timing is invalid or expired.",
            status=409,
        )
    canonical_root = _canonical_nfc_path(
        envelope["canonical_root"],
        field="canonical_root",
        must_exist=True,
    )
    database_path = _canonical_nfc_path(
        envelope["database_path"],
        field="database_path",
        must_exist=False,
    )
    expected_database = resolve_bootstrap_database_path(app_state_dir, str(canonical_root))
    if database_path != expected_database:
        raise LibraryBootstrapError(
            "library_bootstrap_database_scope_mismatch",
            "The bootstrap database is outside the exact managed Library binding.",
            status=409,
        )
    device_text, device = _decimal_identity(
        envelope["expected_library_root_device"],
        field="expected_library_root_device",
    )
    inode_text, inode = _decimal_identity(
        envelope["expected_library_root_inode"],
        field="expected_library_root_inode",
    )
    supplied_digest = envelope["candidate_binding_sha256"]
    expected_digest = candidate_binding_sha256(
        canonical_root=str(canonical_root),
        database_path=str(database_path),
        expected_library_root_device=device_text,
        expected_library_root_inode=inode_text,
    )
    if (
        type(supplied_digest) is not str
        or _SHA256.fullmatch(supplied_digest) is None
        or not hmac.compare_digest(supplied_digest, expected_digest)
    ):
        raise LibraryBootstrapError(
            "library_bootstrap_binding_digest_mismatch",
            "The sealed bootstrap binding digest is invalid.",
            status=409,
        )

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(canonical_root, flags)
    except OSError as exc:
        raise LibraryBootstrapError(
            "library_bootstrap_binding_unavailable",
            "The selected Library root is unavailable.",
            status=409,
        ) from exc
    try:
        settings = Settings.for_bootstrap_candidate(
            image_library_dir=canonical_root,
            db_path=database_path,
        )
        binding = BootstrapCandidateBinding(
            request_id=request_id,
            intent_created_at_ms=created,
            intent_expires_at_ms=expires,
            native_confirmed_at_ms=confirmed,
            canonical_root=canonical_root,
            database_path=database_path,
            expected_library_root_device=device,
            expected_library_root_inode=inode,
            candidate_binding_sha256=supplied_digest,
            root_fd=root_fd,
            settings=settings,
        )
        binding.verify_current_identity()
        return binding
    except Exception:
        os.close(root_fd)
        raise


def load_bootstrap_candidate_from_environment() -> BootstrapCandidateBinding | None:
    request_id = os.environ.get(BOOTSTRAP_REQUEST_ENV)
    if request_id is None:
        return None
    from core.app_settings import resolve_app_state_dir

    project_root = Path(__file__).resolve().parents[3]
    app_state_dir = resolve_app_state_dir(project_root).expanduser().resolve(strict=False)
    return load_bootstrap_candidate_binding(
        app_state_dir=app_state_dir,
        request_id=request_id,
    )


@dataclass(frozen=True)
class LibraryBootstrapCommitResult:
    response: dict[str, Any]
    response_status: int
    replayed: bool
    database_uuid: str


def commit_library_bootstrap(
    binding: BootstrapCandidateBinding,
    *,
    repository_factory: Callable[[Path], MediaRepository] = MediaRepository,
) -> LibraryBootstrapCommitResult:
    """Call the frozen Core aggregate without leaking paths into HTTP input."""

    binding.verify_current_identity()
    require_safe_sqlite_runtime()
    # Initialize the compatibility baseline before Core advances to V14+.
    # After cutover, ensure_schema is deliberately validation-only: creating
    # this table later would bypass the canonical projection schema guard.
    # This writes no Library/root/project/job authority; those remain in the
    # single Core aggregate transaction below.
    binding.database_path.parent.mkdir(parents=True, exist_ok=True)
    ImageIndexRepository(binding.database_path).ensure_schema()
    repository = repository_factory(binding.database_path)
    try:
        # V20 schema admission is deliberately root-free.  Core adopts the
        # selected root only inside its aggregate transaction below.
        repository.ensure_schema(default_library_root=None)
        command = getattr(repository, "commit_library_bootstrap", None)
        if not callable(command):
            raise LibraryBootstrapError(
                "library_bootstrap_core_unavailable",
                "Core Library bootstrap is unavailable in this runtime.",
                status=503,
            )
        raw_result = command(
            request_id=binding.request_id,
            intent_created_at_ms=binding.intent_created_at_ms,
            intent_expires_at_ms=binding.intent_expires_at_ms,
            native_confirmed_at_ms=binding.native_confirmed_at_ms,
            candidate_binding_sha256=binding.candidate_binding_sha256,
            library_root_path=str(binding.canonical_root),
            library_root_fd=binding.root_fd,
            expected_library_root_device=binding.expected_library_root_device,
            expected_library_root_inode=binding.expected_library_root_inode,
        )
        if type(raw_result) is not dict or frozenset(raw_result) != {
            "replayed",
            "response",
            "response_status",
        }:
            raise LibraryBootstrapError(
                "library_bootstrap_core_invalid",
                "Core returned an invalid Library bootstrap result.",
                status=503,
            )
        response = raw_result["response"]
        response_status = raw_result["response_status"]
        replayed = raw_result["replayed"]
        if (
            type(response) is not dict
            or frozenset(response) != BOOTSTRAP_RESPONSE_KEYS
            or response.get("object") != "memolens.library_bootstrap_result"
            or response.get("schema_version") != "1"
            or response.get("request_id") != binding.request_id
            or response.get("status") != "library_authority_committed"
            or response.get("scan_started") is not False
            or response.get("scan_stage") != "awaiting_scan_worker"
            or response.get("editor_state") != "awaiting_grounded_timeline"
            or response.get("blueprint_authority") != "unverified"
            or response.get("timeline_edit_granted") is not False
            or response_status != 201
            or type(replayed) is not bool
        ):
            raise LibraryBootstrapError(
                "library_bootstrap_core_invalid",
                "Core returned an invalid Library bootstrap result.",
                status=503,
            )
        database_uuid = repository.database_uuid
        return LibraryBootstrapCommitResult(
            response=response,
            response_status=response_status,
            replayed=replayed,
            database_uuid=database_uuid,
        )
    finally:
        close = getattr(repository, "close", None)
        if callable(close):
            close()


def validate_bootstrap_command(
    payload: Mapping[str, Any],
    *,
    idempotency_key: str,
    binding: BootstrapCandidateBinding,
) -> None:
    if frozenset(payload) != BOOTSTRAP_COMMAND_KEYS:
        raise LibraryBootstrapError(
            "library_bootstrap_command_invalid",
            "Library bootstrap command has an unsupported shape.",
            status=400,
        )
    if (
        payload.get("object") != BOOTSTRAP_COMMAND_OBJECT
        or payload.get("schema_version") != BOOTSTRAP_COMMAND_SCHEMA_VERSION
        or payload.get("request_id") != binding.request_id
        or payload.get("candidate_binding_sha256") != binding.candidate_binding_sha256
        or idempotency_key != binding.request_id
    ):
        raise LibraryBootstrapError(
            "library_bootstrap_binding_mismatch",
            "Library bootstrap command does not match the exact candidate binding.",
            status=409,
        )


__all__ = [
    "BOOTSTRAP_BINDING_DOMAIN",
    "BOOTSTRAP_COMMAND_KEYS",
    "BOOTSTRAP_COMMAND_OBJECT",
    "BOOTSTRAP_IDEMPOTENCY_HEADER",
    "BOOTSTRAP_RESPONSE_KEYS",
    "BootstrapCandidateBinding",
    "LibraryBootstrapCommitResult",
    "LibraryBootstrapError",
    "candidate_binding_sha256",
    "commit_library_bootstrap",
    "load_bootstrap_candidate_binding",
    "load_bootstrap_candidate_from_environment",
    "resolve_bootstrap_database_path",
    "validate_bootstrap_command",
]
