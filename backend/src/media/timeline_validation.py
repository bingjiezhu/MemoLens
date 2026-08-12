from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.media_db import MediaRepository
from .timeline_materials import TimelineMaterialValidator


SUPPORTED_FPS = {24, 25, 30, 50, 60}
SUPPORTED_TRANSITIONS = {"crossfade", "fade_to_black"}
MAX_TIMELINE_DURATION_MS = 30 * 60 * 1000
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _issue(code: str, pointer: str, message: str, **details: object) -> dict[str, object]:
    value: dict[str, object] = {
        "code": code,
        "pointer": pointer,
        "field": pointer,
        "message": message,
    }
    if details:
        value["details"] = details
    return value


@dataclass
class ValidationContext:
    errors: list[dict[str, object]] = field(default_factory=list)
    warnings: list[dict[str, object]] = field(default_factory=list)
    seen_clip_ids: set[str] = field(default_factory=set)
    clip_lookup: dict[str, tuple[dict[str, object], int]] = field(default_factory=dict)
    max_end: int = 0

    def issue(self, code: str, pointer: str, message: str, **details: object) -> None:
        self.errors.append(_issue(code, pointer, message, **details))


class TimelineValidator:
    """Validate timeline structure, geometry, and repository-backed material."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository
        self.materials = TimelineMaterialValidator(repository)

    def validate(self, timeline: object) -> dict[str, object]:
        if not isinstance(timeline, dict):
            return self._response(
                [_issue("invalid_type", "", "Timeline must be an object.")],
                [],
            )

        context = ValidationContext()
        self._validate_identity(timeline, context)
        declared_duration = self._validate_format(timeline, context)
        tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
        self._validate_tracks(timeline, tracks, context)
        self._validate_transitions(timeline, tracks, context)
        if declared_duration is not None and declared_duration != context.max_end:
            context.issue(
                "duration_mismatch",
                "/format/duration_ms",
                "Duration must equal the maximum clip end.",
                actual=context.max_end,
            )
        if not isinstance(timeline.get("provenance"), dict):
            context.issue("required", "/provenance", "Timeline provenance is required.")
        return self._response(context.errors, context.warnings)

    @staticmethod
    def _response(
        errors: list[dict[str, object]],
        warnings: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "object": "timeline.validation",
            "schema_version": "1",
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def _validate_identity(timeline: dict[str, object], context: ValidationContext) -> None:
        if timeline.get("schema_version") != "1.0":
            context.issue(
                "unsupported_schema",
                "/schema_version",
                "Only timeline schema 1.0 is supported.",
            )
        if not isinstance(timeline.get("id"), str) or not timeline.get("id"):
            context.issue("required", "/id", "Timeline id is required.")
        if not isinstance(timeline.get("project_id"), str) or not timeline.get("project_id"):
            context.issue("required", "/project_id", "Project id is required.")
        if (_integer(timeline.get("revision")) or 0) < 1:
            context.issue("invalid_revision", "/revision", "Revision must be positive.")

    @staticmethod
    def _validate_format(timeline: dict[str, object], context: ValidationContext) -> int | None:
        timeline_format = timeline.get("format") if isinstance(timeline.get("format"), dict) else {}
        width = _integer(timeline_format.get("width"))
        height = _integer(timeline_format.get("height"))
        fps = _integer(timeline_format.get("fps"))
        declared_duration = _integer(timeline_format.get("duration_ms"))
        if width is None or not 240 <= width <= 3840:
            context.issue("out_of_range", "/format/width", "Width must be 240..3840.")
        if height is None or not 240 <= height <= 3840:
            context.issue("out_of_range", "/format/height", "Height must be 240..3840.")
        if fps not in SUPPORTED_FPS:
            context.issue("unsupported_fps", "/format/fps", "FPS must be 24, 25, 30, 50, or 60.")
        if declared_duration is None or not 0 < declared_duration <= MAX_TIMELINE_DURATION_MS:
            context.issue(
                "out_of_range",
                "/format/duration_ms",
                "Duration must be 1 ms..30 minutes.",
            )
        background = timeline_format.get("background_color")
        if not isinstance(background, str) or not HEX_COLOR_RE.fullmatch(str(background)):
            context.issue(
                "invalid_color",
                "/format/background_color",
                "Color must be #RRGGBB.",
            )
        return declared_duration

    def _validate_tracks(
        self,
        timeline: dict[str, object],
        tracks: list[object],
        context: ValidationContext,
    ) -> None:
        primary = [
            track
            for track in tracks
            if isinstance(track, dict) and track.get("role") == "primary"
        ]
        if len(primary) != 1:
            context.issue(
                "primary_track_count",
                "/tracks",
                "Exactly one primary visual track is required.",
            )
        for track_index, track in enumerate(tracks):
            self._validate_track(timeline, track, track_index, context)

    def _validate_track(
        self,
        timeline: dict[str, object],
        track: object,
        track_index: int,
        context: ValidationContext,
    ) -> None:
        pointer = f"/tracks/{track_index}"
        if not isinstance(track, dict):
            context.issue("invalid_type", pointer, "Track must be an object.")
            return
        if track.get("type") not in {"video", "image", "text", "audio"}:
            context.issue("unsupported_track", pointer + "/type", "Unsupported track type.")
        clips = track.get("clips") if isinstance(track.get("clips"), list) else []
        intervals: list[tuple[int, int, str]] = []
        for clip_index, clip in enumerate(clips):
            interval = self._validate_clip(
                clip,
                track_index,
                f"{pointer}/clips/{clip_index}",
                context,
            )
            if interval is not None:
                intervals.append(interval)
        if track.get("role") == "primary":
            self._validate_primary_overlaps(timeline, intervals, pointer, context)

    def _validate_clip(
        self,
        clip: object,
        track_index: int,
        pointer: str,
        context: ValidationContext,
    ) -> tuple[int, int, str] | None:
        if not isinstance(clip, dict):
            context.issue("invalid_type", pointer, "Clip must be an object.")
            return None
        clip_id = str(clip.get("id") or "")
        if not clip_id or clip_id in context.seen_clip_ids:
            context.issue(
                "duplicate_or_missing_id",
                pointer + "/id",
                "Clip id must be unique.",
            )
        context.seen_clip_ids.add(clip_id)
        context.clip_lookup[clip_id] = (clip, track_index)

        start = _integer(clip.get("timeline_start_ms"))
        duration = _integer(clip.get("timeline_duration_ms"))
        if start is None or start < 0:
            context.issue(
                "out_of_range",
                pointer + "/timeline_start_ms",
                "Start must be non-negative.",
            )
            start = 0
        if duration is None or duration <= 0:
            context.issue(
                "out_of_range",
                pointer + "/timeline_duration_ms",
                "Duration must be positive.",
            )
            duration = 0
        end = start + duration
        context.max_end = max(context.max_end, end)
        self.materials.validate(clip, pointer, context)
        return start, end, clip_id

    @staticmethod
    def _validate_primary_overlaps(
        timeline: dict[str, object],
        intervals: list[tuple[int, int, str]],
        pointer: str,
        context: ValidationContext,
    ) -> None:
        intervals.sort()
        for left, right in zip(intervals, intervals[1:]):
            overlap = left[1] - right[0]
            if right[0] < left[1] and not TimelineValidator._transition_matches(
                timeline,
                left[2],
                right[2],
                overlap,
            ):
                context.issue(
                    "clip_overlap",
                    pointer + "/clips",
                    "Primary clips overlap without a matching crossfade.",
                )

    def _validate_transitions(
        self,
        timeline: dict[str, object],
        tracks: list[object],
        context: ValidationContext,
    ) -> None:
        transitions = timeline.get("transitions") if isinstance(timeline.get("transitions"), list) else []
        boundary_keys: set[tuple[str, str | None]] = set()
        for index, transition in enumerate(transitions):
            pointer = f"/transitions/{index}"
            context.issue(
                "unsupported_render_transition",
                pointer,
                "Transitions are not renderable in this release.",
            )
            if not isinstance(transition, dict):
                context.issue("invalid_type", pointer, "Transition must be an object.")
                continue
            self._validate_transition(transition, pointer, tracks, context)
            key = (
                str(transition.get("from_clip_id") or ""),
                str(transition["to_clip_id"]) if transition.get("to_clip_id") is not None else None,
            )
            if key in boundary_keys:
                context.issue(
                    "duplicate_transition",
                    pointer,
                    "A clip boundary can have only one transition.",
                )
            boundary_keys.add(key)

    def _validate_transition(
        self,
        transition: dict[str, object],
        pointer: str,
        tracks: list[object],
        context: ValidationContext,
    ) -> None:
        kind = transition.get("type")
        from_id = str(transition.get("from_clip_id") or "")
        to_id = transition.get("to_clip_id")
        duration = _integer(transition.get("duration_ms"))
        if kind not in SUPPORTED_TRANSITIONS:
            context.issue("unsupported_transition", pointer + "/type", "Unsupported transition.")
        if duration is None or not 1 <= duration <= 1000:
            context.issue(
                "out_of_range",
                pointer + "/duration_ms",
                "Transition must be 1..1000 ms.",
            )
        if from_id not in context.clip_lookup or (
            to_id is not None and str(to_id) not in context.clip_lookup
        ):
            context.issue("unknown_clip", pointer, "Transition references an unknown clip.")
            return
        from_clip, from_track_index = context.clip_lookup[from_id]
        transition_duration = duration or 0
        self._validate_transition_length(from_clip, transition_duration, pointer, context)
        if kind == "fade_to_black" and to_id is not None:
            context.issue(
                "invalid_transition_target",
                pointer + "/to_clip_id",
                "fade_to_black requires null to_clip_id.",
            )
        if kind == "crossfade":
            self._validate_crossfade(
                from_id,
                to_id,
                transition_duration,
                from_clip,
                from_track_index,
                pointer,
                tracks,
                context,
            )

    @staticmethod
    def _validate_transition_length(
        clip: dict[str, object],
        duration: int,
        pointer: str,
        context: ValidationContext,
        *,
        target: bool = False,
    ) -> None:
        if duration <= int(clip.get("timeline_duration_ms") or 0) // 2:
            return
        message = "Transition exceeds half the target clip." if target else "Transition exceeds half the clip."
        context.issue("transition_too_long", pointer + "/duration_ms", message)

    @staticmethod
    def _validate_crossfade(
        from_id: str,
        to_id: object,
        duration: int,
        from_clip: dict[str, object],
        from_track_index: int,
        pointer: str,
        tracks: list[object],
        context: ValidationContext,
    ) -> None:
        if to_id is None or str(to_id) not in context.clip_lookup:
            context.issue(
                "invalid_transition_target",
                pointer + "/to_clip_id",
                "crossfade requires a target clip.",
            )
            return
        to_clip, to_track_index = context.clip_lookup[str(to_id)]
        primary_track = tracks[from_track_index] if from_track_index < len(tracks) else None
        primary_clips = primary_track.get("clips", []) if isinstance(primary_track, dict) else []
        from_position = TimelineValidator._clip_position(primary_clips, from_id)
        to_position = TimelineValidator._clip_position(primary_clips, str(to_id))
        overlap = (
            int(from_clip.get("timeline_start_ms") or 0)
            + int(from_clip.get("timeline_duration_ms") or 0)
            - int(to_clip.get("timeline_start_ms") or 0)
        )
        invalid_geometry = (
            from_track_index != to_track_index
            or not isinstance(primary_track, dict)
            or primary_track.get("role") != "primary"
            or to_position != from_position + 1
            or overlap != duration
        )
        if invalid_geometry:
            context.issue(
                "invalid_crossfade_geometry",
                pointer,
                "crossfade must bind adjacent primary clips with exact overlap.",
            )
        TimelineValidator._validate_transition_length(
            to_clip,
            duration,
            pointer,
            context,
            target=True,
        )

    @staticmethod
    def _clip_position(clips: object, clip_id: str) -> int:
        if not isinstance(clips, list):
            return -1
        return next(
            (
                index
                for index, clip in enumerate(clips)
                if isinstance(clip, dict) and clip.get("id") == clip_id
            ),
            -1,
        )

    @staticmethod
    def _transition_matches(
        timeline: dict[str, object],
        left: str,
        right: str,
        overlap: int,
    ) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("type") == "crossfade"
            and item.get("from_clip_id") == left
            and item.get("to_clip_id") == right
            and item.get("duration_ms") == overlap
            for item in (timeline.get("transitions") or [])
        )
