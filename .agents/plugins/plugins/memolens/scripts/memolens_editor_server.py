#!/usr/bin/env python3
"""Loopback-only, process-scoped editor handoff shared by Agent adapters."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from memolens_agent_client import AgentApiError, AgentPreviewMediaResponse
from memolens_canonical_editor import (
    CANONICAL_EDITOR_OBJECT,
    CANONICAL_EDITOR_ORIGIN,
    CANONICAL_EDITOR_SCHEMA_VERSION,
    TIMELINE_EDIT_ACTION,
    TIMELINE_PREVIEW_ACTION,
    TIMELINE_RESTORE_ACTION,
    TIMELINE_STRUCTURAL_EDIT_ACTION,
    CanonicalEditorError,
    CanonicalEditorHistoricalSnapshot,
    CanonicalEditorSnapshot,
    PairedCanonicalEditorBackend,
    closed_canonical_edit,
    closed_canonical_structural_edit,
    paired_canonical_editor_backend,
    validate_saved_restore_result,
    validate_saved_result,
    validate_saved_structural_result,
)
from memolens_contracts import MemoLensError
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json
from memolens_text_safety import contains_private_locator
from memolens_timeline import (
    MAX_TIMELINE_DURATION_MS,
    TimelineInputError,
    revise_timeline_draft,
    validate_timeline,
)


EDITOR_OBJECT = "memolens.editor_handoff"
EDITOR_SCHEMA_VERSION = "1"
EDITOR_SESSION_TTL_SECONDS = 30 * 60
EDITOR_BOOT_TTL_SECONDS = 120
EDITOR_MAX_SESSIONS = 16
EDITOR_MAX_HISTORY = 50
EDITOR_MAX_REQUEST_BYTES = 256 * 1024
EDITOR_MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
EDITOR_MEDIA_STREAM_CHUNK_BYTES = 64 * 1024
EDITOR_COOKIE = "memolens_editor_session"
LEGACY_EDITOR_ACTIONS = frozenset({"apply", "redo", "undo"})
CANONICAL_EDITOR_ACTIONS = frozenset(
    {
        "discard",
        "refresh",
        "save",
        "select_revision",
        "stage",
        "stage_restore",
        "stage_structural",
    }
)
CANONICAL_EDITOR_EDIT_ACTIONS = frozenset(
    {"move_clip", "replace_clip", "set_clip_duration", "trim_clip"}
)
EDITOR_HTML_PATH = Path(__file__).resolve().parents[1] / "ui" / "editor.html"
CANONICAL_EDITOR_HTML_PATH = (
    Path(__file__).resolve().parents[1] / "ui" / "canonical-editor.html"
)
EDITOR_JSON_LIMITS = StrictJsonLimits(
    max_bytes=EDITOR_MAX_REQUEST_BYTES,
    max_depth=32,
    max_nodes=20_000,
    max_object_items=1_000,
    max_array_items=1_000,
    max_string_chars=64_000,
)
_CANONICAL_REFRESHABLE_CONFLICT_CODES = {
    "blueprint_head_conflict",
    "coverage_head_conflict",
    "database_identity_changed",
    "timeline_head_conflict",
    "timeline_restore_binding_mismatch",
    "timeline_restore_source_stale",
    "timeline_restore_target_invalid",
}
_PREVIEW_REMINT_CODES = {"agent_preview_lease_expired"}
_BROWSER_PREVIEW_REASON_CODES = {
    "agent_preview_head_changed",
    "agent_preview_lease_expired",
    "agent_preview_media_unsupported",
    "agent_preview_rate_limited",
    "agent_preview_scope_denied",
    "agent_preview_source_changed",
    "canonical_editor_history_read_only",
    "canonical_editor_evidence_non_current",
    "canonical_editor_pending_source_change",
    "service_unavailable",
}
_REPLACEMENT_BASE_FIELDS = {
    "kind",
    "label",
    "asset_id",
    "asset_source_id",
    "asset_sha256",
    "reason",
    "match_id",
}
_REPLACEMENT_KIND_FIELDS = {
    "video": {
        "segment_id",
        "analysis_run_id",
        "analysis_revision",
        "source_in_ms",
        "source_out_ms",
    },
    "image": set(),
    "audio": {"source_in_ms", "source_out_ms"},
}
_REPLACEMENT_REQUIRED_FIELDS = {
    "video": {
        "kind",
        "asset_id",
        "asset_source_id",
        "asset_sha256",
        "segment_id",
        "analysis_run_id",
        "analysis_revision",
        "source_in_ms",
        "source_out_ms",
    },
    "image": {"kind", "asset_id", "asset_source_id", "asset_sha256"},
    "audio": {
        "kind",
        "asset_id",
        "asset_source_id",
        "asset_sha256",
        "source_in_ms",
        "source_out_ms",
    },
}
_REPLACEMENT_OPERATION_FIELDS = {
    "asset_id",
    "asset_source_id",
    "asset_sha256",
    "segment_id",
    "analysis_run_id",
    "analysis_revision",
    "source_in_ms",
    "source_out_ms",
    "reason",
    "match_id",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_RANGE_NUMBER_RE = re.compile(r"[0-9]+")
_MAX_SOURCE_TIME_MS = 2_147_483_647


class EditorServerError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = deepcopy(details) if details is not None else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _replacement_error(index: int, field: str, message: str) -> EditorServerError:
    return EditorServerError(
        "invalid_replacement_candidate",
        f"replacement_candidates[{index}].{field} {message}",
    )


def _replacement_id(value: Any, *, index: int, field: str) -> str:
    if not isinstance(value, str):
        raise _replacement_error(index, field, "must be a stable identifier.")
    normalized = value.strip()
    if not _ID_RE.fullmatch(normalized) or contains_private_locator(normalized):
        raise _replacement_error(
            index,
            field,
            "must be a path-free stable identifier of at most 200 characters.",
        )
    return normalized


def _replacement_sha256(value: Any, *, index: int) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise _replacement_error(index, "asset_sha256", "must be a SHA-256 digest.")
    return value.strip().lower()


def _replacement_text(
    value: Any,
    *,
    index: int,
    field: str,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise _replacement_error(index, field, "must be text.")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise _replacement_error(
            index,
            field,
            f"must contain 1-{maximum} characters after whitespace normalization.",
        )
    if contains_private_locator(normalized):
        raise _replacement_error(index, field, "must not contain a private locator.")
    return normalized


def _replacement_int(
    value: Any,
    *,
    index: int,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise _replacement_error(
            index,
            field,
            f"must be an integer between {minimum} and {maximum}.",
        )
    return value


def _normalize_replacements(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 24:
        raise EditorServerError(
            "invalid_replacement_candidates",
            "replacement_candidates must contain at most 24 items.",
        )
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise EditorServerError(
                "invalid_replacement_candidate",
                f"replacement_candidates[{index}] must be an object.",
            )
        kind = raw.get("kind")
        if kind not in _REPLACEMENT_KIND_FIELDS:
            raise EditorServerError(
                "invalid_replacement_candidate",
                f"replacement_candidates[{index}].kind must be video, image, or audio.",
            )
        allowed = _REPLACEMENT_BASE_FIELDS | _REPLACEMENT_KIND_FIELDS[kind]
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise EditorServerError(
                "invalid_replacement_candidate",
                f"replacement_candidates[{index}] contains unsupported fields: "
                + ", ".join(unknown),
            )
        missing = sorted(_REPLACEMENT_REQUIRED_FIELDS[kind] - set(raw))
        if missing:
            raise EditorServerError(
                "invalid_replacement_candidate",
                f"replacement_candidates[{index}] lacks required {kind} fields: "
                + ", ".join(missing),
            )
        candidate: dict[str, Any] = {
            "kind": kind,
            "asset_id": _replacement_id(raw["asset_id"], index=index, field="asset_id"),
            "asset_source_id": _replacement_id(
                raw["asset_source_id"], index=index, field="asset_source_id"
            ),
            "asset_sha256": _replacement_sha256(raw["asset_sha256"], index=index),
        }
        if kind == "video":
            candidate["segment_id"] = _replacement_id(
                raw["segment_id"], index=index, field="segment_id"
            )
            candidate["analysis_run_id"] = _replacement_id(
                raw["analysis_run_id"], index=index, field="analysis_run_id"
            )
            candidate["analysis_revision"] = _replacement_int(
                raw["analysis_revision"],
                index=index,
                field="analysis_revision",
                minimum=1,
                maximum=1_000_000,
            )
        if kind in {"video", "audio"}:
            source_in = _replacement_int(
                raw["source_in_ms"],
                index=index,
                field="source_in_ms",
                minimum=0,
                maximum=_MAX_SOURCE_TIME_MS - 1,
            )
            source_out = _replacement_int(
                raw["source_out_ms"],
                index=index,
                field="source_out_ms",
                minimum=1,
                maximum=_MAX_SOURCE_TIME_MS,
            )
            if source_out <= source_in:
                raise _replacement_error(
                    index, "source_out_ms", "must exceed source_in_ms."
                )
            if source_out - source_in > MAX_TIMELINE_DURATION_MS:
                raise _replacement_error(
                    index,
                    "source_out_ms",
                    f"must define a range no longer than {MAX_TIMELINE_DURATION_MS} ms.",
                )
            candidate["source_in_ms"] = source_in
            candidate["source_out_ms"] = source_out
        for field_name, maximum in (("reason", 500), ("match_id", 200)):
            if field_name in raw:
                candidate[field_name] = _replacement_text(
                    raw[field_name],
                    index=index,
                    field=field_name,
                    maximum=maximum,
                )
        if "label" in raw:
            candidate["label"] = _replacement_text(
                raw["label"], index=index, field="label", maximum=120
            )
        else:
            candidate["label"] = str(
                candidate.get("reason")
                or candidate.get("match_id")
                or candidate["asset_id"]
            )[:120]
        normalized.append(candidate)
    return normalized


def _browser_revision_head(value: dict[str, Any]) -> dict[str, Any]:
    """Project one canonical revision without its write/proof identity."""

    return {"revision": value["revision"]}


def _browser_timeline_head(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only the stable Timeline label and revision needed by the UI."""

    return {
        "revision": value["revision"],
        "timeline_id": value["timeline_id"],
    }


def _browser_clip(value: dict[str, Any]) -> dict[str, Any]:
    """Closed timing projection; all media/proof identity stays server-side."""

    clip = {
        "clip_id": value["clip_id"],
        "ordinal": value["ordinal"],
        "beat_id": value["beat_id"],
        "assignment_id": value["assignment_id"],
        "start_ms": value["start_ms"],
        "end_ms": value["end_ms"],
        "media_kind": value["media_kind"],
        "fit": value["fit"],
        "audio_enabled": value["audio_enabled"],
    }
    if value["media_kind"] == "video":
        clip.update(
            {
                "source_in_ms": value["source_in_ms"],
                "source_out_ms": value["source_out_ms"],
            }
        )
    return clip


def _browser_timeline(value: dict[str, Any]) -> dict[str, Any]:
    track = value["tracks"][0]
    return {
        "object": "memolens.canonical_timeline_timing_projection",
        "schema_version": "1",
        "timeline_id": value["timeline_id"],
        "project_id": value["project_id"],
        "revision": value["revision"],
        "output": {
            "duration_ms": value["output"]["duration_ms"],
            "aspect_ratio": value["output"]["aspect_ratio"],
        },
        "tracks": [
            {
                "track_id": track["track_id"],
                "kind": track["kind"],
                "clips": [_browser_clip(clip) for clip in track["clips"]],
            }
        ],
    }


def _browser_replacement_candidates(
    value: dict[str, Any],
    *,
    timeline: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Expose Stage route identities and timing facts, never source identity."""

    clip_ids = [clip["clip_id"] for clip in timeline["tracks"][0]["clips"]]
    return {
        clip_id: [
            {
                "assignment_id": candidate["assignment_id"],
                "media_kind": candidate["media_kind"],
                "current_slot_duration_ms": candidate["current_slot_duration_ms"],
                "available_duration_ms": candidate["available_duration_ms"],
            }
            for candidate in value.get(clip_id, [])
        ]
        for clip_id in clip_ids
    }


def _browser_content_preview(
    value: dict[str, Any],
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    result = {
        "canonical_timing_exact": True,
        "identity_redacted": True,
        "fixed_execution_source_proven": False,
        "media_playback": value.get("media_playback") is True,
        "verified_source_preview": value.get("verified_source_preview") is True,
    }
    for capability in ("media_thumbnail", "replacement_thumbnail"):
        if capability in value:
            result[capability] = value[capability] is True
    if read_only:
        result["read_only"] = True
    return result


def _browser_canonical_projection(value: dict[str, Any]) -> dict[str, Any]:
    timeline = _browser_timeline(value["timeline"])
    return {
        "object": "memolens.canonical_editor_projection",
        "schema_version": "1",
        "origin": CANONICAL_EDITOR_ORIGIN,
        "project_id": value["project_id"],
        "blueprint_head": _browser_revision_head(value["blueprint_head"]),
        "coverage_head": _browser_revision_head(value["coverage_head"]),
        "timeline_head": _browser_timeline_head(value["timeline_head"]),
        "timeline": timeline,
        "evidence_currency": value["evidence_currency"],
        "eligible_for_current_use": value["eligible_for_current_use"] is True,
        "reason_code": value.get("reason_code"),
        "replacement_candidates": _browser_replacement_candidates(
            value["replacement_candidates"],
            timeline=timeline,
        ),
        "content_preview": _browser_content_preview(value["content_preview"]),
    }


def _browser_historical_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "memolens.canonical_editor_historical_projection",
        "schema_version": "1",
        "origin": CANONICAL_EDITOR_ORIGIN,
        "project_id": value["project_id"],
        "current_timeline_head": _browser_timeline_head(
            value["current_timeline_head"]
        ),
        "selected_revision": _browser_timeline_head(value["selected_revision"]),
        "freshness": {"state": value["freshness"]["state"]},
        "restore_eligible": value["restore_eligible"] is True,
        "evidence_currency": value["evidence_currency"],
        "eligible_for_current_use": value["eligible_for_current_use"] is True,
        "reason_code": value.get("reason_code"),
        "timeline": _browser_timeline(value["timeline"]),
        "content_preview": _browser_content_preview(
            value["content_preview"],
            read_only=True,
        ),
    }


def _browser_pending_preview(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "object": "canonical_timeline.pending_preview",
        "schema_version": "1",
        "project_id": value["project_id"],
        "based_on_head": _browser_timeline_head(value["based_on_head"]),
        "timeline": _browser_timeline(value["timeline"]),
        "canonical_timing_exact": True,
        "identity_redacted": True,
        "fixed_execution_source_proven": False,
    }


def _browser_last_save(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result = {
        "status": value["status"],
        "command_type": value["command_type"],
        "revision": value["revision"],
    }
    if "restore_from_revision" in value:
        result["restore_from_revision"] = value["restore_from_revision"]
    return result


def _replacement_track_kind(timeline: dict[str, Any], clip_id: Any) -> str | None:
    if not isinstance(clip_id, str):
        return None
    for track in timeline.get("tracks", []):
        if any(clip.get("id") == clip_id for clip in track.get("clips", [])):
            kind = track.get("type")
            return kind if kind in _REPLACEMENT_KIND_FIELDS else None
    return None


def _validate_replacement_operations(
    session: "_EditorSession",
    operations: Any,
) -> None:
    if not isinstance(operations, list):
        return
    for operation_index, raw in enumerate(operations):
        if not isinstance(raw, dict) or raw.get("op") != "replace_clip":
            continue
        kind = _replacement_track_kind(session.timeline, raw.get("clip_id"))
        if kind is None:
            raise EditorServerError(
                "invalid_replacement_operation",
                f"operations[{operation_index}] does not target a replaceable clip.",
                status=422,
            )
        supplied = {
            key: raw[key]
            for key in _REPLACEMENT_OPERATION_FIELDS
            if key in raw
        }
        allowed_candidates = [
            {
                key: candidate[key]
                for key in _REPLACEMENT_OPERATION_FIELDS
                if key in candidate
            }
            for candidate in session.replacement_candidates
            if candidate["kind"] == kind
        ]
        if supplied not in allowed_candidates:
            raise EditorServerError(
                "invalid_replacement_operation",
                f"operations[{operation_index}] must exactly match a supplied {kind} "
                "replacement candidate.",
                status=422,
            )


@dataclass
class _EditorSession:
    session_id: str
    timeline: dict[str, Any]
    replacement_candidates: list[dict[str, Any]]
    boot_token_sha256: str
    boot_expires_at: float
    expires_at: float
    auth_token_sha256: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    future: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        validation = validate_timeline(self.timeline)
        replacement_kinds = {
            candidate["kind"] for candidate in self.replacement_candidates
        }
        replacement_available = any(
            track.get("type") in replacement_kinds and bool(track.get("clips"))
            for track in self.timeline.get("tracks", [])
        )
        return {
            "object": "memolens.editor_session",
            "schema_version": EDITOR_SCHEMA_VERSION,
            "status": "ready",
            "experience": "unsaved_draft_lab",
            "title": "MemoLens Unsaved Draft Lab",
            "session_id": self.session_id,
            "persistence": "not_saved",
            "timeline": deepcopy(self.timeline),
            "validation": validation,
            "replacement_candidates": deepcopy(self.replacement_candidates),
            "history": {
                "undo_available": bool(self.history),
                "redo_available": bool(self.future),
                "undo_depth": len(self.history),
                "redo_depth": len(self.future),
            },
            "capabilities": {
                "split": True,
                "trim": True,
                "move": True,
                "replace": replacement_available,
                "delete": True,
                "volume": True,
                "fit": True,
                "aspect_ratio": True,
                "undo_redo": True,
                "drag_reorder": True,
                "media_playback": False,
                "persist_timeline": False,
                "render": False,
                "export": False,
            },
            "safety": {
                "loopback_only": True,
                "process_scoped": True,
                "timeline_persisted": False,
                "media_modified": False,
                "rendered": False,
                "exported": False,
                "source_paths_loaded": False,
                "storage_paths_introduced": False,
            },
        }


@dataclass
class _CanonicalEditorSession:
    session_id: str
    backend: PairedCanonicalEditorBackend
    snapshot: CanonicalEditorSnapshot
    boot_token_sha256: str
    boot_expires_at: float
    expires_at: float
    auth_token_sha256: str | None = None
    historical_snapshot: CanonicalEditorHistoricalSnapshot | None = None
    pending_edit: dict[str, object] | None = None
    pending_structural_edit: dict[str, object] | None = None
    pending_preview: dict[str, Any] | None = None
    pending_restore: dict[str, object] | None = None
    last_save: dict[str, Any] | None = None
    preview_lease: dict[str, Any] | None = field(default=None, repr=False)
    preview_error_code: str | None = None
    preview_epoch: int = 0
    active_preview_streams: set[AgentPreviewMediaResponse] = field(
        default_factory=set,
        repr=False,
    )

    def _browser_playback(self) -> dict[str, Any]:
        clips = []
        if self.preview_lease is not None:
            for row in self.preview_lease.get("clips", []):
                if isinstance(row, dict):
                    clips.append(
                        {
                            "clip_id": row["clip_id"],
                            "status": row["status"],
                            "reason_code": row["reason_code"],
                        }
                    )
        playable = any(row["status"] == "playable" for row in clips)
        reason = self.preview_error_code
        if not playable and reason is None:
            reason = (
                "agent_preview_media_unsupported"
                if bool(getattr(self.backend, "can_preview", False))
                else "agent_preview_scope_denied"
            )
        return {
            "object": "memolens.canonical_source_preview",
            "schema_version": "2",
            "mode": "verified_source_preview",
            "output_muted": True,
            "audio_playback_enabled": False,
            "transport_may_include_audio": True,
            "audio_stream_attested": False,
            "final_fidelity": False,
            "clips": clips,
            "available": playable,
            "reason_code": None if playable else reason,
        }

    def public(self) -> dict[str, Any]:
        current_evidence = (
            self.snapshot.public.get("eligible_for_current_use") is True
        )
        can_edit = current_evidence and bool(getattr(self.backend, "can_edit", True))
        can_structural_edit = current_evidence and bool(
            getattr(self.backend, "can_structural_edit", False)
        )
        can_restore = current_evidence and bool(
            getattr(self.backend, "can_restore", False)
        )
        can_preview = current_evidence and bool(
            getattr(self.backend, "can_preview", False)
        )
        playback = self._browser_playback()
        media_playback = can_preview and playback["available"] is True
        can_thumbnail = callable(getattr(self.backend, "thumbnail_for_clip", None))
        can_replacement_thumbnail = callable(
            getattr(self.backend, "thumbnail_for_replacement", None)
        ) and current_evidence
        private_projection = deepcopy(self.snapshot.public)
        private_content_preview = private_projection.get("content_preview")
        if isinstance(private_content_preview, dict):
            private_content_preview["media_thumbnail"] = can_thumbnail
            private_content_preview["replacement_thumbnail"] = (
                can_replacement_thumbnail
            )
            private_content_preview["media_playback"] = media_playback
            private_content_preview["verified_source_preview"] = media_playback
        projection = _browser_canonical_projection(private_projection)
        pending: dict[str, Any] | None
        if self.pending_edit is not None:
            pending = {
                "object": "memolens.canonical_editor_pending_edit",
                "schema_version": "1",
                "based_on_head": deepcopy(
                    _browser_timeline_head(self.snapshot.expected_timeline_head)
                ),
                "edit": deepcopy(self.pending_edit),
                "preview": _browser_pending_preview(self.pending_preview),
                "canonical": False,
                "persisted": False,
            }
        elif self.pending_structural_edit is not None:
            pending = {
                "object": "memolens.canonical_editor_pending_structural_edit",
                "schema_version": "1",
                "based_on_head": deepcopy(
                    _browser_timeline_head(self.snapshot.expected_timeline_head)
                ),
                "structural_edit": deepcopy(self.pending_structural_edit),
                "preview": _browser_pending_preview(self.pending_preview),
                "canonical": False,
                "persisted": False,
            }
        elif self.pending_restore is not None:
            pending = {
                "object": "memolens.canonical_editor_pending_restore",
                "schema_version": "1",
                "based_on_head": deepcopy(
                    _browser_timeline_head(self.snapshot.expected_timeline_head)
                ),
                "restore_from": _browser_timeline_head(self.pending_restore),
                "canonical": False,
                "persisted": False,
            }
        else:
            pending = None
        return {
            "object": "memolens.canonical_editor_session",
            "schema_version": CANONICAL_EDITOR_SCHEMA_VERSION,
            "status": "ready",
            "session_id": self.session_id,
            "persistence": "save_creates_new_canonical_revision",
            "projection": projection,
            "historical": (
                None
                if self.historical_snapshot is None
                else _browser_historical_projection(
                    self.historical_snapshot.public
                )
            ),
            "pending": pending,
            "last_save": _browser_last_save(self.last_save),
            "playback": playback,
            "capabilities": {
                "move_clip": can_edit,
                "trim_clip": can_edit,
                "set_clip_duration": can_edit,
                "replace_clip": can_edit,
                "split_clip": can_structural_edit,
                "delete_clip": can_structural_edit,
                # History selection is a read-only projection, not write
                # authority. The separate restore action gates only staging
                # and saving K as N+1.
                "inspect_history": True,
                "restore_revision": can_restore,
                "discard": True,
                "refresh": True,
                "save": can_edit or can_structural_edit or can_restore,
                "media_thumbnail": can_thumbnail,
                "replacement_thumbnail": can_replacement_thumbnail,
                "media_playback": media_playback,
                "render": False,
                "export": False,
                "legacy_draft_operations": False,
            },
            "authority": {
                "actions": [
                    action
                    for action, admitted in (
                        (TIMELINE_EDIT_ACTION, can_edit),
                        (TIMELINE_STRUCTURAL_EDIT_ACTION, can_structural_edit),
                        (TIMELINE_RESTORE_ACTION, can_restore),
                        (TIMELINE_PREVIEW_ACTION, can_preview),
                    )
                    if admitted
                ],
                "origin": "agent_plugin_editor",
                "vendor_identity_verified": False,
                "user_authority_verified": False,
                "semantic_confirmation": False,
                "credential_location": "server_only",
            },
            "safety": {
                "loopback_only": True,
                "process_scoped_pending": True,
                "canonical_head_persisted": True,
                "current_image_evidence_verified": current_evidence,
                "pending_persisted": False,
                "media_modified": False,
                "original_media_unchanged": True,
                "rendered": False,
                "exported": False,
                "source_paths_loaded_by_browser": False,
                "verified_thumbnail_bytes_proxied": can_thumbnail,
                "verified_replacement_thumbnail_bytes_proxied": (
                    can_replacement_thumbnail
                ),
                "verified_source_preview_bytes_proxied": media_playback,
                "source_preview_output_muted": True,
                "source_preview_audio_playback_enabled": False,
                "source_preview_transport_may_include_audio": True,
                "source_preview_audio_stream_attested": False,
                "source_preview_final_fidelity": False,
                "pairing_secret_exposed": False,
                "desktop_token_exposed": False,
                "main_token_exposed": False,
            },
        }


class EditorServerManager:
    """Own one loopback server and bounded editor sessions for one MCP process."""

    def __init__(
        self,
        *,
        session_ttl_seconds: int = EDITOR_SESSION_TTL_SECONDS,
        boot_ttl_seconds: int = EDITOR_BOOT_TTL_SECONDS,
    ) -> None:
        self._session_ttl_seconds = session_ttl_seconds
        self._boot_ttl_seconds = boot_ttl_seconds
        self._lock = threading.RLock()
        self._sessions: dict[
            str, _EditorSession | _CanonicalEditorSession
        ] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._origin: str | None = None

    @property
    def origin(self) -> str | None:
        return self._origin

    @staticmethod
    def _preview_block_reason(session: _CanonicalEditorSession) -> str | None:
        if session.snapshot.public.get("eligible_for_current_use") is not True:
            return "canonical_editor_evidence_non_current"
        if session.historical_snapshot is not None:
            return "canonical_editor_history_read_only"
        if session.pending_restore is not None:
            return "canonical_editor_pending_source_change"
        if session.pending_structural_edit is not None:
            return "canonical_editor_pending_source_change"
        if (
            session.pending_edit is not None
            and session.pending_edit.get("op") == "replace_clip"
        ):
            return "canonical_editor_pending_source_change"
        return None

    @staticmethod
    def _drop_preview(session: _CanonicalEditorSession, reason: str | None) -> None:
        lease = session.preview_lease
        streams = tuple(session.active_preview_streams)
        session.active_preview_streams.clear()
        for stream in streams:
            stream.close()
        session.preview_lease = None
        session.preview_epoch += 1
        session.preview_error_code = (
            reason if reason in _BROWSER_PREVIEW_REASON_CODES else None
        )
        provider = getattr(session.backend, "drop_preview_lease", None)
        if callable(provider):
            provider(lease)
        elif lease is not None:
            lease.clear()

    @classmethod
    def _mint_preview(cls, session: _CanonicalEditorSession) -> None:
        cls._drop_preview(session, None)
        blocked = cls._preview_block_reason(session)
        if blocked is not None:
            session.preview_error_code = blocked
            return
        if not bool(getattr(session.backend, "can_preview", False)):
            session.preview_error_code = "agent_preview_scope_denied"
            return
        provider = getattr(session.backend, "mint_preview_lease", None)
        if not callable(provider):
            session.preview_error_code = "service_unavailable"
            return
        try:
            lease = provider(snapshot=session.snapshot)
        except CanonicalEditorError as exc:
            session.preview_error_code = (
                exc.code
                if exc.code in _BROWSER_PREVIEW_REASON_CODES
                else "service_unavailable"
            )
            return
        except MemoLensError as exc:
            session.preview_error_code = (
                exc.code
                if exc.code in _BROWSER_PREVIEW_REASON_CODES
                else "service_unavailable"
            )
            return
        if not isinstance(lease, dict):
            session.preview_error_code = "service_unavailable"
            return
        session.preview_lease = lease
        session.preview_epoch += 1
        session.preview_error_code = (
            None
            if any(
                isinstance(row, dict) and row.get("status") == "playable"
                for row in lease.get("clips", [])
            )
            else "agent_preview_media_unsupported"
        )

    @staticmethod
    def _local_preview_lease_expired(lease: dict[str, Any]) -> bool:
        issued_raw = lease.get("issued_at")
        expires_raw = lease.get("expires_at")
        if (
            type(issued_raw) is not str
            or type(expires_raw) is not str
            or not issued_raw.endswith("Z")
            or not expires_raw.endswith("Z")
            or len(issued_raw) > 64
            or len(expires_raw) > 64
        ):
            return True
        try:
            issued = datetime.fromisoformat(issued_raw[:-1] + "+00:00")
            expires = datetime.fromisoformat(expires_raw[:-1] + "+00:00")
        except ValueError:
            return True
        now = datetime.now(timezone.utc)
        if (
            issued.tzinfo != timezone.utc
            or expires.tzinfo != timezone.utc
            or not issued < expires <= issued + timedelta(seconds=90)
            or issued < now - timedelta(minutes=2)
            or issued > now + timedelta(seconds=30)
        ):
            return True
        return now >= expires

    def _ensure_server(self) -> str:
        with self._lock:
            if self._server is not None and self._origin is not None:
                return self._origin
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EditorRequestHandler)
            server.daemon_threads = True
            server.editor_manager = self  # type: ignore[attr-defined]
            port = int(server.server_address[1])
            self._server = server
            self._origin = f"http://127.0.0.1:{port}"
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="memolens-editor-loopback",
                daemon=True,
            )
            self._thread.start()
            return self._origin

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if isinstance(session, _CanonicalEditorSession):
                self._drop_preview(session, "agent_preview_lease_expired")
        while len(self._sessions) >= EDITOR_MAX_SESSIONS:
            oldest = min(
                self._sessions.values(),
                key=lambda item: item.expires_at,
            )
            removed = self._sessions.pop(oldest.session_id, None)
            if isinstance(removed, _CanonicalEditorSession):
                self._drop_preview(removed, "agent_preview_lease_expired")

    def create_handoff(
        self,
        *,
        timeline: Any,
        replacement_candidates: Any = None,
    ) -> dict[str, Any]:
        validation = validate_timeline(timeline)
        if not validation["valid"]:
            first = validation["errors"][0]
            raise EditorServerError(
                "invalid_timeline",
                f"{first['field']}: {first['message']}",
                status=422,
            )
        origin = self._ensure_server()
        boot_token = secrets.token_urlsafe(32)
        session_id = f"edit_{secrets.token_urlsafe(18)}"
        now = time.monotonic()
        session = _EditorSession(
            session_id=session_id,
            timeline=deepcopy(timeline),
            replacement_candidates=_normalize_replacements(replacement_candidates),
            boot_token_sha256=_sha256(boot_token),
            boot_expires_at=now + self._boot_ttl_seconds,
            expires_at=now + self._boot_ttl_seconds,
        )
        with self._lock:
            self._prune()
            self._sessions[session_id] = session
        editor_url = (
            f"{origin}/editor/{quote(session_id, safe='')}"
            f"#boot={quote(boot_token, safe='')}"
        )
        return {
            "object": EDITOR_OBJECT,
            "schema_version": EDITOR_SCHEMA_VERSION,
            "status": "ready",
            "surface": "loopback-browser",
            "experience": "unsaved_draft_lab",
            "title": "MemoLens Unsaved Draft Lab",
            "session_id": session_id,
            "timeline_id": timeline["id"],
            "timeline_revision": timeline["revision"],
            "editor_url": editor_url,
            "uiHandoff": {
                "type": "browser",
                "url": editor_url,
                "requiresUserGesture": True,
            },
            # Compatibility projection for the installed Codex adapter. New
            # Agent adapters should consume the neutral uiHandoff value above.
            "browserHandoff": {
                "url": editor_url,
                "preferredMode": "codex-internal-browser",
            },
            "expires_in_seconds": self._session_ttl_seconds,
            "boot_token_expires_in_seconds": self._boot_ttl_seconds,
            "persistence": "not_saved",
            "next_step": (
                "Open uiHandoff.url in a trusted Agent browser surface, or present it "
                "as a user-clicked link when the host cannot open pages directly. The "
                "Unsaved Draft Lab is Not saved and process-scoped; it never saves a "
                "canonical Timeline."
            ),
            "safety": {
                "loopback_only": True,
                "single_use_boot_token": True,
                "timeline_persisted": False,
                "media_modified": False,
                "rendered": False,
                "exported": False,
                "source_paths_loaded": False,
                "storage_paths_introduced": False,
            },
        }

    def create_canonical_handoff(
        self,
        *,
        backend: PairedCanonicalEditorBackend,
    ) -> dict[str, Any]:
        try:
            snapshot = backend.read()
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        origin = self._ensure_server()
        boot_token = secrets.token_urlsafe(32)
        session_id = f"canonical_{secrets.token_urlsafe(18)}"
        now = time.monotonic()
        session = _CanonicalEditorSession(
            session_id=session_id,
            backend=backend,
            snapshot=snapshot,
            boot_token_sha256=_sha256(boot_token),
            boot_expires_at=now + self._boot_ttl_seconds,
            expires_at=now + self._boot_ttl_seconds,
        )
        self._mint_preview(session)
        with self._lock:
            self._prune()
            self._sessions[session_id] = session
        editor_url = (
            f"{origin}/canonical-editor/{quote(session_id, safe='')}"
            f"#boot={quote(boot_token, safe='')}"
        )
        head = snapshot.expected_timeline_head
        admitted_actions = [
            action
            for action, admitted in (
                (TIMELINE_EDIT_ACTION, bool(getattr(backend, "can_edit", True))),
                (
                    TIMELINE_STRUCTURAL_EDIT_ACTION,
                    bool(getattr(backend, "can_structural_edit", False)),
                ),
                (
                    TIMELINE_RESTORE_ACTION,
                    bool(getattr(backend, "can_restore", False)),
                ),
                (
                    TIMELINE_PREVIEW_ACTION,
                    bool(getattr(backend, "can_preview", False)),
                ),
            )
            if admitted and snapshot.public.get("eligible_for_current_use") is True
        ]
        return {
            "object": CANONICAL_EDITOR_OBJECT,
            "schema_version": CANONICAL_EDITOR_SCHEMA_VERSION,
            "status": "ready",
            "surface": "loopback-browser",
            "session_id": session_id,
            "project_id": snapshot.public["project_id"],
            "timeline_head": _browser_timeline_head(head),
            "editor_url": editor_url,
            "uiHandoff": {
                "type": "browser",
                "url": editor_url,
                "requiresUserGesture": True,
            },
            "browserHandoff": {
                "url": editor_url,
                "preferredMode": "codex-internal-browser",
            },
            "expires_in_seconds": self._session_ttl_seconds,
            "boot_token_expires_in_seconds": self._boot_ttl_seconds,
            "persistence": "save_creates_new_canonical_revision",
            "next_step": (
                "Open the exact uiHandoff.url through a trusted Agent browser "
                "surface. A staged Timeline change is not canonical until explicit Save, "
                "paired admission, and an exact canonical reread succeed."
            ),
            "authority": {
                "actions": admitted_actions,
                "origin": "agent_plugin_editor",
                "credential_location": "server_only",
                "vendor_identity_verified": False,
                "user_authority_verified": False,
                "semantic_confirmation": False,
            },
            "safety": {
                "loopback_only": True,
                "single_use_boot_token": True,
                "caller_timeline_accepted": False,
                "caller_source_identity_accepted": False,
                "pairing_secret_exposed": False,
                "desktop_token_exposed": False,
                "main_token_exposed": False,
                "source_paths_exposed": False,
                "pending_persisted": False,
                "original_media_unchanged": True,
                "rendered": False,
                "exported": False,
            },
        }

    def _session(
        self, session_id: str
    ) -> _EditorSession | _CanonicalEditorSession:
        with self._lock:
            self._prune()
            session = self._sessions.get(session_id)
            if session is None:
                raise EditorServerError(
                    "editor_session_not_found",
                    "The editor session is missing or expired.",
                    status=404,
                )
            return session

    def bootstrap(self, session_id: str, token: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(token, str) or not token:
            raise EditorServerError(
                "editor_boot_token_invalid",
                "The editor boot token is invalid.",
                status=401,
            )
        with self._lock:
            session = self._session(session_id)
            if (
                time.monotonic() > session.boot_expires_at
                or not session.boot_token_sha256
                or not hmac.compare_digest(session.boot_token_sha256, _sha256(token))
            ):
                raise EditorServerError(
                    "editor_boot_token_invalid",
                    "The editor boot token is invalid, expired, or already used.",
                    status=401,
                )
            auth_token = secrets.token_urlsafe(32)
            session.auth_token_sha256 = _sha256(auth_token)
            session.boot_token_sha256 = ""
            if (
                isinstance(session, _CanonicalEditorSession)
                and session.preview_lease is not None
                and self._local_preview_lease_expired(session.preview_lease)
            ):
                self._mint_preview(session)
            # The cookie and server session share one fixed lifetime from bootstrap.
            # Requests do not slide one clock while the other remains fixed.
            session.expires_at = time.monotonic() + self._session_ttl_seconds
            return auth_token, session.public()

    def authenticate(
        self, session_id: str, token: str | None
    ) -> _EditorSession | _CanonicalEditorSession:
        if not token:
            raise EditorServerError(
                "editor_session_unauthorized",
                "The editor session cookie is missing.",
                status=401,
            )
        session = self._session(session_id)
        expected = session.auth_token_sha256 or ""
        if not expected or not hmac.compare_digest(expected, _sha256(token)):
            raise EditorServerError(
                "editor_session_unauthorized",
                "The editor session cookie is invalid.",
                status=401,
            )
        return session

    @staticmethod
    def _thumbnail_snapshot(
        session: _CanonicalEditorSession,
        expected_revision: int | None,
    ) -> CanonicalEditorSnapshot:
        snapshot = session.snapshot
        if expected_revision is not None and (
            type(expected_revision) is not int
            or expected_revision != snapshot.public["timeline_head"]["revision"]
        ):
            raise EditorServerError(
                "agent_preview_head_changed",
                "The requested thumbnail version is not the current editor head.",
                status=409,
            )
        # The revision segment is a cache identity and current-head assertion,
        # never a historical source selector. Pin the exact checked snapshot.
        return snapshot

    def thumbnail(
        self,
        session_id: str,
        token: str | None,
        clip_id: str,
        *,
        expected_revision: int | None = None,
    ) -> bytes:
        session = self.authenticate(session_id, token)
        if _ID_RE.fullmatch(clip_id) is None:
            raise EditorServerError(
                "canonical_editor_media_unavailable",
                "The requested clip has no verified thumbnail.",
                status=404,
            )
        if not isinstance(session, _CanonicalEditorSession):
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        snapshot = self._thumbnail_snapshot(session, expected_revision)
        provider = getattr(session.backend, "thumbnail_for_clip", None)
        if not callable(provider):
            raise EditorServerError(
                "canonical_editor_media_unavailable",
                "The canonical editor has no verified thumbnail adapter.",
                status=404,
            )
        try:
            body = provider(snapshot=snapshot, clip_id=clip_id)
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except MemoLensError as exc:
            raise EditorServerError(
                "canonical_editor_media_unavailable",
                "The verified clip thumbnail is unavailable.",
                status=503,
            ) from exc
        if (
            not isinstance(body, bytes)
            or not body
            or len(body) > EDITOR_MAX_THUMBNAIL_BYTES
        ):
            raise EditorServerError(
                "canonical_editor_media_unavailable",
                "The verified clip thumbnail is unavailable.",
                status=503,
            )
        return body

    def replacement_thumbnail(
        self,
        session_id: str,
        token: str | None,
        clip_id: str,
        assignment_id: str,
        *,
        expected_revision: int | None = None,
    ) -> bytes:
        session = self.authenticate(session_id, token)
        if not isinstance(session, _CanonicalEditorSession):
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        if (
            _ID_RE.fullmatch(clip_id) is None
            or _ID_RE.fullmatch(assignment_id) is None
        ):
            raise EditorServerError(
                "canonical_editor_replacement_unavailable",
                "The requested replacement has no verified thumbnail.",
                status=404,
            )
        snapshot = self._thumbnail_snapshot(session, expected_revision)
        candidates = snapshot.public.get("replacement_candidates")
        eligible = candidates.get(clip_id) if isinstance(candidates, dict) else None
        timeline = snapshot.public.get("timeline")
        tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
        clips = (
            tracks[0].get("clips")
            if isinstance(tracks, list)
            and tracks
            and isinstance(tracks[0], dict)
            else None
        )
        current_clip = isinstance(clips, list) and any(
            isinstance(clip, dict) and clip.get("clip_id") == clip_id
            for clip in clips
        )
        if (
            not current_clip
            or not isinstance(eligible, list)
            or not any(
                isinstance(candidate, dict)
                and candidate.get("assignment_id") == assignment_id
                for candidate in eligible
            )
        ):
            raise EditorServerError(
                "canonical_editor_replacement_unavailable",
                "The requested replacement has no verified thumbnail.",
                status=404,
            )
        provider = getattr(session.backend, "thumbnail_for_replacement", None)
        if not callable(provider):
            raise EditorServerError(
                "canonical_editor_replacement_unavailable",
                "The canonical editor has no verified replacement thumbnail adapter.",
                status=404,
            )
        try:
            body = provider(
                snapshot=snapshot,
                clip_id=clip_id,
                assignment_id=assignment_id,
            )
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except MemoLensError as exc:
            raise EditorServerError(
                "canonical_editor_replacement_unavailable",
                "The verified replacement thumbnail is unavailable.",
                status=503,
            ) from exc
        if (
            not isinstance(body, bytes)
            or not body
            or len(body) > EDITOR_MAX_THUMBNAIL_BYTES
        ):
            raise EditorServerError(
                "canonical_editor_replacement_unavailable",
                "The verified replacement thumbnail is unavailable.",
                status=503,
            )
        return body

    @staticmethod
    def _preview_unavailable(session: _CanonicalEditorSession) -> EditorServerError:
        code = session.preview_error_code or "agent_preview_lease_expired"
        status = {
            "agent_preview_media_unsupported": 415,
            "agent_preview_scope_denied": 403,
            "canonical_editor_history_read_only": 409,
            "canonical_editor_evidence_non_current": 409,
            "canonical_editor_pending_source_change": 409,
            "service_unavailable": 503,
        }.get(code, 403)
        return EditorServerError(
            code,
            "The exact current clip has no active canonical source preview grant.",
            status=status,
        )

    @staticmethod
    def _require_current_video_clip(
        session: _CanonicalEditorSession,
        clip_id: str,
    ) -> None:
        timeline = session.snapshot.public.get("timeline")
        tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
        clips = (
            tracks[0].get("clips")
            if isinstance(tracks, list)
            and tracks
            and isinstance(tracks[0], dict)
            else None
        )
        if not isinstance(clips, list) or not any(
            isinstance(clip, dict)
            and clip.get("clip_id") == clip_id
            and clip.get("media_kind") == "video"
            for clip in clips
        ):
            raise EditorServerError(
                "canonical_editor_media_unavailable",
                "The requested clip has no current canonical video preview.",
                status=404,
            )

    def authorize_clip_media_request(
        self,
        session_id: str,
        token: str | None,
        clip_id: str,
    ) -> None:
        """Bind even locally rejected Range requests to a current session clip."""

        with self._lock:
            session = self.authenticate(session_id, token)
            if not isinstance(session, _CanonicalEditorSession):
                raise EditorServerError(
                    "editor_route_not_found",
                    "Editor route not found.",
                    status=404,
                )
            if _ID_RE.fullmatch(clip_id) is None:
                raise EditorServerError(
                    "canonical_editor_media_unavailable",
                    "The requested clip has no current canonical video preview.",
                    status=404,
                )
            self._require_current_video_clip(session, clip_id)

    def clip_media(
        self,
        session_id: str,
        token: str | None,
        clip_id: str,
        *,
        method: str,
        range_header: str | None,
    ) -> AgentPreviewMediaResponse:
        """Open a current-session upstream stream without holding manager lock."""

        if method not in {"GET", "HEAD"}:
            raise EditorServerError(
                "agent_preview_range_invalid",
                "Only one valid Timeline preview byte range is supported.",
                status=416,
            )
        if _ID_RE.fullmatch(clip_id) is None:
            raise EditorServerError(
                "canonical_editor_media_unavailable",
                "The requested clip has no current canonical video preview.",
                status=404,
            )
        with self._lock:
            session = self.authenticate(session_id, token)
            if not isinstance(session, _CanonicalEditorSession):
                raise EditorServerError(
                    "editor_route_not_found",
                    "Editor route not found.",
                    status=404,
                )
            self._require_current_video_clip(session, clip_id)
            if (
                range_header is not None
                and (
                    type(range_header) is not str
                    or not range_header.strip()
                    or len(range_header.strip()) > 64
                    or any(
                        ord(character) < 0x20 or ord(character) > 0x7E
                        for character in range_header.strip()
                    )
                    or any(
                        len(group) > 19
                        for group in _RANGE_NUMBER_RE.findall(range_header)
                    )
                )
            ):
                raise EditorServerError(
                    "agent_preview_range_invalid",
                    "Timeline preview Range is too large or unsafe.",
                    status=416,
                )
            blocked = self._preview_block_reason(session)
            if blocked is not None:
                self._drop_preview(session, blocked)
                raise self._preview_unavailable(session)
            if (
                session.preview_lease is not None
                and self._local_preview_lease_expired(session.preview_lease)
            ):
                self._mint_preview(session)
            if session.preview_lease is None:
                raise self._preview_unavailable(session)
            provider = getattr(session.backend, "preview_media", None)
            if not callable(provider):
                self._drop_preview(session, "service_unavailable")
                raise self._preview_unavailable(session)
            lease = deepcopy(session.preview_lease)
            snapshot = session.snapshot
            epoch = session.preview_epoch

        def open_upstream() -> AgentPreviewMediaResponse:
            try:
                result = provider(
                    snapshot=snapshot,
                    lease=lease,
                    clip_id=clip_id,
                    method=method,
                    range_header=range_header,
                )
            except CanonicalEditorError as exc:
                known = exc.code in _BROWSER_PREVIEW_REASON_CODES
                code = exc.code if known else "service_unavailable"
                status = (
                    exc.status
                    if known
                    and exc.status in {401, 403, 404, 409, 415, 416, 429, 503}
                    else 503
                )
                raise EditorServerError(
                    code,
                    "The canonical source preview was rejected before streaming.",
                    status=status,
                ) from exc
            except MemoLensError as exc:
                raise EditorServerError(
                    "service_unavailable",
                    "The canonical source preview is unavailable.",
                    status=503,
                ) from exc
            if not isinstance(result, AgentPreviewMediaResponse):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise EditorServerError(
                    "canonical_editor_media_unavailable",
                    "The canonical source preview adapter returned an invalid stream.",
                    status=503,
                )
            return result

        try:
            upstream = open_upstream()
        except EditorServerError as exc:
            can_remint = (
                exc.code in _PREVIEW_REMINT_CODES
                and self._local_preview_lease_expired(lease)
            )
            with self._lock:
                current = self._sessions.get(session_id)
                unchanged = (
                    current is session
                    and session.snapshot is snapshot
                    and session.preview_epoch == epoch
                    and self._preview_block_reason(session) is None
                )
                if not unchanged:
                    raise
                if not can_remint:
                    self._drop_preview(
                        session,
                        exc.code
                        if exc.code in _BROWSER_PREVIEW_REASON_CODES
                        else "agent_preview_lease_expired",
                    )
                    raise
                self._mint_preview(session)
                if session.preview_lease is None:
                    raise self._preview_unavailable(session) from exc
                lease = deepcopy(session.preview_lease)
                epoch = session.preview_epoch
            # Exactly one TTL-expiry retry. Budget, concurrency, revoke,
            # runtime and capability failures never enter this branch.
            try:
                upstream = open_upstream()
            except EditorServerError as retry_exc:
                with self._lock:
                    if self._sessions.get(session_id) is session:
                        self._drop_preview(
                            session,
                            retry_exc.code
                            if retry_exc.code in _BROWSER_PREVIEW_REASON_CODES
                            else "agent_preview_lease_expired",
                        )
                raise

        with self._lock:
            current = self._sessions.get(session_id)
            current_lease_id = (
                session.preview_lease.get("lease_id")
                if session.preview_lease is not None
                else None
            )
            if (
                current is not session
                or session.snapshot is not snapshot
                or session.preview_epoch != epoch
                or self._preview_block_reason(session) is not None
                or current_lease_id != lease.get("lease_id")
            ):
                upstream.close()
                raise EditorServerError(
                    "agent_preview_head_changed",
                    "Canonical editor state changed before preview response admission.",
                    status=409,
                )
            session.active_preview_streams.add(upstream)
        return upstream

    def release_preview_stream(
        self,
        session_id: str,
        upstream: AgentPreviewMediaResponse,
    ) -> None:
        """Forget a completed/aborted private stream without reviving state."""

        with self._lock:
            session = self._sessions.get(session_id)
            if isinstance(session, _CanonicalEditorSession):
                session.active_preview_streams.discard(upstream)

    def apply_action(
        self,
        session_id: str,
        token: str | None,
        payload: Any,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise EditorServerError(
                "invalid_editor_action",
                "Editor action must contain a type.",
            )
        with self._lock:
            session = self.authenticate(session_id, token)
            if isinstance(session, _CanonicalEditorSession):
                return self._apply_canonical_action(session, payload)
            action = payload["type"]
            # Keep these literal comparisons in the transport-adjacent method:
            # production-surface discovery treats them as the closed registry.
            if action == "apply" or action == "undo" or action == "redo":
                return self._apply_legacy_action(session, payload)
            raise EditorServerError(
                "invalid_editor_action",
                "Editor action must be apply, undo, or redo.",
            )

    @staticmethod
    def _apply_legacy_action(
        session: _EditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        action = payload["type"]
        if action not in LEGACY_EDITOR_ACTIONS:
            raise EditorServerError(
                "invalid_editor_action",
                "Editor action must be apply, undo, or redo.",
            )
        if action == "apply":
            if set(payload) != {"type", "operations", "created_at"}:
                raise EditorServerError(
                    "invalid_editor_action",
                    "Apply accepts only operations and created_at.",
                )
            _validate_replacement_operations(session, payload["operations"])
            try:
                revised = revise_timeline_draft(
                    timeline=session.timeline,
                    operations=payload["operations"],
                    created_at=payload["created_at"],
                )
            except TimelineInputError as exc:
                raise EditorServerError(
                    "invalid_timeline_operation",
                    f"{exc.field}: {exc}",
                    status=422,
                ) from exc
            session.history.append(deepcopy(session.timeline))
            session.history = session.history[-EDITOR_MAX_HISTORY:]
            session.timeline = revised
            session.future.clear()
        elif action == "undo":
            if set(payload) != {"type"}:
                raise EditorServerError(
                    "invalid_editor_action",
                    "Undo accepts no additional fields.",
                )
            if not session.history:
                raise EditorServerError(
                    "undo_unavailable",
                    "There is no earlier editor state.",
                    status=409,
                )
            session.future.append(deepcopy(session.timeline))
            session.timeline = session.history.pop()
        else:
            if set(payload) != {"type"}:
                raise EditorServerError(
                    "invalid_editor_action",
                    "Redo accepts no additional fields.",
                )
            if not session.future:
                raise EditorServerError(
                    "redo_unavailable",
                    "There is no later editor state.",
                    status=409,
                )
            session.history.append(deepcopy(session.timeline))
            session.timeline = session.future.pop()
        return session.public()

    @staticmethod
    def _stage_canonical_edit(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type", "edit"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Stage accepts exactly one closed edit.",
                status=422,
            )
        if session.snapshot.public.get("eligible_for_current_use") is not True:
            raise EditorServerError(
                "canonical_editor_evidence_non_current",
                "Identity-only image evidence is audit-only and cannot be edited or saved.",
                status=409,
            )
        if not bool(getattr(session.backend, "can_edit", True)):
            raise EditorServerError(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical Timeline edits.",
                status=403,
            )
        if (
            session.pending_restore is not None
            or session.pending_structural_edit is not None
        ):
            raise EditorServerError(
                "canonical_editor_pending_conflict",
                "Discard the pending Timeline restore or structural edit before staging an edit.",
                status=409,
            )
        try:
            edit, preview = closed_canonical_edit(
                payload["edit"], snapshot=session.snapshot
            )
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        session.pending_edit = edit
        session.pending_preview = preview
        session.last_save = None
        if edit.get("op") == "replace_clip":
            EditorServerManager._drop_preview(
                session,
                "canonical_editor_pending_source_change",
            )
        return session.public()

    @staticmethod
    def _stage_canonical_structural_edit(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type", "structural_edit"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Structural Stage accepts exactly one closed structural_edit.",
                status=422,
            )
        if session.snapshot.public.get("eligible_for_current_use") is not True:
            raise EditorServerError(
                "canonical_editor_evidence_non_current",
                "Identity-only image evidence is audit-only and cannot authorize structural edits.",
                status=409,
            )
        if not bool(getattr(session.backend, "can_structural_edit", False)):
            raise EditorServerError(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical Timeline structural edits.",
                status=403,
            )
        if (
            session.pending_edit is not None
            or session.pending_restore is not None
            or session.pending_structural_edit is not None
        ):
            raise EditorServerError(
                "canonical_editor_pending_conflict",
                "Discard the pending Timeline change before staging a structural edit.",
                status=409,
            )
        try:
            structural_edit, preview = closed_canonical_structural_edit(
                payload["structural_edit"],
                snapshot=session.snapshot,
            )
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        session.pending_structural_edit = structural_edit
        session.pending_preview = preview
        session.last_save = None
        EditorServerManager._drop_preview(
            session,
            "canonical_editor_pending_source_change",
        )
        return session.public()

    @staticmethod
    def _select_canonical_revision(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type", "revision"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Historical selection accepts only a revision number.",
                status=422,
            )
        if (
            session.pending_edit is not None
            or session.pending_structural_edit is not None
            or session.pending_restore is not None
        ):
            raise EditorServerError(
                "canonical_editor_pending_conflict",
                "Discard the pending Timeline change before inspecting another revision.",
                status=409,
            )
        EditorServerManager._drop_preview(
            session,
            "canonical_editor_history_read_only",
        )
        try:
            session.historical_snapshot = session.backend.read_revision(
                payload["revision"],
                current=session.snapshot,
            )
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except MemoLensError as exc:
            raise EditorServerError(exc.code, str(exc), status=503) from exc
        session.last_save = None
        return session.public()

    @staticmethod
    def _stage_canonical_restore(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Stage restore accepts no caller-supplied Timeline identity.",
                status=422,
            )
        if session.snapshot.public.get("eligible_for_current_use") is not True:
            raise EditorServerError(
                "canonical_editor_evidence_non_current",
                "Identity-only current image evidence cannot authorize a restore.",
                status=409,
            )
        if not bool(getattr(session.backend, "can_restore", False)):
            raise EditorServerError(
                "agent_pairing_scope_denied",
                "The active pairing does not allow canonical Timeline restore.",
                status=403,
            )
        if (
            session.pending_edit is not None
            or session.pending_structural_edit is not None
        ):
            raise EditorServerError(
                "canonical_editor_pending_conflict",
                "Discard the pending Timeline edit or structural edit before staging a restore.",
                status=409,
            )
        historical = session.historical_snapshot
        if historical is None:
            raise EditorServerError(
                "canonical_editor_history_required",
                "Inspect one verified historical revision before staging a restore.",
                status=409,
            )
        if not historical.restore_eligible:
            raise EditorServerError(
                "canonical_editor_history_not_restorable",
                "This historical revision is read-only because its current bindings or sources do not match N.",
                status=409,
            )
        if (
            historical.public.get("current_timeline_head")
            != session.snapshot.expected_timeline_head
        ):
            raise EditorServerError(
                "canonical_editor_history_mismatch",
                "Historical selection no longer belongs to the exact current head.",
                status=409,
            )
        session.pending_restore = deepcopy(historical.restore_from)
        session.pending_preview = None
        session.last_save = None
        EditorServerManager._drop_preview(
            session,
            "canonical_editor_pending_source_change",
        )
        return session.public()

    @staticmethod
    def _discard_canonical_pending(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Discard accepts no additional fields.",
            )
        if (
            session.pending_edit is None
            and session.pending_structural_edit is None
            and session.pending_restore is None
        ):
            raise EditorServerError(
                "canonical_editor_pending_required",
                "There is no pending Timeline change to discard.",
                status=409,
            )
        remint_current = (
            session.pending_structural_edit is not None
            or (
                session.pending_restore is None
                and session.pending_edit is not None
                and session.pending_edit.get("op") == "replace_clip"
            )
        )
        session.pending_edit = None
        session.pending_structural_edit = None
        session.pending_preview = None
        session.pending_restore = None
        session.last_save = None
        if (
            session.historical_snapshot is None
            and (remint_current or session.preview_lease is None)
        ):
            EditorServerManager._mint_preview(session)
        elif session.historical_snapshot is not None:
            EditorServerManager._drop_preview(
                session,
                "canonical_editor_history_read_only",
            )
        return session.public()

    @staticmethod
    def _refresh_canonical_session(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Refresh accepts no additional fields.",
            )
        if (
            session.pending_edit is not None
            or session.pending_structural_edit is not None
            or session.pending_restore is not None
        ):
            raise EditorServerError(
                "canonical_editor_pending_conflict",
                "Discard the pending Timeline change before refreshing the canonical head.",
                status=409,
            )
        EditorServerManager._drop_preview(
            session,
            "agent_preview_head_changed",
        )
        try:
            session.snapshot = session.backend.read()
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except MemoLensError as exc:
            raise EditorServerError(exc.code, str(exc), status=503) from exc
        session.historical_snapshot = None
        session.last_save = None
        EditorServerManager._mint_preview(session)
        return session.public()

    @staticmethod
    def _save_canonical_change(
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if set(payload) != {"type"}:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Save accepts no additional fields.",
            )
        if session.snapshot.public.get("eligible_for_current_use") is not True:
            raise EditorServerError(
                "canonical_editor_evidence_non_current",
                "Identity-only image evidence is audit-only and cannot be saved.",
                status=409,
            )
        if (
            session.pending_edit is None
            and session.pending_structural_edit is None
            and session.pending_restore is None
        ):
            raise EditorServerError(
                "canonical_editor_pending_required",
                "Stage one Timeline edit, structural edit, or restore before Save.",
                status=409,
            )
        prior = session.snapshot
        edit = deepcopy(session.pending_edit)
        structural_edit = deepcopy(session.pending_structural_edit)
        restore_from = deepcopy(session.pending_restore)
        historical = session.historical_snapshot
        if restore_from is not None and (
            historical is None or historical.restore_from != restore_from
        ):
            raise EditorServerError(
                "canonical_editor_history_mismatch",
                "The server-held historical selection no longer matches the pending restore.",
                status=409,
            )
        # Save begins an authority transition. Stop every old-head stream
        # before issuing the write, including a trim that retained its lease
        # while it was only a noncanonical local preview.
        EditorServerManager._drop_preview(
            session,
            "agent_preview_head_changed",
        )
        try:
            if edit is not None:
                result = session.backend.save(
                    snapshot=prior,
                    edit=edit,
                )
            elif structural_edit is not None:
                result = session.backend.save_structural_edit(
                    snapshot=prior,
                    structural_edit=structural_edit,
                )
            else:
                assert historical is not None
                result = session.backend.save_restore(
                    snapshot=prior,
                    historical=historical,
                )
        except AgentApiError as exc:
            if (
                exc.status == 409
                and exc.code in _CANONICAL_REFRESHABLE_CONFLICT_CODES
            ):
                try:
                    current = session.backend.read()
                except (CanonicalEditorError, MemoLensError) as reread_exc:
                    EditorServerManager._drop_preview(
                        session,
                        "agent_preview_head_changed",
                    )
                    raise EditorServerError(
                        "canonical_editor_conflict_refresh_required",
                        (
                            "Save was rejected as a conflict, but the current "
                            "canonical head could not be reread. The pending Timeline change "
                            "and its write identity are retained; retry Save in this "
                            "session to reconcile, or Discard before Refresh."
                        ),
                        status=503,
                    ) from reread_exc
                session.snapshot = current
                session.pending_edit = None
                session.pending_structural_edit = None
                session.pending_preview = None
                session.pending_restore = None
                session.historical_snapshot = None
                session.last_save = None
                EditorServerManager._mint_preview(session)
                raise EditorServerError(
                    exc.code,
                    "Canonical state changed. The pending Timeline change was discarded; review the current head before staging again.",
                    status=409,
                    details={"current": session.public()},
                ) from exc
            if exc.status == 409:
                EditorServerManager._drop_preview(
                    session,
                    "agent_preview_head_changed",
                )
                raise EditorServerError(
                    exc.code,
                    "Canonical save returned a permanent or unrecognized conflict. The pending Timeline change and exact write identity are retained.",
                    status=409,
                ) from exc
            EditorServerManager._drop_preview(
                session,
                "agent_preview_lease_expired",
            )
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except CanonicalEditorError as exc:
            EditorServerManager._drop_preview(
                session,
                "agent_preview_lease_expired",
            )
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except MemoLensError as exc:
            EditorServerManager._drop_preview(
                session,
                "agent_preview_lease_expired",
            )
            raise EditorServerError(
                exc.code,
                (
                    "Save outcome is not confirmed. Keep this pending Timeline change and retry "
                    "Save with the same session so the permanent idempotency receipt "
                    "can reconcile the result."
                ),
                status=503,
            ) from exc
        EditorServerManager._drop_preview(
            session,
            "agent_preview_head_changed",
        )
        try:
            reread = session.backend.read()
        except AgentApiError as exc:
            raise EditorServerError(
                "canonical_editor_post_commit_reconciliation_required",
                (
                    "Save returned success, but its canonical reread failed. "
                    "The pending Timeline change and its write identity are retained; retry "
                    "Save in this session so the idempotency receipt can reconcile "
                    "the committed result."
                ),
                status=503,
            ) from exc
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        except MemoLensError as exc:
            raise EditorServerError(
                "canonical_editor_post_commit_reconciliation_required",
                (
                    "Save returned success, but its canonical reread failed. "
                    "The pending Timeline change and its write identity are retained; retry "
                    "Save in this session so the idempotency receipt can reconcile "
                    "the committed result."
                ),
                status=503,
            ) from exc
        try:
            if edit is not None:
                saved = validate_saved_result(
                    project_id=str(prior.public["project_id"]),
                    prior=prior,
                    edit=edit,
                    result=result,
                    reread=reread,
                )
            elif structural_edit is not None:
                saved = validate_saved_structural_result(
                    project_id=str(prior.public["project_id"]),
                    prior=prior,
                    structural_edit=structural_edit,
                    result=result,
                    reread=reread,
                )
            else:
                assert historical is not None
                saved = validate_saved_restore_result(
                    project_id=str(prior.public["project_id"]),
                    prior=prior,
                    historical=historical,
                    result=result,
                    reread=reread,
                )
        except CanonicalEditorError as exc:
            raise EditorServerError(exc.code, str(exc), status=exc.status) from exc
        session.snapshot = reread
        session.pending_edit = None
        session.pending_structural_edit = None
        session.pending_preview = None
        session.pending_restore = None
        session.historical_snapshot = None
        session.last_save = saved
        EditorServerManager._mint_preview(session)
        return session.public()

    def _apply_canonical_action(
        self,
        session: _CanonicalEditorSession,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        action = payload["type"]
        if action not in CANONICAL_EDITOR_ACTIONS:
            raise EditorServerError(
                "invalid_canonical_editor_action",
                "Canonical action must be stage, stage_structural, select_revision, stage_restore, discard, refresh, or save.",
            )
        if action == "stage":
            return self._stage_canonical_edit(session, payload)
        if action == "stage_structural":
            return self._stage_canonical_structural_edit(session, payload)
        if action == "select_revision":
            return self._select_canonical_revision(session, payload)
        if action == "stage_restore":
            return self._stage_canonical_restore(session, payload)
        if action == "discard":
            return self._discard_canonical_pending(session, payload)
        if action == "refresh":
            return self._refresh_canonical_session(session, payload)
        if action == "save":
            return self._save_canonical_change(session, payload)
        raise EditorServerError(
            "invalid_canonical_editor_action",
            "Canonical action must be stage, stage_structural, select_revision, stage_restore, discard, refresh, or save.",
        )

    def close(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._origin = None
            sessions = list(self._sessions.values())
            self._sessions.clear()
            for session in sessions:
                if isinstance(session, _CanonicalEditorSession):
                    self._drop_preview(session, "agent_preview_lease_expired")
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)


class _EditorRequestHandler(BaseHTTPRequestHandler):
    server_version = "MemoLensEditor/1"
    sys_version = ""

    @property
    def manager(self) -> EditorServerManager:
        return self.server.editor_manager  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _origin(self) -> str:
        origin = self.manager.origin
        if origin is None:
            raise EditorServerError(
                "editor_server_unavailable",
                "The editor server is unavailable.",
                status=503,
            )
        return origin

    def _validate_request_origin(self) -> None:
        expected = urlsplit(self._origin())
        expected_host = expected.netloc
        if self.headers.get("Host", "") != expected_host:
            raise EditorServerError(
                "editor_host_invalid",
                "The editor Host header is invalid.",
                status=403,
            )
        supplied_origin = self.headers.get("Origin")
        if supplied_origin not in {None, self._origin()}:
            raise EditorServerError(
                "editor_origin_invalid",
                "The editor Origin header is invalid.",
                status=403,
            )

    def _session_id(self, *, api: bool) -> str:
        parsed = urlsplit(self.path)
        if api:
            prefix = "/api/sessions/"
        elif parsed.path.startswith("/canonical-editor/"):
            prefix = "/canonical-editor/"
        else:
            prefix = "/editor/"
        if not parsed.path.startswith(prefix):
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        remainder = parsed.path[len(prefix):]
        session_id = remainder.split("/", 1)[0]
        if not session_id.startswith(("edit_", "canonical_")) or len(session_id) > 80:
            raise EditorServerError(
                "editor_session_not_found",
                "Editor session not found.",
                status=404,
            )
        if (
            not api
            and (
                (prefix == "/editor/" and not session_id.startswith("edit_"))
                or (
                    prefix == "/canonical-editor/"
                    and not session_id.startswith("canonical_")
                )
            )
        ):
            raise EditorServerError(
                "editor_session_not_found",
                "Editor session not found.",
                status=404,
            )
        return session_id

    def _cookie_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(EDITOR_COOKIE)
        return None if morsel is None else morsel.value

    def _body(self) -> Any:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise EditorServerError(
                "editor_content_type_invalid",
                "Editor requests require application/json.",
                status=415,
            )
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise EditorServerError(
                "invalid_content_length",
                "Content-Length is invalid.",
            ) from exc
        if not 1 <= length <= EDITOR_MAX_REQUEST_BYTES:
            raise EditorServerError(
                "editor_request_too_large",
                "Editor request body is empty or too large.",
                status=413,
            )
        try:
            return decode_strict_json(
                self.rfile.read(length),
                limits=EDITOR_JSON_LIMITS,
            )
        except StrictJsonError as exc:
            raise EditorServerError(
                "invalid_editor_json",
                "Editor request JSON is invalid.",
            ) from exc

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")

    def _json(
        self,
        payload: dict[str, Any],
        *,
        status: int = 200,
        cookie: tuple[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        if cookie is not None:
            session_id, token = cookie
            self.send_header(
                "Set-Cookie",
                (
                    f"{EDITOR_COOKIE}={token}; HttpOnly; SameSite=Strict; "
                    f"Path=/api/sessions/{session_id}; "
                    f"Max-Age={self.manager._session_ttl_seconds}"
                ),
            )
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: EditorServerError) -> None:
        error: dict[str, Any] = {"code": exc.code, "message": str(exc)}
        if exc.details is not None:
            error["details"] = deepcopy(exc.details)
        self._json(
            {
                "object": "memolens.editor_error",
                "status": "error",
                "error": error,
            },
            status=exc.status,
        )

    def _local_range_error(self) -> None:
        """Emit a closed media 416 when an unsafe header cannot be forwarded."""

        self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", "0")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()

    @staticmethod
    def _thumbnail_revision(value: str) -> int:
        if re.fullmatch(r"[1-9][0-9]{0,15}", value) is None:
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        return int(value)

    def _clip_media_id(self, path: str, session_id: str) -> str | None:
        prefix = f"/api/sessions/{session_id}/clip-media/"
        if not path.startswith(prefix):
            return None
        encoded = path[len(prefix):]
        if not encoded or "/" in encoded:
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        return unquote(encoded)

    def _request_range(self) -> str | None:
        values = self.headers.get_all("Range") or []
        if not values:
            return None
        if len(values) != 1 or type(values[0]) is not str:
            raise EditorServerError(
                "agent_preview_range_invalid",
                "Only one valid Timeline preview byte range is supported.",
                status=416,
            )
        return values[0].strip()

    def _serve_clip_media(
        self,
        *,
        session_id: str,
        clip_id: str,
        method: str,
    ) -> None:
        token = self._cookie_token()
        # Authenticate and bind the opaque clip before parsing a Range that
        # may need a process-local closed 416 response.
        self.manager.authorize_clip_media_request(session_id, token, clip_id)
        upstream = self.manager.clip_media(
            session_id,
            token,
            clip_id,
            method=method,
            range_header=self._request_range(),
        )
        first = b""
        try:
            if method == "GET" and upstream.content_length > 0:
                try:
                    first = upstream.read(EDITOR_MEDIA_STREAM_CHUNK_BYTES)
                except MemoLensError as exc:
                    raise EditorServerError(
                        exc.code,
                        "The canonical source stream ended before response admission.",
                        status=503,
                    ) from exc
            if upstream.closed:
                raise EditorServerError(
                    "agent_preview_head_changed",
                    "Canonical editor state changed before response admission.",
                    status=409,
                )
            self.send_response(upstream.status)
            for name in (
                "Content-Type",
                "Content-Length",
                "Accept-Ranges",
                "Content-Range",
                "ETag",
                "Cache-Control",
                "Pragma",
            ):
                value = upstream.headers.get(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()
            if method == "HEAD" or upstream.content_length == 0:
                return
            try:
                if upstream.closed:
                    self.close_connection = True
                    return
                self.wfile.write(first)
                while not upstream.complete:
                    chunk = upstream.read(EDITOR_MEDIA_STREAM_CHUNK_BYTES)
                    if upstream.closed:
                        self.close_connection = True
                        return
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, MemoLensError, OSError):
                # Headers already left this process.  Closing both sockets is
                # the only honest response to downstream disconnect or a late
                # truncated upstream; never synthesize success bytes.
                self.close_connection = True
        finally:
            upstream.close()
            self.manager.release_preview_stream(session_id, upstream)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._validate_request_origin()
            path = urlsplit(self.path).path
            if path.startswith(("/editor/", "/canonical-editor/")):
                self._session_id(api=False)
                canonical = path.startswith("/canonical-editor/")
                body = (
                    CANONICAL_EDITOR_HTML_PATH if canonical else EDITOR_HTML_PATH
                ).read_bytes()
                self.send_response(HTTPStatus.OK)
                self._headers("text/html; charset=utf-8", len(body))
                default_source = "'self'" if canonical else "'none'"
                self.send_header(
                    "Content-Security-Policy",
                    (
                        f"default-src {default_source}; "
                        "base-uri 'none'; form-action 'none'; "
                        "frame-ancestors 'none'; "
                        "img-src 'self' data:; "
                        "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                        "connect-src 'self'; media-src 'self'"
                    ),
                )
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/api/sessions/"):
                session_id = self._session_id(api=True)
                clip_id = self._clip_media_id(path, session_id)
                if clip_id is not None:
                    self._serve_clip_media(
                        session_id=session_id,
                        clip_id=clip_id,
                        method="GET",
                    )
                    return
                replacement_preview_prefix = (
                    f"/api/sessions/{session_id}/replacement-previews/"
                )
                if path.startswith(replacement_preview_prefix):
                    if "?" in self.path or "#" in self.path:
                        raise EditorServerError(
                            "editor_route_not_found",
                            "Editor route not found.",
                            status=404,
                        )
                    identities = path[len(replacement_preview_prefix):].split("/")
                    if len(identities) not in {2, 3} or not all(identities):
                        raise EditorServerError(
                            "editor_route_not_found",
                            "Editor route not found.",
                            status=404,
                        )
                    body = self.manager.replacement_thumbnail(
                        session_id,
                        self._cookie_token(),
                        unquote(identities[0]),
                        unquote(identities[1]),
                        expected_revision=(
                            self._thumbnail_revision(identities[2])
                            if len(identities) == 3 else None
                        ),
                    )
                    self.send_response(HTTPStatus.OK)
                    self._headers("image/jpeg", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                preview_prefix = f"/api/sessions/{session_id}/previews/"
                if path.startswith(preview_prefix):
                    if "?" in self.path or "#" in self.path:
                        raise EditorServerError(
                            "editor_route_not_found",
                            "Editor route not found.",
                            status=404,
                        )
                    identities = path[len(preview_prefix):].split("/")
                    if len(identities) not in {1, 2} or not all(identities):
                        raise EditorServerError(
                            "editor_route_not_found",
                            "Editor route not found.",
                            status=404,
                        )
                    body = self.manager.thumbnail(
                        session_id,
                        self._cookie_token(),
                        unquote(identities[0]),
                        expected_revision=(
                            self._thumbnail_revision(identities[1])
                            if len(identities) == 2 else None
                        ),
                    )
                    self.send_response(HTTPStatus.OK)
                    self._headers("image/jpeg", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path != f"/api/sessions/{session_id}":
                    raise EditorServerError(
                        "editor_route_not_found",
                        "Editor route not found.",
                        status=404,
                    )
                session = self.manager.authenticate(session_id, self._cookie_token())
                self._json(session.public())
                return
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        except EditorServerError as exc:
            if exc.code == "agent_preview_range_invalid" and exc.status == 416:
                self._local_range_error()
            else:
                self._error(exc)
        except OSError:
            self._error(
                EditorServerError(
                    "editor_ui_unavailable",
                    "The packaged editor UI is unavailable.",
                    status=503,
                )
            )

    def do_HEAD(self) -> None:  # noqa: N802
        try:
            self._validate_request_origin()
            path = urlsplit(self.path).path
            if not path.startswith("/api/sessions/"):
                raise EditorServerError(
                    "editor_method_not_allowed",
                    "Editor HEAD is allowed only for canonical clip media.",
                    status=405,
                )
            session_id = self._session_id(api=True)
            clip_id = self._clip_media_id(path, session_id)
            if clip_id is None:
                raise EditorServerError(
                    "editor_method_not_allowed",
                    "Editor HEAD is allowed only for canonical clip media.",
                    status=405,
                )
            self._serve_clip_media(
                session_id=session_id,
                clip_id=clip_id,
                method="HEAD",
            )
        except EditorServerError as exc:
            if exc.code == "agent_preview_range_invalid" and exc.status == 416:
                self._local_range_error()
                return
            error = json.dumps(
                {
                    "object": "memolens.editor_error",
                    "status": "error",
                    "error": {"code": exc.code, "message": str(exc)},
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(exc.status)
            self._headers("application/json; charset=utf-8", len(error))
            self.end_headers()

    def _method_not_allowed(self) -> None:
        try:
            self._validate_request_origin()
            raise EditorServerError(
                "editor_method_not_allowed",
                "Editor method is not allowed.",
                status=405,
            )
        except EditorServerError as exc:
            self._error(exc)

    do_DELETE = _method_not_allowed  # type: ignore[assignment]  # noqa: N815
    do_PATCH = _method_not_allowed  # type: ignore[assignment]  # noqa: N815
    do_PUT = _method_not_allowed  # type: ignore[assignment]  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._validate_request_origin()
            session_id = self._session_id(api=True)
            path = urlsplit(self.path).path
            payload = self._body()
            if path == f"/api/sessions/{session_id}/bootstrap":
                if not isinstance(payload, dict) or set(payload) != {"token"}:
                    raise EditorServerError(
                        "editor_boot_token_invalid",
                        "Bootstrap accepts only a token.",
                        status=401,
                    )
                cookie, state = self.manager.bootstrap(session_id, payload["token"])
                self._json(state, cookie=(session_id, cookie))
                return
            if path == f"/api/sessions/{session_id}/actions":
                result = self.manager.apply_action(
                    session_id,
                    self._cookie_token(),
                    payload,
                )
                self._json(result)
                return
            raise EditorServerError(
                "editor_route_not_found",
                "Editor route not found.",
                status=404,
            )
        except EditorServerError as exc:
            self._error(exc)


_MANAGER_LOCK = threading.Lock()
_MANAGER: EditorServerManager | None = None


def get_editor_server_manager() -> EditorServerManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = EditorServerManager()
        return _MANAGER


def create_editor_handoff(
    *,
    timeline: Any,
    replacement_candidates: Any = None,
) -> dict[str, Any]:
    return get_editor_server_manager().create_handoff(
        timeline=timeline,
        replacement_candidates=replacement_candidates,
    )


def create_canonical_editor_handoff(*, project_id: str) -> dict[str, Any]:
    try:
        backend = paired_canonical_editor_backend(project_id)
        return get_editor_server_manager().create_canonical_handoff(
            backend=backend,
        )
    except CanonicalEditorError as exc:
        raise EditorServerError(exc.code, str(exc), status=exc.status) from exc


__all__ = [
    "CANONICAL_EDITOR_ACTIONS",
    "CANONICAL_EDITOR_EDIT_ACTIONS",
    "EDITOR_OBJECT",
    "EDITOR_SCHEMA_VERSION",
    "LEGACY_EDITOR_ACTIONS",
    "EditorServerError",
    "EditorServerManager",
    "create_canonical_editor_handoff",
    "create_editor_handoff",
    "get_editor_server_manager",
]
