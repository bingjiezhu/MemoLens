"""Restricted local credential storage for short-lived paired Agent sessions.

The proof secret is deliberately absent from CLI/MCP output, argv, environment
variables, MemoLens project data, and the media database.  It lives only in a
0600 app-state file so independent CLI invocations can use one native-approved
pairing window.  The current OS user remains inside the B1 trust boundary.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

from memolens_contracts import MemoLensError


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_ACTIONS = (
    "blueprint.commit_proposal",
    "blueprint.restore_revision",
    "timeline.apply_edit",
    "timeline.apply_structural_edit",
    "timeline.restore_revision",
    "timeline.preview_media",
)
_STATUSES = frozenset({"pending", "active", "denied", "expired", "revoked", "unavailable"})
_MAX_CREDENTIAL_BYTES = 16_384
_CREDENTIAL_FIELDS = frozenset(
    {
        "object",
        "schema_version",
        "base_url",
        "project_id",
        "pairing_id",
        "capability_id",
        "subject_id",
        "claimed_client_label",
        "proof_secret",
        "database_uuid",
        "actions",
        "created_at",
        "expires_at",
        "status",
    }
)


def _clean_state_dir(value: str | None) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser().resolve()


def agent_state_dir() -> Path:
    """Return the same canonical app-state family used by MemoLens Desktop."""

    configured = _clean_state_dir(os.getenv("MEMOLENS_APP_STATE_DIR"))
    if configured is not None:
        return configured
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support/MemoLens").resolve()
    if os.name == "nt":
        appdata = _clean_state_dir(os.getenv("APPDATA"))
        return ((appdata or Path.home() / "AppData/Roaming") / "MemoLens").resolve()
    xdg_state = _clean_state_dir(os.getenv("XDG_STATE_HOME"))
    return ((xdg_state or Path.home() / ".local/state") / "MemoLens").resolve()


def _credential_directory() -> Path:
    return agent_state_dir() / "agent-credentials"


def _credential_filename(project_id: str) -> str:
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:32]
    return f"project-{digest}.json"


def _credential_path(project_id: str) -> Path:
    return _credential_directory() / _credential_filename(project_id)


def _require_identifier(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise MemoLensError(
            f"Stored Agent credential field `{field}` is invalid.",
            code="agent_credential_invalid",
        )
    return value


def _validate_credential(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _CREDENTIAL_FIELDS:
        raise MemoLensError(
            "Stored Agent credential has an unsupported shape.",
            code="agent_credential_invalid",
        )
    credential = dict(value)
    if credential["object"] != "memolens.agent_project_credential" or credential[
        "schema_version"
    ] != "1":
        raise MemoLensError(
            "Stored Agent credential uses an unsupported contract.",
            code="agent_credential_invalid",
        )
    _require_identifier(credential["project_id"], "project_id")
    _require_identifier(credential["pairing_id"], "pairing_id")
    _require_identifier(credential["capability_id"], "capability_id", nullable=True)
    _require_identifier(credential["subject_id"], "subject_id")
    database_uuid = credential["database_uuid"]
    if database_uuid is not None and (
        type(database_uuid) is not str or not 1 <= len(database_uuid) <= 200
    ):
        raise MemoLensError(
            "Stored Agent credential database identity is invalid.",
            code="agent_credential_invalid",
        )
    if type(credential["proof_secret"]) is not str or _HEX_256.fullmatch(
        credential["proof_secret"]
    ) is None:
        raise MemoLensError(
            "Stored Agent credential proof is invalid.",
            code="agent_credential_invalid",
        )
    actions = credential["actions"]
    if (
        type(actions) is not list
        or not actions
        or tuple(actions) != tuple(action for action in _ALLOWED_ACTIONS if action in actions)
        or len(actions) != len(set(actions))
    ):
        raise MemoLensError(
            "Stored Agent credential scope is invalid.",
            code="agent_credential_invalid",
        )
    if credential["status"] not in _STATUSES:
        raise MemoLensError(
            "Stored Agent credential status is invalid.",
            code="agent_credential_invalid",
        )
    for field, maximum in (
        ("base_url", 2048),
        ("claimed_client_label", 120),
        ("created_at", 64),
        ("expires_at", 64),
    ):
        item = credential[field]
        if type(item) is not str or not 1 <= len(item) <= maximum:
            raise MemoLensError(
                f"Stored Agent credential field `{field}` is invalid.",
                code="agent_credential_invalid",
            )
    return credential


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        identity = os.lstat(path)
    except OSError as exc:
        raise MemoLensError(
            "Agent credential directory is unavailable.",
            code="agent_credential_unavailable",
        ) from exc
    if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
        raise MemoLensError(
            "Agent credential directory is not a private directory.",
            code="agent_credential_insecure",
        )
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise MemoLensError(
            "Agent credential directory permissions could not be restricted.",
            code="agent_credential_insecure",
        ) from exc
    if os.name != "nt" and stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode) & 0o077:
        raise MemoLensError(
            "Agent credential directory permissions are too broad.",
            code="agent_credential_insecure",
        )


def save_agent_credential(value: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically persist one validated credential without returning its secret."""

    credential = _validate_credential(dict(value))
    directory = _credential_directory()
    _ensure_private_directory(directory)
    target = _credential_path(credential["project_id"])
    try:
        existing = os.lstat(target)
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise MemoLensError(
            "Agent credential target could not be inspected.",
            code="agent_credential_unavailable",
        ) from exc
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
    ):
        raise MemoLensError(
            "Agent credential target is unsafe.",
            code="agent_credential_insecure",
        )

    serialized = json.dumps(
        credential,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(serialized) > 16_384:
        raise MemoLensError(
            "Agent credential exceeded the local safety limit.",
            code="agent_credential_invalid",
        )
    temporary = directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise MemoLensError(
            "Agent credential could not be stored privately.",
            code="agent_credential_unavailable",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return public_credential_summary(credential)


def load_agent_credential(project_id: str) -> dict[str, Any]:
    normalized = _require_identifier(project_id, "project_id")
    assert isinstance(normalized, str)
    path = _credential_path(normalized)
    descriptor: int | None = None
    try:
        identity = os.lstat(path)
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISREG(identity.st_mode):
            raise MemoLensError(
                "Agent credential target is unsafe.",
                code="agent_credential_insecure",
            )
        if os.name != "nt" and stat.S_IMODE(identity.st_mode) & 0o077:
            raise MemoLensError(
                "Agent credential file permissions are too broad.",
                code="agent_credential_insecure",
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino)
        ):
            raise MemoLensError(
                "Agent credential target changed while it was being opened.",
                code="agent_credential_insecure",
            )
        if os.name != "nt" and stat.S_IMODE(opened.st_mode) & 0o077:
            raise MemoLensError(
                "Agent credential file permissions are too broad.",
                code="agent_credential_insecure",
            )
        chunks: list[bytes] = []
        remaining = _MAX_CREDENTIAL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except FileNotFoundError as exc:
        raise MemoLensError(
            "No paired Agent credential exists for this project. Run `agent-pair` first.",
            code="agent_pairing_required",
        ) from exc
    except MemoLensError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MemoLensError(
                "Agent credential target is unsafe.",
                code="agent_credential_insecure",
            ) from exc
        raise MemoLensError(
            "Agent credential could not be read.",
            code="agent_credential_unavailable",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > _MAX_CREDENTIAL_BYTES:
        raise MemoLensError(
            "Stored Agent credential exceeded the local safety limit.",
            code="agent_credential_invalid",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MemoLensError(
            "Stored Agent credential is not valid JSON.",
            code="agent_credential_invalid",
        ) from exc
    credential = _validate_credential(payload)
    if credential["project_id"] != normalized:
        raise MemoLensError(
            "Stored Agent credential project binding is invalid.",
            code="agent_credential_invalid",
        )
    return credential


def public_credential_summary(credential: Mapping[str, Any]) -> dict[str, Any]:
    """Return the only credential fields safe for normal Agent output."""

    return {
        "object": "memolens.agent_pairing",
        "schema_version": "1",
        "project_id": credential.get("project_id"),
        "pairing_id": credential.get("pairing_id"),
        "capability_id": credential.get("capability_id"),
        "claimed_client_label": credential.get("claimed_client_label"),
        "client_identity_verified": False,
        "actions": list(credential.get("actions") or []),
        "status": credential.get("status"),
        "expires_at": credential.get("expires_at"),
        "proof_secret_exposed": False,
        "credential_storage": "restricted_app_state_file",
    }


__all__ = [
    "agent_state_dir",
    "load_agent_credential",
    "public_credential_summary",
    "save_agent_credential",
]
