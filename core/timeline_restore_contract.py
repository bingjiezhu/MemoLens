"""Pure append-only restore contract for one canonical Timeline ledger.

The selected historical revision never becomes the parent of the new write.
The current head remains the CAS parent, while the historical payload is
re-enveloped with ``revision=N+1`` and ``parent=N``.  Persistence, authority,
source availability, and idempotency remain responsibilities of the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .timeline_lowering_contract import (
    TimelineLoweringContractError,
    canonical_timeline_content_sha256,
    require_current_canonical_timeline,
    require_current_timeline_source_bindings,
)


TIMELINE_RESTORE_MAX_REVISION = 1_800_000


@dataclass(frozen=True)
class TimelineRestoreContractError(ValueError):
    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]):
        normalized = tuple(
            {
                "code": str(item.get("code") or "timeline_restore_target_invalid"),
                "path": str(item.get("path") or ""),
                "message": str(
                    item.get("message") or "Canonical Timeline restore is invalid."
                ),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"]
            if normalized
            else "Canonical Timeline restore is invalid.",
        )


def _raise(code: str, path: str, message: str) -> None:
    raise TimelineRestoreContractError(
        [{"code": code, "path": path, "message": message}]
    )


def _validated_revision(
    timeline: object,
    source_bindings: object,
    *,
    path: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        validated_timeline = require_current_canonical_timeline(timeline)
        validated_sources = require_current_timeline_source_bindings(
            source_bindings,
            timeline=validated_timeline,
        )
    except TimelineLoweringContractError as exc:
        message = (
            exc.errors[0]["message"]
            if exc.errors
            else "Canonical Timeline revision is invalid."
        )
        _raise("timeline_restore_target_invalid", path, message)
    return validated_timeline, validated_sources


def restore_canonical_timeline(
    *,
    current_timeline: Mapping[str, object],
    current_source_bindings: Sequence[Mapping[str, object]],
    restore_timeline: Mapping[str, object],
    restore_source_bindings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Return the exact historical payload re-enveloped as current N+1."""

    current, _current_sources = _validated_revision(
        current_timeline,
        current_source_bindings,
        path="/current_timeline",
    )
    target, target_sources = _validated_revision(
        restore_timeline,
        restore_source_bindings,
        path="/restore_timeline",
    )
    current_revision = int(current["revision"])
    target_revision = int(target["revision"])
    if target_revision >= current_revision:
        _raise(
            "timeline_restore_target_invalid",
            "/restore_timeline/revision",
            "Restore target must be older than the exact current Timeline head.",
        )
    if (
        target["project_id"] != current["project_id"]
        or target["timeline_id"] != current["timeline_id"]
        or target["blueprint_binding"] != current["blueprint_binding"]
        or target["coverage_binding"] != current["coverage_binding"]
    ):
        _raise(
            "timeline_restore_binding_mismatch",
            "/restore_timeline",
            "Restore target and current head must share one project, Timeline, Blueprint, and Coverage binding.",
        )
    next_revision = current_revision + 1
    if next_revision > TIMELINE_RESTORE_MAX_REVISION:
        _raise(
            "timeline_restore_target_invalid",
            "/current_timeline/revision",
            "Timeline revision exceeds the canonical restore limit.",
        )

    restored = deepcopy(target)
    restored["revision"] = next_revision
    restored["parent"] = {
        "revision": current_revision,
        "content_sha256": canonical_timeline_content_sha256(current),
    }
    restored_sources = deepcopy(target_sources)
    try:
        require_current_canonical_timeline(restored)
        require_current_timeline_source_bindings(
            restored_sources,
            timeline=restored,
        )
    except TimelineLoweringContractError as exc:
        message = (
            exc.errors[0]["message"]
            if exc.errors
            else "Restored canonical Timeline is invalid."
        )
        _raise("timeline_restore_target_invalid", "", message)
    return {"timeline": restored, "source_bindings": restored_sources}


def require_exact_timeline_restore_replay(
    timeline: object,
    *,
    source_bindings: object,
    current_timeline: Mapping[str, object],
    current_source_bindings: Sequence[Mapping[str, object]],
    restore_timeline: Mapping[str, object],
    restore_source_bindings: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    """Cold-audit a restore revision against its exact N and K inputs."""

    try:
        persisted, persisted_sources = _validated_revision(
            timeline,
            source_bindings,
            path="/timeline",
        )
        replay = restore_canonical_timeline(
            current_timeline=current_timeline,
            current_source_bindings=current_source_bindings,
            restore_timeline=restore_timeline,
            restore_source_bindings=restore_source_bindings,
        )
    except TimelineRestoreContractError as exc:
        raise TimelineRestoreContractError(
            [
                {
                    "code": "timeline_restore_replay_mismatch",
                    "path": "",
                    "message": "Persisted Timeline cannot be reproduced from the exact restore inputs.",
                }
            ]
        ) from exc
    if replay["timeline"] != persisted or replay["source_bindings"] != persisted_sources:
        _raise(
            "timeline_restore_replay_mismatch",
            "",
            "Persisted Timeline is not the exact result of the supplied restore inputs.",
        )
    return persisted


__all__ = [
    "TIMELINE_RESTORE_MAX_REVISION",
    "TimelineRestoreContractError",
    "require_exact_timeline_restore_replay",
    "restore_canonical_timeline",
]
