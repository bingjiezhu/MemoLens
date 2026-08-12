#!/usr/bin/env python3
"""Deterministic, in-memory helpers for MemoLens Timeline Schema 1.0."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
from typing import Any


TIMELINE_SCHEMA_VERSION = "1.0"
TIMELINE_RESPONSE_SCHEMA_VERSION = "1"
MAX_TIMELINE_DURATION_MS = 30 * 60 * 1000
MAX_TRACKS = 16
MAX_CLIPS = 500
MAX_TRANSITIONS = 500
ALLOWED_TRACK_TYPES = {"video", "image", "text", "audio"}
ALLOWED_TRACK_ROLES = {"primary", "overlay", "subtitle", "music", "voiceover"}
ALLOWED_FITS = {"contain", "cover", "stretch"}
ALLOWED_TRANSITIONS = {"crossfade", "fade_to_black"}
ALLOWED_FPS = {24, 25, 30, 50, 60}
ALLOWED_FONTS = {"Arial", "Helvetica", "Inter", "SF Pro", "system-ui"}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class TimelineInputError(ValueError):
    """Raised for malformed tool input before a timeline can be produced."""

    def __init__(self, message: str, *, field: str = "timeline") -> None:
        super().__init__(message)
        self.field = field


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TimelineInputError("Timeline input must be finite JSON data.") from exc


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value.strip()):
        raise TimelineInputError(
            f"{field} must be a non-empty stable identifier.", field=field
        )
    return value.strip()


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value.strip()):
        raise TimelineInputError(f"{field} must be a SHA-256 hex digest.", field=field)
    return value.strip().lower()


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimelineInputError(f"{field} must be an integer.", field=field)
    if minimum is not None and value < minimum:
        raise TimelineInputError(f"{field} must be at least {minimum}.", field=field)
    if maximum is not None and value > maximum:
        raise TimelineInputError(f"{field} must be at most {maximum}.", field=field)
    return value


def _require_number(
    value: Any, field: str, *, minimum: float, maximum: float
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimelineInputError(f"{field} must be numeric.", field=field)
    if not minimum <= value <= maximum:
        raise TimelineInputError(
            f"{field} must be between {minimum} and {maximum}.", field=field
        )
    return value


def _require_timestamp(value: Any, field: str = "created_at") -> str:
    if not isinstance(value, str) or not value.strip():
        raise TimelineInputError(
            f"{field} must be an ISO 8601 timestamp with a timezone.", field=field
        )
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimelineInputError(
            f"{field} must be an ISO 8601 timestamp with a timezone.", field=field
        ) from exc
    if parsed.tzinfo is None:
        raise TimelineInputError(f"{field} must include a timezone.", field=field)
    return normalized


def _format(raw: Any, *, derived_duration_ms: int | None = None) -> dict[str, Any]:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise TimelineInputError("format must be an object.", field="format")
    allowed = {
        "width",
        "height",
        "fps",
        "sample_rate",
        "duration_ms",
        "background_color",
    }
    if set(raw) - allowed:
        raise TimelineInputError("format contains unknown fields.", field="format")
    result = {
        "width": raw.get("width", 1080),
        "height": raw.get("height", 1920),
        "fps": raw.get("fps", 30),
        "sample_rate": raw.get("sample_rate", 48000),
        "duration_ms": raw.get("duration_ms", derived_duration_ms),
        "background_color": raw.get("background_color", "#000000"),
    }
    result["width"] = _require_int(
        result["width"], "format.width", minimum=240, maximum=3840
    )
    result["height"] = _require_int(
        result["height"], "format.height", minimum=240, maximum=3840
    )
    result["fps"] = _require_int(result["fps"], "format.fps")
    if result["fps"] not in ALLOWED_FPS:
        raise TimelineInputError("format.fps is not supported.", field="format.fps")
    result["sample_rate"] = _require_int(
        result["sample_rate"],
        "format.sample_rate",
        minimum=8000,
        maximum=192000,
    )
    result["duration_ms"] = _require_int(
        result["duration_ms"],
        "format.duration_ms",
        minimum=1,
        maximum=MAX_TIMELINE_DURATION_MS,
    )
    if not isinstance(result["background_color"], str) or not HEX_COLOR_RE.fullmatch(
        result["background_color"]
    ):
        raise TimelineInputError(
            "format.background_color must be a hex color.",
            field="format.background_color",
        )
    return result


def _normalize_draft_item(
    raw: Any, index: int, timeline_cursor: int
) -> tuple[dict[str, Any], int]:
    field = f"items[{index}]"
    if not isinstance(raw, dict):
        raise TimelineInputError(f"{field} must be an object.", field=field)
    allowed = {
        "kind",
        "asset_id",
        "asset_source_id",
        "asset_sha256",
        "segment_id",
        "analysis_run_id",
        "analysis_revision",
        "source_in_ms",
        "source_out_ms",
        "timeline_start_ms",
        "timeline_duration_ms",
        "fit",
        "crop",
        "volume_db",
        "audio_enabled",
        "fade_in_ms",
        "fade_out_ms",
        "reason",
        "match_id",
    }
    if set(raw) - allowed:
        raise TimelineInputError(f"{field} contains unknown fields.", field=field)
    kind = raw.get("kind")
    if kind not in {"video", "image", "audio"}:
        raise TimelineInputError(
            f"{field}.kind must be video, image, or audio.", field=f"{field}.kind"
        )
    asset_id = _require_id(raw.get("asset_id"), f"{field}.asset_id")
    asset_source_id = _require_id(
        raw.get("asset_source_id"), f"{field}.asset_source_id"
    )
    asset_sha256 = _require_sha256(
        raw.get("asset_sha256"), f"{field}.asset_sha256"
    )
    timeline_start_ms = _require_int(
        raw.get("timeline_start_ms", timeline_cursor),
        f"{field}.timeline_start_ms",
        minimum=0,
        maximum=MAX_TIMELINE_DURATION_MS - 1,
    )
    item: dict[str, Any] = {
        "kind": kind,
        "asset_id": asset_id,
        "asset_source_id": asset_source_id,
        "asset_sha256": asset_sha256,
        "timeline_start_ms": timeline_start_ms,
        "fit": raw.get("fit", "cover" if kind in {"video", "image"} else None),
        "reason": str(raw.get("reason") or "Selected for the draft.").strip()[:500],
        "match_id": str(
            raw.get("match_id") or raw.get("segment_id") or asset_id
        ).strip()[:200],
    }
    if not item["reason"] or not item["match_id"]:
        raise TimelineInputError(
            f"{field} reason and match_id must be non-empty.", field=field
        )
    if kind in {"video", "image"} and item["fit"] not in ALLOWED_FITS:
        raise TimelineInputError(
            f"{field}.fit is not supported.", field=f"{field}.fit"
        )
    if kind in {"video", "audio"}:
        source_in = _require_int(
            raw.get("source_in_ms"), f"{field}.source_in_ms", minimum=0
        )
        source_out = _require_int(
            raw.get("source_out_ms"), f"{field}.source_out_ms", minimum=1
        )
        if source_out <= source_in:
            raise TimelineInputError(
                f"{field}.source_out_ms must exceed source_in_ms.",
                field=f"{field}.source_out_ms",
            )
        source_duration = source_out - source_in
        duration_ms = _require_int(
            raw.get("timeline_duration_ms", source_duration),
            f"{field}.timeline_duration_ms",
            minimum=1,
            maximum=MAX_TIMELINE_DURATION_MS,
        )
        if duration_ms != source_duration:
            raise TimelineInputError(
                f"{field}.timeline_duration_ms must equal the source range.",
                field=f"{field}.timeline_duration_ms",
            )
        item.update(
            {
                "source_in_ms": source_in,
                "source_out_ms": source_out,
                "timeline_duration_ms": duration_ms,
            }
        )
    else:
        item["timeline_duration_ms"] = _require_int(
            raw.get("timeline_duration_ms"),
            f"{field}.timeline_duration_ms",
            minimum=1,
            maximum=MAX_TIMELINE_DURATION_MS,
        )
    if kind == "video":
        item["segment_id"] = _require_id(
            raw.get("segment_id"), f"{field}.segment_id"
        )
        item["analysis_run_id"] = _require_id(
            raw.get("analysis_run_id"), f"{field}.analysis_run_id"
        )
        item["analysis_revision"] = _require_int(
            raw.get("analysis_revision"),
            f"{field}.analysis_revision",
            minimum=1,
            maximum=1_000_000,
        )
        audio_enabled = raw.get("audio_enabled", True)
        if not isinstance(audio_enabled, bool):
            raise TimelineInputError(
                f"{field}.audio_enabled must be boolean.",
                field=f"{field}.audio_enabled",
            )
        item["audio_enabled"] = audio_enabled
        item["volume_db"] = _require_number(
            raw.get("volume_db", 0), f"{field}.volume_db", minimum=-60, maximum=12
        )
    elif kind == "audio":
        item["volume_db"] = _require_number(
            raw.get("volume_db", 0), f"{field}.volume_db", minimum=-60, maximum=12
        )
        for name in ("fade_in_ms", "fade_out_ms"):
            item[name] = _require_int(
                raw.get(name, 0),
                f"{field}.{name}",
                minimum=0,
                maximum=item["timeline_duration_ms"] // 2,
            )
    if raw.get("crop") is not None:
        item["crop"] = deepcopy(raw["crop"])
    end = timeline_start_ms + item["timeline_duration_ms"]
    if end > MAX_TIMELINE_DURATION_MS:
        raise TimelineInputError(
            f"{field} exceeds the 30-minute limit.",
            field=f"{field}.timeline_duration_ms",
        )
    return item, max(timeline_cursor, end)


def draft_timeline(
    *,
    project_id: str,
    items: list[dict[str, Any]],
    created_at: str,
    format_options: dict[str, Any] | None = None,
    brief_revision: int = 1,
) -> dict[str, Any]:
    """Create a deterministic, unsaved Timeline 1.0 draft."""

    project_id = _require_id(project_id, "project_id")
    created_at = _require_timestamp(created_at)
    brief_revision = _require_int(
        brief_revision, "brief_revision", minimum=1, maximum=1_000_000
    )
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CLIPS:
        raise TimelineInputError(
            f"items must contain between 1 and {MAX_CLIPS} clips.", field="items"
        )

    normalized_items: list[dict[str, Any]] = []
    timeline_cursor = 0
    for index, raw in enumerate(items):
        item, timeline_cursor = _normalize_draft_item(raw, index, timeline_cursor)
        normalized_items.append(item)
    if not any(item["kind"] in {"video", "image"} for item in normalized_items):
        raise TimelineInputError(
            "items must include at least one visual clip.", field="items"
        )

    timeline_format = _format(format_options, derived_duration_ms=timeline_cursor)
    if timeline_format["duration_ms"] != timeline_cursor:
        raise TimelineInputError(
            "format.duration_ms must equal the maximum clip end.",
            field="format.duration_ms",
        )

    first_visual_kind = next(
        item["kind"] for item in normalized_items if item["kind"] in {"video", "image"}
    )
    tracks_by_kind: dict[str, dict[str, Any]] = {}
    source_runs: dict[str, str] = {}
    source_assets: dict[str, dict[str, str]] = {}
    source_revisions: dict[str, int] = {}
    for index, source_item in enumerate(normalized_items):
        item = deepcopy(source_item)
        kind = item.pop("kind")
        asset_sha256 = item.pop("asset_sha256")
        analysis_run_id = item.pop("analysis_run_id", None)
        analysis_revision = item.pop("analysis_revision", None)
        reason = item.pop("reason")
        match_id = item.pop("match_id")
        asset_id = item["asset_id"]
        source_record = {
            "sha256": asset_sha256,
            "asset_source_id": item["asset_source_id"],
        }
        previous_source = source_assets.setdefault(asset_id, source_record)
        if previous_source != source_record:
            raise TimelineInputError(
                "A draft cannot bind one asset to multiple source IDs or hashes.",
                field=f"items[{index}].asset_source_id",
            )
        if kind == "video":
            source_runs[item["segment_id"]] = analysis_run_id
            source_revisions[item["segment_id"]] = analysis_revision
        if kind == first_visual_kind:
            role = "primary"
        elif kind in {"video", "image"}:
            role = "overlay"
        else:
            role = "music"
        track = tracks_by_kind.setdefault(
            kind,
            {
                "id": f"track_{kind}_01",
                "type": kind,
                "role": role,
                "z_index": 0 if role in {"primary", "music"} else 1,
                "muted": False,
                "clips": [],
            },
        )
        clip_seed = {
            "project_id": project_id,
            "index": index,
            "item": source_item,
        }
        clip = {
            "id": _stable_id("clip", clip_seed),
            **item,
            "provenance": {"reason": reason, "match_id": match_id},
        }
        track["clips"].append(clip)

    tracks = list(tracks_by_kind.values())
    identity_seed = {
        "project_id": project_id,
        "format": timeline_format,
        "tracks": tracks,
        "source_analysis_runs": source_runs,
        "source_assets": source_assets,
        "brief_revision": brief_revision,
    }
    timeline = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "id": _stable_id("tl", identity_seed),
        "project_id": project_id,
        "revision": 1,
        "format": timeline_format,
        "tracks": tracks,
        "transitions": [],
        "provenance": {
            "created_by": "codex",
            "parent_revision": None,
            "brief_revision": brief_revision,
            "operations": [
                {
                    "op": "draft",
                    "clip_count": len(normalized_items),
                    "analysis_revisions": source_revisions,
                }
            ],
            "source_analysis_runs": source_runs,
            "source_assets": source_assets,
            "created_at": created_at,
        },
    }
    validation = validate_timeline(timeline)
    if not validation["valid"]:
        first = validation["errors"][0]
        raise TimelineInputError(first["message"], field=first["field"])
    return timeline


def _validation_error(
    errors: list[dict[str, str]], code: str, field: str, message: str
) -> None:
    errors.append({"code": code, "field": field, "message": message})


def validate_timeline(timeline: Any) -> dict[str, Any]:
    """Validate Timeline 1.0 structure without SQLite, files, or network access."""

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(timeline, dict):
        _validation_error(errors, "invalid_type", "timeline", "timeline must be an object.")
        return _validation_result(None, None, errors, warnings)
    allowed_top = {
        "schema_version",
        "id",
        "project_id",
        "revision",
        "format",
        "tracks",
        "transitions",
        "provenance",
    }
    unknown_top = sorted(set(timeline) - allowed_top)
    if unknown_top:
        _validation_error(
            errors,
            "unknown_field",
            "timeline",
            f"Unknown timeline fields: {', '.join(unknown_top)}.",
        )
    if timeline.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        _validation_error(
            errors,
            "unsupported_schema",
            "schema_version",
            "schema_version must be 1.0.",
        )
    for field in ("id", "project_id"):
        try:
            _require_id(timeline.get(field), field)
        except TimelineInputError as exc:
            _validation_error(errors, "invalid_id", exc.field, str(exc))
    try:
        _require_int(
            timeline.get("revision"), "revision", minimum=1, maximum=1_000_000
        )
    except TimelineInputError as exc:
        _validation_error(errors, "invalid_integer", exc.field, str(exc))

    timeline_format: dict[str, Any] | None = None
    try:
        timeline_format = _format(timeline.get("format"))
    except TimelineInputError as exc:
        _validation_error(errors, "invalid_format", exc.field, str(exc))

    tracks = timeline.get("tracks")
    if not isinstance(tracks, list) or not 1 <= len(tracks) <= MAX_TRACKS:
        _validation_error(
            errors,
            "invalid_tracks",
            "tracks",
            f"tracks must contain 1-{MAX_TRACKS} entries.",
        )
        tracks = []
    track_ids: set[str] = set()
    clip_ids: set[str] = set()
    clip_locations: dict[str, tuple[int, int, dict[str, Any], dict[str, Any]]] = {}
    primary_visual_tracks = 0
    total_clips = 0
    timeline_end = 0
    for track_index, track in enumerate(tracks):
        prefix = f"tracks[{track_index}]"
        if not isinstance(track, dict):
            _validation_error(errors, "invalid_type", prefix, "track must be an object.")
            continue
        unknown = sorted(set(track) - {"id", "type", "role", "z_index", "muted", "clips"})
        if unknown:
            _validation_error(
                errors,
                "unknown_field",
                prefix,
                f"Unknown track fields: {', '.join(unknown)}.",
            )
        try:
            track_id = _require_id(track.get("id"), f"{prefix}.id")
            if track_id in track_ids:
                _validation_error(
                    errors, "duplicate_id", f"{prefix}.id", "Track IDs must be unique."
                )
            track_ids.add(track_id)
        except TimelineInputError as exc:
            _validation_error(errors, "invalid_id", exc.field, str(exc))
        track_type = track.get("type")
        role = track.get("role")
        if track_type not in ALLOWED_TRACK_TYPES:
            _validation_error(errors, "invalid_enum", f"{prefix}.type", "Unsupported track type.")
        if role not in ALLOWED_TRACK_ROLES or not _valid_track_role(track_type, role):
            _validation_error(
                errors,
                "invalid_enum",
                f"{prefix}.role",
                "Track role is incompatible with its type.",
            )
        if role == "primary" and track_type in {"video", "image"}:
            primary_visual_tracks += 1
        try:
            z_index = _require_int(
                track.get("z_index"), f"{prefix}.z_index", minimum=0, maximum=100
            )
            if track_type == "audio" and z_index != 0:
                _validation_error(
                    errors,
                    "invalid_z_index",
                    f"{prefix}.z_index",
                    "Audio track z_index must be 0.",
                )
        except TimelineInputError as exc:
            _validation_error(errors, "invalid_integer", exc.field, str(exc))
        if not isinstance(track.get("muted"), bool):
            _validation_error(
                errors, "invalid_type", f"{prefix}.muted", "muted must be boolean."
            )
        clips = track.get("clips")
        if not isinstance(clips, list):
            _validation_error(
                errors, "invalid_type", f"{prefix}.clips", "clips must be an array."
            )
            continue
        total_clips += len(clips)
        for clip_index, clip in enumerate(clips):
            clip_prefix = f"{prefix}.clips[{clip_index}]"
            clip_end = _validate_clip(clip, track_type, clip_prefix, clip_ids, errors)
            if isinstance(clip, dict) and isinstance(clip.get("id"), str):
                clip_locations[clip["id"]] = (track_index, clip_index, track, clip)
            if clip_end is not None:
                timeline_end = max(timeline_end, clip_end)
    if primary_visual_tracks != 1:
        _validation_error(
            errors,
            "invalid_primary_track",
            "tracks",
            "Timeline must contain exactly one primary visual track.",
        )
    if total_clips > MAX_CLIPS:
        _validation_error(
            errors, "too_many_clips", "tracks", f"Timeline exceeds {MAX_CLIPS} clips."
        )
    if timeline_format and timeline_end != timeline_format["duration_ms"]:
        _validation_error(
            errors,
            "duration_mismatch",
            "format.duration_ms",
            "format.duration_ms must equal the maximum clip end.",
        )

    allowed_crossfades = _validate_transitions(
        timeline.get("transitions"), clip_locations, errors
    )
    _validate_primary_overlaps(tracks, allowed_crossfades, errors)
    _validate_provenance(
        timeline.get("provenance"), timeline.get("revision"), tracks, errors
    )
    return _validation_result(
        timeline.get("id"), timeline.get("revision"), errors, warnings
    )


def _valid_track_role(track_type: Any, role: Any) -> bool:
    if track_type in {"video", "image"}:
        return role in {"primary", "overlay"}
    if track_type == "text":
        return role in {"subtitle", "overlay"}
    if track_type == "audio":
        return role in {"music", "voiceover"}
    return False


def _clip_allowed_fields(track_type: Any) -> set[str]:
    base = {"id", "timeline_start_ms", "timeline_duration_ms", "provenance"}
    if track_type == "video":
        return base | {
            "asset_id",
            "asset_source_id",
            "segment_id",
            "source_in_ms",
            "source_out_ms",
            "fit",
            "crop",
            "volume_db",
            "audio_enabled",
        }
    if track_type == "image":
        return base | {"asset_id", "asset_source_id", "fit", "crop"}
    if track_type == "audio":
        return base | {
            "asset_id",
            "asset_source_id",
            "source_in_ms",
            "source_out_ms",
            "volume_db",
            "fade_in_ms",
            "fade_out_ms",
        }
    if track_type == "text":
        return base | {
            "text",
            "font_family",
            "font_size",
            "color",
            "background",
            "position",
            "alignment",
            "transcript_segment_ids",
        }
    return base


def _validate_clip(
    clip: Any,
    track_type: Any,
    prefix: str,
    clip_ids: set[str],
    errors: list[dict[str, str]],
) -> int | None:
    if not isinstance(clip, dict):
        _validation_error(errors, "invalid_type", prefix, "clip must be an object.")
        return None
    unknown = sorted(set(clip) - _clip_allowed_fields(track_type))
    if unknown:
        _validation_error(
            errors,
            "unknown_field",
            prefix,
            f"Unknown clip fields: {', '.join(unknown)}.",
        )
    try:
        clip_id = _require_id(clip.get("id"), f"{prefix}.id")
        if clip_id in clip_ids:
            _validation_error(
                errors, "duplicate_id", f"{prefix}.id", "Clip IDs must be unique."
            )
        clip_ids.add(clip_id)
    except TimelineInputError as exc:
        _validation_error(errors, "invalid_id", exc.field, str(exc))
    if track_type != "text":
        for field in ("asset_id", "asset_source_id"):
            try:
                _require_id(clip.get(field), f"{prefix}.{field}")
            except TimelineInputError as exc:
                _validation_error(errors, "invalid_id", exc.field, str(exc))
    if track_type == "video":
        try:
            _require_id(clip.get("segment_id"), f"{prefix}.segment_id")
        except TimelineInputError as exc:
            _validation_error(errors, "invalid_id", exc.field, str(exc))
    try:
        start = _require_int(
            clip.get("timeline_start_ms"), f"{prefix}.timeline_start_ms", minimum=0
        )
        duration = _require_int(
            clip.get("timeline_duration_ms"),
            f"{prefix}.timeline_duration_ms",
            minimum=1,
        )
    except TimelineInputError as exc:
        _validation_error(errors, "invalid_integer", exc.field, str(exc))
        return None
    if start + duration > MAX_TIMELINE_DURATION_MS:
        _validation_error(
            errors,
            "timeline_overflow",
            f"{prefix}.timeline_duration_ms",
            "Clip exceeds the 30-minute limit.",
        )
    if track_type in {"video", "audio"}:
        try:
            source_in = _require_int(
                clip.get("source_in_ms"), f"{prefix}.source_in_ms", minimum=0
            )
            source_out = _require_int(
                clip.get("source_out_ms"), f"{prefix}.source_out_ms", minimum=1
            )
            if source_out <= source_in:
                _validation_error(
                    errors,
                    "invalid_source_range",
                    f"{prefix}.source_out_ms",
                    "source_out_ms must exceed source_in_ms.",
                )
            elif duration != source_out - source_in:
                _validation_error(
                    errors,
                    "speed_not_supported",
                    f"{prefix}.timeline_duration_ms",
                    "timeline_duration_ms must equal the source range.",
                )
        except TimelineInputError as exc:
            _validation_error(errors, "invalid_integer", exc.field, str(exc))
    if track_type in {"video", "image"}:
        if clip.get("fit") not in ALLOWED_FITS:
            _validation_error(
                errors, "invalid_enum", f"{prefix}.fit", "Unsupported fit mode."
            )
        _validate_crop(clip.get("crop"), f"{prefix}.crop", errors)
    if track_type in {"video", "audio"}:
        _validate_volume(clip.get("volume_db"), f"{prefix}.volume_db", errors)
    if track_type == "video" and not isinstance(clip.get("audio_enabled"), bool):
        _validation_error(
            errors,
            "invalid_type",
            f"{prefix}.audio_enabled",
            "audio_enabled must be boolean.",
        )
    if track_type == "audio":
        for name in ("fade_in_ms", "fade_out_ms"):
            try:
                fade = _require_int(
                    clip.get(name), f"{prefix}.{name}", minimum=0, maximum=duration // 2
                )
                del fade
            except TimelineInputError as exc:
                _validation_error(errors, "invalid_fade", exc.field, str(exc))
    if track_type == "text":
        _validate_text_clip(clip, prefix, errors)
    provenance = clip.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"reason", "match_id"}:
        _validation_error(
            errors,
            "invalid_provenance",
            f"{prefix}.provenance",
            "Clip provenance requires only reason and match_id.",
        )
    elif not all(
        isinstance(provenance.get(key), str) and provenance[key].strip()
        for key in ("reason", "match_id")
    ):
        _validation_error(
            errors,
            "invalid_provenance",
            f"{prefix}.provenance",
            "Clip provenance values must be non-empty strings.",
        )
    return start + duration


def _validate_crop(value: Any, field: str, errors: list[dict[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        _validation_error(
            errors,
            "invalid_crop",
            field,
            "crop requires x, y, width, and height only.",
        )
        return
    numbers = [value.get(key) for key in ("x", "y", "width", "height")]
    if any(
        isinstance(number, bool) or not isinstance(number, (int, float))
        for number in numbers
    ):
        _validation_error(errors, "invalid_crop", field, "crop values must be numeric.")
        return
    x, y, width, height = numbers
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        _validation_error(
            errors,
            "invalid_crop",
            field,
            "crop must describe a positive rectangle inside 0-1 bounds.",
        )


def _validate_volume(value: Any, field: str, errors: list[dict[str, str]]) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -60 <= value <= 12:
        _validation_error(
            errors, "invalid_volume", field, "volume_db must be between -60 and 12."
        )


def _validate_text_clip(
    clip: dict[str, Any], prefix: str, errors: list[dict[str, str]]
) -> None:
    text = clip.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        _validation_error(
            errors,
            "invalid_text",
            f"{prefix}.text",
            "text must contain 1-2000 characters.",
        )
    if clip.get("font_family") not in ALLOWED_FONTS:
        _validation_error(
            errors,
            "invalid_font",
            f"{prefix}.font_family",
            "font_family is not allowlisted.",
        )
    font_size = clip.get("font_size")
    if (
        isinstance(font_size, bool)
        or not isinstance(font_size, (int, float))
        or not 6 <= font_size <= 300
    ):
        _validation_error(
            errors,
            "invalid_font_size",
            f"{prefix}.font_size",
            "font_size must be between 6 and 300.",
        )
    for field in ("color", "background"):
        value = clip.get(field)
        if not isinstance(value, str) or not HEX_COLOR_RE.fullmatch(value):
            _validation_error(
                errors,
                "invalid_color",
                f"{prefix}.{field}",
                f"{field} must be a hex color.",
            )
    position = clip.get("position")
    if not isinstance(position, dict) or set(position) != {"x", "y"}:
        _validation_error(
            errors,
            "invalid_position",
            f"{prefix}.position",
            "position requires x and y only.",
        )
    elif any(
        isinstance(position[key], bool)
        or not isinstance(position[key], (int, float))
        or not 0 <= position[key] <= 1
        for key in ("x", "y")
    ):
        _validation_error(
            errors,
            "invalid_position",
            f"{prefix}.position",
            "position values must be within 0-1.",
        )
    if clip.get("alignment") not in {"left", "center", "right"}:
        _validation_error(
            errors,
            "invalid_alignment",
            f"{prefix}.alignment",
            "alignment must be left, center, or right.",
        )
    transcript_ids = clip.get("transcript_segment_ids")
    if transcript_ids is not None:
        if not isinstance(transcript_ids, list) or len(transcript_ids) > 500:
            _validation_error(
                errors,
                "invalid_transcript_refs",
                f"{prefix}.transcript_segment_ids",
                "transcript_segment_ids must be an array with at most 500 IDs.",
            )
        else:
            for index, value in enumerate(transcript_ids):
                try:
                    _require_id(value, f"{prefix}.transcript_segment_ids[{index}]")
                except TimelineInputError as exc:
                    _validation_error(errors, "invalid_id", exc.field, str(exc))


def _validate_transitions(
    value: Any,
    clip_locations: dict[str, tuple[int, int, dict[str, Any], dict[str, Any]]],
    errors: list[dict[str, str]],
) -> set[tuple[str, str]]:
    allowed_pairs: set[tuple[str, str]] = set()
    if not isinstance(value, list) or len(value) > MAX_TRANSITIONS:
        _validation_error(
            errors,
            "invalid_transitions",
            "transitions",
            f"transitions must be an array with at most {MAX_TRANSITIONS} entries.",
        )
        return allowed_pairs
    if value:
        for index, _transition in enumerate(value):
            _validation_error(
                errors,
                "unsupported_render_transition",
                f"transitions[{index}]",
                "MemoLens 0.3 renders deterministic hard cuts only.",
            )
        return allowed_pairs
    transition_ids: set[str] = set()
    boundaries: set[tuple[str, str | None]] = set()
    for index, transition in enumerate(value):
        prefix = f"transitions[{index}]"
        if not isinstance(transition, dict) or set(transition) != {
            "id",
            "type",
            "from_clip_id",
            "to_clip_id",
            "duration_ms",
        }:
            _validation_error(
                errors,
                "invalid_transition",
                prefix,
                "Transition has an invalid shape.",
            )
            continue
        try:
            transition_id = _require_id(transition.get("id"), f"{prefix}.id")
            if transition_id in transition_ids:
                _validation_error(
                    errors,
                    "duplicate_id",
                    f"{prefix}.id",
                    "Transition IDs must be unique.",
                )
            transition_ids.add(transition_id)
            from_id = _require_id(
                transition.get("from_clip_id"), f"{prefix}.from_clip_id"
            )
            duration = _require_int(
                transition.get("duration_ms"),
                f"{prefix}.duration_ms",
                minimum=1,
                maximum=1000,
            )
        except TimelineInputError as exc:
            _validation_error(errors, "invalid_transition", exc.field, str(exc))
            continue
        transition_type = transition.get("type")
        if transition_type not in ALLOWED_TRANSITIONS:
            _validation_error(
                errors,
                "invalid_transition",
                f"{prefix}.type",
                "Unsupported transition type.",
            )
            continue
        from_location = clip_locations.get(from_id)
        if from_location is None:
            _validation_error(
                errors,
                "missing_reference",
                f"{prefix}.from_clip_id",
                "from_clip_id does not exist.",
            )
            continue
        _, _, from_track, from_clip = from_location
        if from_track.get("role") != "primary" or from_track.get("type") not in {
            "video",
            "image",
        }:
            _validation_error(
                errors,
                "invalid_transition",
                f"{prefix}.from_clip_id",
                "Transitions must originate on the primary visual track.",
            )
        from_duration = from_clip.get("timeline_duration_ms")
        if isinstance(from_duration, int) and duration > from_duration // 2:
            _validation_error(
                errors,
                "transition_too_long",
                f"{prefix}.duration_ms",
                "Transition cannot exceed half the source clip duration.",
            )
        to_id = transition.get("to_clip_id")
        if transition_type == "fade_to_black":
            if to_id is not None:
                _validation_error(
                    errors,
                    "invalid_transition",
                    f"{prefix}.to_clip_id",
                    "fade_to_black requires a null to_clip_id.",
                )
            boundary = (from_id, None)
        else:
            try:
                to_id = _require_id(to_id, f"{prefix}.to_clip_id")
            except TimelineInputError as exc:
                _validation_error(errors, "invalid_transition", exc.field, str(exc))
                continue
            to_location = clip_locations.get(to_id)
            if to_location is None:
                _validation_error(
                    errors,
                    "missing_reference",
                    f"{prefix}.to_clip_id",
                    "to_clip_id does not exist.",
                )
                continue
            from_track_index, from_clip_index, _, _ = from_location
            to_track_index, to_clip_index, to_track, to_clip = to_location
            if (
                from_track_index != to_track_index
                or to_track.get("role") != "primary"
                or to_clip_index != from_clip_index + 1
            ):
                _validation_error(
                    errors,
                    "invalid_transition",
                    f"{prefix}.to_clip_id",
                    "crossfade clips must be adjacent on the primary visual track.",
                )
            expected_start = (
                from_clip.get("timeline_start_ms", 0)
                + from_clip.get("timeline_duration_ms", 0)
                - duration
            )
            if to_clip.get("timeline_start_ms") != expected_start:
                _validation_error(
                    errors,
                    "transition_timing_mismatch",
                    f"{prefix}.duration_ms",
                    "crossfade duration must exactly equal the adjacent clip overlap.",
                )
            to_duration = to_clip.get("timeline_duration_ms")
            if isinstance(to_duration, int) and duration > to_duration // 2:
                _validation_error(
                    errors,
                    "transition_too_long",
                    f"{prefix}.duration_ms",
                    "Transition cannot exceed half the destination clip duration.",
                )
            boundary = (from_id, to_id)
            allowed_pairs.add((from_id, to_id))
        if boundary in boundaries:
            _validation_error(
                errors,
                "duplicate_transition",
                prefix,
                "A clip boundary can have only one transition.",
            )
        boundaries.add(boundary)
    return allowed_pairs


def _validate_primary_overlaps(
    tracks: list[Any],
    allowed_crossfades: set[tuple[str, str]],
    errors: list[dict[str, str]],
) -> None:
    for track_index, track in enumerate(tracks):
        if not isinstance(track, dict) or track.get("role") != "primary":
            continue
        clips = track.get("clips")
        if not isinstance(clips, list):
            continue
        for index, (previous, current) in enumerate(zip(clips, clips[1:]), start=1):
            if not isinstance(previous, dict) or not isinstance(current, dict):
                continue
            previous_end = previous.get("timeline_start_ms", 0) + previous.get(
                "timeline_duration_ms", 0
            )
            current_start = current.get("timeline_start_ms")
            if isinstance(current_start, int) and current_start < previous_end:
                pair = (previous.get("id"), current.get("id"))
                if pair not in allowed_crossfades:
                    _validation_error(
                        errors,
                        "overlapping_clips",
                        f"tracks[{track_index}].clips[{index}].timeline_start_ms",
                        "Primary visual clips may overlap only through a valid crossfade.",
                    )


def _validate_provenance(
    value: Any,
    revision: Any,
    tracks: list[Any],
    errors: list[dict[str, str]],
) -> None:
    field = "provenance"
    required = {
        "created_by",
        "parent_revision",
        "brief_revision",
        "operations",
        "source_analysis_runs",
        "source_assets",
        "created_at",
    }
    if not isinstance(value, dict) or set(value) != required:
        _validation_error(
            errors,
            "invalid_provenance",
            field,
            "Timeline provenance has an invalid shape.",
        )
        return
    if value.get("created_by") not in {"user", "director", "codex"}:
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.created_by",
            "Unsupported provenance creator.",
        )
    parent = value.get("parent_revision")
    if revision == 1 and parent is not None:
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.parent_revision",
            "Revision 1 must have a null parent_revision.",
        )
    if isinstance(revision, int) and revision > 1 and parent != revision - 1:
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.parent_revision",
            "parent_revision must reference the prior revision.",
        )
    try:
        _require_int(value.get("brief_revision"), f"{field}.brief_revision", minimum=1)
        _require_timestamp(value.get("created_at"), f"{field}.created_at")
    except TimelineInputError as exc:
        _validation_error(errors, "invalid_provenance", exc.field, str(exc))
    operations = value.get("operations")
    if (
        not isinstance(operations, list)
        or len(operations) > 100
        or any(not isinstance(operation, dict) for operation in operations)
    ):
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.operations",
            "operations must contain at most 100 objects.",
        )

    expected_assets: dict[str, str] = {}
    expected_segments: set[str] = set()
    for track in tracks:
        if not isinstance(track, dict):
            continue
        for clip in track.get("clips", []):
            if not isinstance(clip, dict) or track.get("type") == "text":
                continue
            asset_id = clip.get("asset_id")
            source_id = clip.get("asset_source_id")
            if isinstance(asset_id, str) and isinstance(source_id, str):
                if asset_id in expected_assets and expected_assets[asset_id] != source_id:
                    _validation_error(
                        errors,
                        "source_conflict",
                        f"{field}.source_assets",
                        "One asset cannot bind to multiple source IDs in a revision.",
                    )
                expected_assets[asset_id] = source_id
            segment_id = clip.get("segment_id")
            if isinstance(segment_id, str):
                expected_segments.add(segment_id)

    source_assets = value.get("source_assets")
    if not isinstance(source_assets, dict) or set(source_assets) != set(expected_assets):
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.source_assets",
            "source_assets must exactly cover all non-text assets.",
        )
    else:
        for asset_id, expected_source_id in expected_assets.items():
            record = source_assets.get(asset_id)
            if not isinstance(record, dict) or set(record) != {
                "sha256",
                "asset_source_id",
            }:
                _validation_error(
                    errors,
                    "invalid_provenance",
                    f"{field}.source_assets.{asset_id}",
                    "Source asset provenance has an invalid shape.",
                )
                continue
            try:
                _require_sha256(record.get("sha256"), f"{field}.source_assets.{asset_id}.sha256")
                source_id = _require_id(
                    record.get("asset_source_id"),
                    f"{field}.source_assets.{asset_id}.asset_source_id",
                )
                if source_id != expected_source_id:
                    _validation_error(
                        errors,
                        "source_mismatch",
                        f"{field}.source_assets.{asset_id}.asset_source_id",
                        "Clip source does not match source asset provenance.",
                    )
            except TimelineInputError as exc:
                _validation_error(errors, "invalid_provenance", exc.field, str(exc))

    source_runs = value.get("source_analysis_runs")
    if not isinstance(source_runs, dict) or set(source_runs) != expected_segments:
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.source_analysis_runs",
            "source_analysis_runs must exactly cover all segment references.",
        )
    elif any(
        not isinstance(segment_id, str)
        or not isinstance(run_id, str)
        or not ID_RE.fullmatch(segment_id)
        or not ID_RE.fullmatch(run_id)
        for segment_id, run_id in source_runs.items()
    ):
        _validation_error(
            errors,
            "invalid_provenance",
            f"{field}.source_analysis_runs",
            "Analysis provenance must map segment IDs to analysis run IDs.",
        )


def _validation_result(
    timeline_id: Any,
    revision: Any,
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "object": "memolens.timeline_validation",
        "schema_version": TIMELINE_RESPONSE_SCHEMA_VERSION,
        "status": "valid" if not errors else "invalid",
        "valid": not errors,
        "timeline_id": timeline_id if isinstance(timeline_id, str) else None,
        "revision": (
            revision if isinstance(revision, int) and not isinstance(revision, bool) else None
        ),
        "errors": errors,
        "warnings": warnings,
        "safety": {
            "pure_validation": True,
            "sqlite_access": False,
            "network_access": False,
            "persisted": False,
            "rendered": False,
            "exported": False,
        },
    }


def _clip_index(timeline: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str]]:
    return {
        clip["id"]: (clip, track["type"])
        for track in timeline["tracks"]
        for clip in track["clips"]
    }


def _filter_provenance_maps(timeline: dict[str, Any]) -> None:
    assets = {
        clip["asset_id"]
        for track in timeline["tracks"]
        if track["type"] != "text"
        for clip in track["clips"]
    }
    segments = {
        clip["segment_id"]
        for track in timeline["tracks"]
        for clip in track["clips"]
        if "segment_id" in clip
    }
    provenance = timeline["provenance"]
    provenance["source_assets"] = {
        key: value
        for key, value in provenance["source_assets"].items()
        if key in assets
    }
    provenance["source_analysis_runs"] = {
        key: value
        for key, value in provenance["source_analysis_runs"].items()
        if key in segments
    }


def revise_timeline_draft(
    *, timeline: dict[str, Any], operations: list[dict[str, Any]], created_at: str
) -> dict[str, Any]:
    """Apply typed operations to an unsaved draft and return a new revision."""

    validation = validate_timeline(timeline)
    if not validation["valid"]:
        first = validation["errors"][0]
        raise TimelineInputError(
            f"Input timeline is invalid: {first['message']}", field=first["field"]
        )
    created_at = _require_timestamp(created_at)
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise TimelineInputError(
            "operations must contain 1-100 typed operations.", field="operations"
        )

    revised = deepcopy(timeline)
    normalized_operations: list[dict[str, Any]] = []
    for index, raw in enumerate(operations):
        field = f"operations[{index}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("op"), str):
            raise TimelineInputError(f"{field} must contain an op string.", field=field)
        op = raw["op"]
        if op == "set_format":
            allowed = {
                "op",
                "width",
                "height",
                "fps",
                "sample_rate",
                "duration_ms",
                "background_color",
            }
            if set(raw) - allowed or len(raw) == 1:
                raise TimelineInputError(
                    f"{field} has invalid set_format fields.", field=field
                )
            revised["format"].update(
                {key: value for key, value in raw.items() if key != "op"}
            )
            normalized_operations.append(deepcopy(raw))
            continue
        if op == "set_transitions":
            raise TimelineInputError(
                "MemoLens 0.3 renders deterministic hard cuts only; set_transitions is unavailable.",
                field=field,
            )
        if op == "relink_source":
            if set(raw) != {"op", "asset_id", "asset_source_id", "asset_sha256"}:
                raise TimelineInputError(
                    f"{field} has invalid relink_source fields.", field=field
                )
            asset_id = _require_id(raw["asset_id"], f"{field}.asset_id")
            source_id = _require_id(
                raw["asset_source_id"], f"{field}.asset_source_id"
            )
            sha256 = _require_sha256(
                raw["asset_sha256"], f"{field}.asset_sha256"
            )
            affected = 0
            for track in revised["tracks"]:
                for clip in track["clips"]:
                    if clip.get("asset_id") == asset_id:
                        clip["asset_source_id"] = source_id
                        affected += 1
            if not affected:
                raise TimelineInputError(
                    f"{field}.asset_id does not exist.", field=f"{field}.asset_id"
                )
            revised["provenance"]["source_assets"][asset_id] = {
                "sha256": sha256,
                "asset_source_id": source_id,
            }
            normalized_operations.append(
                {**deepcopy(raw), "asset_sha256": sha256}
            )
            continue

        clips = _clip_index(revised)
        clip_id = _require_id(raw.get("clip_id"), f"{field}.clip_id")
        indexed = clips.get(clip_id)
        if indexed is None:
            raise TimelineInputError(
                f"{field}.clip_id does not exist.", field=f"{field}.clip_id"
            )
        clip, track_type = indexed
        if op == "move_clip":
            if set(raw) != {"op", "clip_id", "timeline_start_ms"}:
                raise TimelineInputError(
                    f"{field} has invalid move_clip fields.", field=field
                )
            clip["timeline_start_ms"] = _require_int(
                raw["timeline_start_ms"], f"{field}.timeline_start_ms", minimum=0
            )
        elif op == "trim_clip":
            if track_type not in {"video", "audio"} or set(raw) != {
                "op",
                "clip_id",
                "source_in_ms",
                "source_out_ms",
            }:
                raise TimelineInputError(
                    f"{field} has invalid trim_clip fields.", field=field
                )
            source_in = _require_int(
                raw["source_in_ms"], f"{field}.source_in_ms", minimum=0
            )
            source_out = _require_int(
                raw["source_out_ms"], f"{field}.source_out_ms", minimum=1
            )
            if source_out <= source_in:
                raise TimelineInputError(
                    f"{field}.source_out_ms must exceed source_in_ms.",
                    field=f"{field}.source_out_ms",
                )
            clip["source_in_ms"] = source_in
            clip["source_out_ms"] = source_out
            clip["timeline_duration_ms"] = source_out - source_in
        elif op == "set_volume":
            if track_type not in {"video", "audio"} or set(raw) != {
                "op",
                "clip_id",
                "volume_db",
            }:
                raise TimelineInputError(
                    f"{field} has invalid set_volume fields.", field=field
                )
            clip["volume_db"] = _require_number(
                raw["volume_db"], f"{field}.volume_db", minimum=-60, maximum=12
            )
        elif op == "set_fit":
            if track_type not in {"video", "image"} or set(raw) != {
                "op",
                "clip_id",
                "fit",
            } or raw.get("fit") not in ALLOWED_FITS:
                raise TimelineInputError(
                    f"{field} has invalid set_fit fields.", field=field
                )
            clip["fit"] = raw["fit"]
        elif op == "replace_clip":
            allowed = {
                "op",
                "clip_id",
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
            required = {"asset_id", "asset_source_id", "asset_sha256"}
            if set(raw) - allowed or not required.issubset(raw):
                raise TimelineInputError(
                    f"{field} has invalid replace_clip fields.", field=field
                )
            asset_id = _require_id(raw["asset_id"], f"{field}.asset_id")
            source_id = _require_id(
                raw["asset_source_id"], f"{field}.asset_source_id"
            )
            sha256 = _require_sha256(
                raw["asset_sha256"], f"{field}.asset_sha256"
            )
            clip["asset_id"] = asset_id
            clip["asset_source_id"] = source_id
            revised["provenance"]["source_assets"][asset_id] = {
                "sha256": sha256,
                "asset_source_id": source_id,
            }
            if track_type == "video":
                for required_field in (
                    "segment_id",
                    "analysis_run_id",
                    "analysis_revision",
                    "source_in_ms",
                    "source_out_ms",
                ):
                    if required_field not in raw:
                        raise TimelineInputError(
                            f"{field}.{required_field} is required for video replacement.",
                            field=f"{field}.{required_field}",
                        )
                segment_id = _require_id(
                    raw["segment_id"], f"{field}.segment_id"
                )
                run_id = _require_id(
                    raw["analysis_run_id"], f"{field}.analysis_run_id"
                )
                source_in = _require_int(
                    raw["source_in_ms"], f"{field}.source_in_ms", minimum=0
                )
                source_out = _require_int(
                    raw["source_out_ms"], f"{field}.source_out_ms", minimum=1
                )
                _require_int(
                    raw["analysis_revision"],
                    f"{field}.analysis_revision",
                    minimum=1,
                )
                if source_out <= source_in:
                    raise TimelineInputError(
                        f"{field}.source_out_ms must exceed source_in_ms.",
                        field=f"{field}.source_out_ms",
                    )
                clip.update(
                    {
                        "segment_id": segment_id,
                        "source_in_ms": source_in,
                        "source_out_ms": source_out,
                        "timeline_duration_ms": source_out - source_in,
                    }
                )
                revised["provenance"]["source_analysis_runs"][segment_id] = run_id
            elif track_type == "audio":
                if not {"source_in_ms", "source_out_ms"}.issubset(raw):
                    raise TimelineInputError(
                        f"{field} requires a source range for audio replacement.",
                        field=field,
                    )
                source_in = _require_int(
                    raw["source_in_ms"], f"{field}.source_in_ms", minimum=0
                )
                source_out = _require_int(
                    raw["source_out_ms"], f"{field}.source_out_ms", minimum=1
                )
                if source_out <= source_in:
                    raise TimelineInputError(
                        f"{field}.source_out_ms must exceed source_in_ms.",
                        field=f"{field}.source_out_ms",
                    )
                clip.update(
                    {
                        "source_in_ms": source_in,
                        "source_out_ms": source_out,
                        "timeline_duration_ms": source_out - source_in,
                    }
                )
            clip["provenance"] = {
                "reason": str(
                    raw.get("reason") or "Replaced in a typed revision."
                ).strip(),
                "match_id": str(
                    raw.get("match_id") or raw.get("segment_id") or asset_id
                ).strip(),
            }
        else:
            raise TimelineInputError(
                f"{field}.op is not supported.", field=f"{field}.op"
            )
        normalized_operations.append(deepcopy(raw))

    _filter_provenance_maps(revised)
    previous_revision = revised["revision"]
    revised["revision"] = previous_revision + 1
    revised["provenance"] = {
        **revised["provenance"],
        "created_by": "codex",
        "parent_revision": previous_revision,
        "operations": normalized_operations,
        "created_at": created_at,
    }
    validation = validate_timeline(revised)
    if not validation["valid"]:
        first = validation["errors"][0]
        raise TimelineInputError(first["message"], field=first["field"])
    return revised


__all__ = [
    "TIMELINE_RESPONSE_SCHEMA_VERSION",
    "TIMELINE_SCHEMA_VERSION",
    "TimelineInputError",
    "draft_timeline",
    "revise_timeline_draft",
    "validate_timeline",
]
