from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Protocol

from core.media_db import MediaRepository, canonical_json


class ClipFactory(Protocol):
    def __call__(
        self,
        match: dict[str, object],
        *,
        timeline_start_ms: int,
        duration_override_ms: int | None = None,
    ) -> dict[str, object] | None: ...


@dataclass(frozen=True)
class ClipLocation:
    clips: list[dict[str, object]]
    index: int
    clip: dict[str, object]


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def find_clip(timeline: dict[str, object], clip_id: str) -> ClipLocation | None:
    for track in timeline.get("tracks") or []:
        if not isinstance(track, dict) or not isinstance(track.get("clips"), list):
            continue
        clips = track["clips"]
        for index, clip in enumerate(clips):
            if isinstance(clip, dict) and clip.get("id") == clip_id:
                return ClipLocation(clips=clips, index=index, clip=clip)
    return None


def reflow_timeline(timeline: dict[str, object]) -> None:
    max_end = 0
    for track in timeline.get("tracks") or []:
        if not isinstance(track, dict) or not isinstance(track.get("clips"), list):
            continue
        cursor = 0
        for clip in track["clips"]:
            if not isinstance(clip, dict):
                continue
            clip["timeline_start_ms"] = cursor
            cursor += int(clip.get("timeline_duration_ms") or 0)
        max_end = max(max_end, cursor)
    if isinstance(timeline.get("format"), dict):
        timeline["format"]["duration_ms"] = max_end


def operation_summary(operation: dict[str, object]) -> str:
    subject = operation.get("clip_id") or operation.get("transition_id") or ""
    return f"{operation.get('op')} {subject}".strip()


class TimelineEditor:
    """Apply validated, typed edits without owning revision or persistence policy."""

    def __init__(self, repository: MediaRepository, clip_factory: ClipFactory):
        self.repository = repository
        self.clip_factory = clip_factory
        self._clip_handlers = {
            "remove_clip": self._remove_clip,
            "replace_clip": self._replace_clip,
            "relink_source": self._relink_source,
            "trim_clip": self._trim_clip,
            "move_clip": self._move_clip,
            "set_duration": self._set_duration,
            "set_crop": self._set_crop,
            "set_fit": self._set_fit,
            "set_volume": self._set_volume,
        }

    def apply(self, timeline: dict[str, object], operation: dict[str, object]) -> None:
        operation_name = str(operation.get("op") or "")
        if operation_name == "set_format":
            self._validate_preconditions(timeline, operation)
            self._set_format(timeline, operation)
            return
        if operation_name in {"add_transition", "remove_transition"}:
            self._validate_preconditions(timeline, operation)
            self._edit_transition(timeline, operation)
            return

        handler = self._clip_handlers.get(operation_name)
        if handler is None:
            raise ValueError(f"Unsupported timeline operation `{operation_name}`.")
        self._validate_preconditions(timeline, operation)
        location = find_clip(timeline, str(operation.get("clip_id") or ""))
        if location is None:
            raise ValueError("Operation references an unknown clip.")
        handler(location, operation)

    @staticmethod
    def _validate_preconditions(timeline: dict[str, object], operation: dict[str, object]) -> None:
        preconditions = operation.get("preconditions")
        if not isinstance(preconditions, dict):
            raise ValueError("Operation preconditions are required.")
        if preconditions.get("timeline_revision") != timeline.get("revision"):
            raise RuntimeError(f"revision_conflict:{timeline.get('revision')}")
        expected_sha = preconditions.get("timeline_content_sha256")
        actual_sha = hashlib.sha256(canonical_json(timeline).encode()).hexdigest()
        if expected_sha is not None and expected_sha != actual_sha:
            raise RuntimeError(f"revision_conflict:{timeline.get('revision')}")

    @staticmethod
    def _set_format(timeline: dict[str, object], operation: dict[str, object]) -> None:
        timeline_format = timeline.get("format")
        if not isinstance(timeline_format, dict):
            raise ValueError("Timeline format is invalid.")
        for key in ("width", "height", "fps", "background_color"):
            if key in operation:
                timeline_format[key] = operation[key]

    @staticmethod
    def _edit_transition(timeline: dict[str, object], operation: dict[str, object]) -> None:
        transitions = timeline.setdefault("transitions", [])
        if not isinstance(transitions, list):
            raise ValueError("Timeline transitions are invalid.")
        if operation.get("op") == "add_transition":
            raise ValueError("unsupported_timeline_feature: transitions are not renderable in this release")
        transition_id = str(operation.get("transition_id") or "")
        timeline["transitions"] = [
            item
            for item in transitions
            if not isinstance(item, dict) or item.get("id") != transition_id
        ]

    @staticmethod
    def _remove_clip(location: ClipLocation, _operation: dict[str, object]) -> None:
        location.clips.pop(location.index)

    @staticmethod
    def _move_clip(location: ClipLocation, operation: dict[str, object]) -> None:
        target = _integer(operation.get("to_index"))
        if target is None or not 0 <= target < len(location.clips):
            raise ValueError("to_index is outside the track.")
        location.clips.insert(target, location.clips.pop(location.index))

    @staticmethod
    def _trim_clip(location: ClipLocation, operation: dict[str, object]) -> None:
        if location.clip.get("kind") != "video":
            raise ValueError("Only video clips can be trimmed.")
        start = _integer(operation.get("source_in_ms"))
        end = _integer(operation.get("source_out_ms"))
        if start is None or end is None or end <= start:
            raise ValueError("Trim range is invalid.")
        location.clip.update(
            source_in_ms=start,
            source_out_ms=end,
            timeline_duration_ms=end - start,
        )

    @staticmethod
    def _set_duration(location: ClipLocation, operation: dict[str, object]) -> None:
        duration = _integer(operation.get("timeline_duration_ms"))
        if duration is None or duration <= 0:
            raise ValueError("Duration must be positive.")
        if location.clip.get("kind") == "video":
            location.clip["source_out_ms"] = int(location.clip["source_in_ms"]) + duration
        location.clip["timeline_duration_ms"] = duration

    def _replace_clip(self, location: ClipLocation, operation: dict[str, object]) -> None:
        match_id = str(operation.get("match_id") or "")
        segment = self.repository.get_segment(match_id)
        match = (
            {"id": match_id, "result_type": "video_segment"}
            if segment
            else {"id": match_id, "asset_id": match_id, "result_type": "image_asset"}
        )
        replacement = self.clip_factory(
            match,
            timeline_start_ms=int(location.clip["timeline_start_ms"]),
            duration_override_ms=int(location.clip["timeline_duration_ms"]),
        )
        if replacement is None:
            raise ValueError("Replacement match is unavailable.")
        replacement["id"] = location.clip["id"]
        location.clips[location.index] = replacement

    def _relink_source(self, location: ClipLocation, operation: dict[str, object]) -> None:
        source = self.repository.get_asset_source(str(operation.get("asset_source_id") or ""))
        if (
            not source
            or source.get("asset_id") != location.clip.get("asset_id")
            or source.get("availability") != "available"
        ):
            raise ValueError("Replacement source is unavailable or belongs to another asset.")
        location.clip["asset_source_id"] = source["id"]

    @staticmethod
    def _set_crop(location: ClipLocation, operation: dict[str, object]) -> None:
        location.clip["crop"] = copy.deepcopy(operation.get("crop"))

    @staticmethod
    def _set_fit(location: ClipLocation, operation: dict[str, object]) -> None:
        location.clip["fit"] = operation.get("fit")

    @staticmethod
    def _set_volume(location: ClipLocation, operation: dict[str, object]) -> None:
        location.clip["volume_db"] = operation.get("volume_db")
