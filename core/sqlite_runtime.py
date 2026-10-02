"""SQLite runtime admission for MemoLens writable WAL workloads.

The shared policy artifact is the only version allowlist.  Python's version is
not used as a proxy: distributors choose which SQLite library an interpreter
loads, so admission must inspect ``sqlite3.sqlite_version_info`` at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import platform
import sqlite3
from typing import Any


SQLITE_WAL_RESET_UNSAFE = "sqlite_wal_reset_unsafe"
_POLICY_PATH = Path(__file__).with_name("sqlite_runtime_policy.json")
_POLICY_KEYS = {
    "object",
    "schema_version",
    "policy_id",
    "sqlite_major",
    "backport_minimums",
    "mainline_minimum",
    "withdrawn_branches",
}


@dataclass(frozen=True)
class _SQLiteRuntimePolicy:
    policy_id: str
    sqlite_major: int
    backport_minimums: tuple[tuple[int, int, int], ...]
    mainline_minimum: tuple[int, int, int]
    withdrawn_branches: tuple[tuple[int, int], ...]


def _version_triplet(version: object) -> tuple[int, int, int] | None:
    if not isinstance(version, Sequence) or isinstance(version, (str, bytes, bytearray)):
        return None
    if len(version) != 3:
        return None
    components = tuple(version)
    if any(type(component) is not int or component < 0 for component in components):
        return None
    major, minor, patch = components
    return major, minor, patch


def _branch_pair(version: object) -> tuple[int, int] | None:
    if not isinstance(version, Sequence) or isinstance(version, (str, bytes, bytearray)):
        return None
    if len(version) != 2:
        return None
    components = tuple(version)
    if any(type(component) is not int or component < 0 for component in components):
        return None
    major, minor = components
    return major, minor


def _parse_sqlite_runtime_policy(payload: object) -> _SQLiteRuntimePolicy | None:
    if not isinstance(payload, dict) or set(payload) != _POLICY_KEYS:
        return None
    if payload.get("object") != "memolens.sqlite_runtime_policy":
        return None
    if payload.get("schema_version") != "1":
        return None

    policy_id = payload.get("policy_id")
    sqlite_major = payload.get("sqlite_major")
    if not isinstance(policy_id, str) or not policy_id:
        return None
    if type(sqlite_major) is not int or sqlite_major < 0:
        return None

    raw_backports = payload.get("backport_minimums")
    raw_withdrawn = payload.get("withdrawn_branches")
    if not isinstance(raw_backports, list) or not isinstance(raw_withdrawn, list):
        return None

    backports = tuple(_version_triplet(version) for version in raw_backports)
    withdrawn = tuple(_branch_pair(branch) for branch in raw_withdrawn)
    mainline = _version_triplet(payload.get("mainline_minimum"))
    if (
        mainline is None
        or any(version is None for version in backports)
        or any(branch is None for branch in withdrawn)
    ):
        return None

    typed_backports = tuple(version for version in backports if version is not None)
    typed_withdrawn = tuple(branch for branch in withdrawn if branch is not None)
    backport_branches = tuple(version[:2] for version in typed_backports)
    if (
        mainline[0] != sqlite_major
        or any(version[0] != sqlite_major for version in typed_backports)
        or any(branch[0] != sqlite_major for branch in typed_withdrawn)
        or len(set(backport_branches)) != len(backport_branches)
        or len(set(typed_withdrawn)) != len(typed_withdrawn)
        or any(branch in typed_withdrawn for branch in backport_branches)
        or mainline[:2] in typed_withdrawn
        or any(version >= mainline for version in typed_backports)
    ):
        return None

    return _SQLiteRuntimePolicy(
        policy_id=policy_id,
        sqlite_major=sqlite_major,
        backport_minimums=typed_backports,
        mainline_minimum=mainline,
        withdrawn_branches=typed_withdrawn,
    )


def _load_sqlite_runtime_policy() -> _SQLiteRuntimePolicy | None:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate SQLite runtime policy key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            _POLICY_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, ValueError):
        return None
    return _parse_sqlite_runtime_policy(payload)


_SQLITE_RUNTIME_POLICY = _load_sqlite_runtime_policy()


def is_wal_reset_safe(version: object) -> bool:
    """Return whether an SQLite release contains the official WAL-reset fix."""

    normalized = _version_triplet(version)
    policy = _SQLITE_RUNTIME_POLICY
    if normalized is None or policy is None:
        return False
    if normalized[0] != policy.sqlite_major:
        return False
    if normalized[:2] in policy.withdrawn_branches:
        return False
    for minimum in policy.backport_minimums:
        if normalized[:2] == minimum[:2]:
            return normalized >= minimum
    return normalized >= policy.mainline_minimum


def sqlite_runtime_remediation() -> str:
    """Return the single policy-derived remediation shown by every caller."""

    policy = _SQLITE_RUNTIME_POLICY
    if policy is None:
        return (
            "Restore or reinstall MemoLens so its SQLite runtime policy artifact "
            "can be validated, then choose a runtime admitted by that policy."
        )

    fixed_branches = [
        f"{major}.{minor}.{patch}+"
        for major, minor, patch in policy.backport_minimums
    ]
    mainline = ".".join(str(component) for component in policy.mainline_minimum)
    alternatives = ", ".join(fixed_branches)
    if alternatives:
        alternatives = f"a fixed {alternatives} patch branch, or "
    withdrawn = ", ".join(
        ".".join(str(component) for component in branch)
        for branch in policy.withdrawn_branches
    )
    withdrawn_note = f"; the {withdrawn} line is withdrawn" if withdrawn else ""
    return f"Use {alternatives}SQLite {mainline} and later{withdrawn_note}."


def sqlite_runtime_error_message(capability: Mapping[str, Any]) -> str:
    """Build the canonical unsafe-runtime message without caller-owned policy text."""

    sqlite_version = capability.get("sqlite_version", "unknown")
    return (
        "MemoLens refuses writable WAL access with SQLite "
        f"{sqlite_version}: this runtime lacks the WAL-reset corruption fix. "
        f"{sqlite_runtime_remediation()}"
    )


def sqlite_runtime_capability(
    *,
    python_version: str | None = None,
    sqlite_version: str | None = None,
    sqlite_version_info: object | None = None,
) -> dict[str, object]:
    """Build the bounded runtime fact exposed to setup and health checks."""

    resolved_python_version = python_version or platform.python_version()
    resolved_sqlite_version = sqlite_version or sqlite3.sqlite_version
    resolved_version_info = (
        sqlite3.sqlite_version_info
        if sqlite_version_info is None
        else sqlite_version_info
    )
    wal_reset_safe = is_wal_reset_safe(resolved_version_info)
    policy = _SQLITE_RUNTIME_POLICY
    return {
        "object": "memolens.sqlite_runtime",
        "schema_version": "1",
        "policy_id": policy.policy_id if policy is not None else None,
        "python_version": resolved_python_version,
        "sqlite_version": resolved_sqlite_version,
        "wal_reset_safe": wal_reset_safe,
        "journal_policy": "wal",
        "status": "ready" if wal_reset_safe else "unsafe",
        "reason_code": None if wal_reset_safe else SQLITE_WAL_RESET_UNSAFE,
    }


class UnsafeSQLiteRuntimeError(RuntimeError):
    """Raised before a writable MemoLens runtime can touch its SQLite file."""

    code = SQLITE_WAL_RESET_UNSAFE

    def __init__(self, capability: Mapping[str, Any]):
        self.capability = dict(capability)
        super().__init__(sqlite_runtime_error_message(capability))


def require_safe_sqlite_runtime() -> dict[str, object]:
    """Return an admitted capability or fail closed with a stable error code."""

    # Always sample the current process.  A caller-supplied diagnostic payload
    # would otherwise become a way to attest its own runtime.
    resolved = sqlite_runtime_capability()
    if resolved.get("wal_reset_safe") is not True:
        raise UnsafeSQLiteRuntimeError(resolved)
    return resolved
