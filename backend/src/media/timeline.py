from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import datetime, timezone
from typing import Iterable

from core.media_db import MediaRepository, canonical_json, new_id


SUPPORTED_FPS = {24, 25, 30, 50, 60}
SUPPORTED_FITS = {"contain", "cover", "stretch"}
SUPPORTED_TRANSITIONS = {"crossfade", "fade_to_black"}
MAX_TIMELINE_DURATION_MS = 30 * 60 * 1000
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _issue(code: str, pointer: str, message: str, **details: object) -> dict[str, object]:
    value: dict[str, object] = {"code": code, "pointer": pointer, "field": pointer, "message": message}
    if details:
        value["details"] = details
    return value


def _operation_id(timeline_id: str, base_revision: int, value: object, index: int) -> str:
    digest = hashlib.sha256(f"{timeline_id}\0{base_revision}\0{index}\0{canonical_json(value)}".encode()).hexdigest()
    return f"op_{digest[:24]}"


class TimelineValidationError(ValueError):
    def __init__(self, errors: list[dict[str, object]]):
        super().__init__(str(errors[0].get("message")) if errors else "Timeline is invalid.")
        self.errors = errors


class TimelineService:
    """Deterministic timeline authority. FFmpeg never consumes free-form instructions."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    def create_from_project(self, project_id: str, *, brief_revision: int = 1) -> dict[str, object]:
        project = self.repository.get_project(project_id)
        brief_row = self.repository.get_brief(project_id, brief_revision)
        if project is None or brief_row is None:
            raise LookupError("Creative project or brief revision does not exist.")
        brief = brief_row["brief"]
        raw_candidates = brief.get("candidate_refs") if isinstance(brief, dict) else None
        candidates = [item for item in raw_candidates or [] if isinstance(item, dict)]
        if not candidates:
            raise ValueError("The creative brief has no grounded candidate assets.")

        target = min(max(_integer(brief.get("duration_ms")) or 30_000, 1_000), MAX_TIMELINE_DURATION_MS)
        clips: list[dict[str, object]] = []
        cursor = 0
        for candidate in candidates:
            if cursor >= target:
                break
            clip = self._clip_from_match(candidate, timeline_start_ms=cursor)
            if clip is None:
                continue
            duration = min(int(clip["timeline_duration_ms"]), target - cursor)
            if duration <= 0:
                continue
            clip["timeline_duration_ms"] = duration
            if clip["kind"] == "video":
                clip["source_out_ms"] = int(clip["source_in_ms"]) + duration
            clips.append(clip)
            cursor += duration
        if not clips:
            raise ValueError("No grounded candidate can be used in a timeline.")

        timeline_id = new_id("tl")
        ratio = str(brief.get("aspect_ratio") or "16:9")
        width, height = {
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:5": (1080, 1350),
            "16:9": (1920, 1080),
        }.get(ratio, (1920, 1080))
        timeline: dict[str, object] = {
            "schema_version": "1.0",
            "id": timeline_id,
            "project_id": project_id,
            "revision": 1,
            "format": {
                "width": width,
                "height": height,
                "fps": 30,
                "sample_rate": 48_000,
                "duration_ms": cursor,
                "background_color": "#000000",
            },
            "tracks": [
                {
                    "id": new_id("track"),
                    "type": "video",
                    "role": "primary",
                    "z_index": 0,
                    "muted": False,
                    "clips": clips,
                }
            ],
            "transitions": [],
            "provenance": self._provenance(
                clips,
                created_by="director",
                parent_revision=None,
                brief_revision=brief_revision,
                operations=[],
            ),
        }
        validation = self.validate(timeline)
        if not validation["valid"]:
            raise TimelineValidationError(validation["errors"])
        saved = self.repository.save_timeline(
            timeline_id=timeline_id,
            project_id=project_id,
            revision=1,
            timeline=timeline,
            provenance=timeline["provenance"],
            validation_status="valid",
        )
        return {
            "timeline": saved["timeline"],
            "content_sha256": saved["content_sha256"],
            "validation": validation,
            "diff": [],
        }

    def _clip_from_match(
        self,
        match: dict[str, object],
        *,
        timeline_start_ms: int,
        duration_override_ms: int | None = None,
    ) -> dict[str, object] | None:
        if match.get("result_type") == "video_segment":
            segment = self.repository.get_segment(str(match.get("id") or ""))
            if not segment or segment.get("source_availability") != "available":
                return None
            source_in = int(segment["start_ms"])
            available = int(segment["end_ms"]) - source_in
            duration = min(duration_override_ms or available, available)
            asset = self.repository.get_asset(str(segment["asset_id"]))
            if not asset:
                return None
            return self._base_clip(
                kind="video",
                asset=asset,
                asset_source_id=str(segment["asset_source_id"]),
                segment_id=str(segment["id"]),
                source_in_ms=source_in,
                source_out_ms=source_in + duration,
                timeline_start_ms=timeline_start_ms,
                timeline_duration_ms=duration,
                match_id=str(segment["id"]),
                reason="Grounded video segment selected by mixed retrieval.",
            )
        asset_id = str(match.get("asset_id") or match.get("id") or "")
        asset = self.repository.get_asset(asset_id)
        if not asset or asset.get("kind") != "image" or asset.get("source_availability") != "available":
            return None
        return self._base_clip(
            kind="image",
            asset=asset,
            asset_source_id=str(asset["asset_source_id"]),
            segment_id=None,
            source_in_ms=None,
            source_out_ms=None,
            timeline_start_ms=timeline_start_ms,
            timeline_duration_ms=duration_override_ms or 3_000,
            match_id=asset_id,
            reason="Grounded image selected by mixed retrieval.",
        )

    @staticmethod
    def _base_clip(
        *,
        kind: str,
        asset: dict[str, object],
        asset_source_id: str,
        segment_id: str | None,
        source_in_ms: int | None,
        source_out_ms: int | None,
        timeline_start_ms: int,
        timeline_duration_ms: int,
        match_id: str,
        reason: str,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "id": new_id("clip"),
            "kind": kind,
            "asset_id": str(asset["id"]),
            "asset_source_id": asset_source_id,
            "segment_id": segment_id,
            "timeline_start_ms": timeline_start_ms,
            "timeline_duration_ms": timeline_duration_ms,
            "fit": "cover",
            "crop": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "volume_db": 0.0,
            "audio_enabled": kind == "video",
            "provenance": {"reason": reason, "match_id": match_id},
        }
        if kind == "video":
            value["source_in_ms"] = source_in_ms
            value["source_out_ms"] = source_out_ms
        return value

    def _provenance(
        self,
        clips: list[dict[str, object]],
        *,
        created_by: str,
        parent_revision: int | None,
        brief_revision: int,
        operations: list[dict[str, object]],
    ) -> dict[str, object]:
        source_assets: dict[str, object] = {}
        source_runs: dict[str, str] = {}
        for clip in clips:
            asset = self.repository.get_asset(str(clip.get("asset_id") or ""))
            if asset:
                source_assets[str(asset["id"])] = {
                    "sha256": str(asset["sha256"]),
                    "asset_source_id": str(clip.get("asset_source_id") or ""),
                }
            if clip.get("segment_id"):
                segment = self.repository.get_segment(str(clip["segment_id"]))
                if segment:
                    source_runs[str(segment["id"])] = str(segment["analysis_run_id"])
        return {
            "created_by": created_by,
            "parent_revision": parent_revision,
            "brief_revision": brief_revision,
            "operations": operations,
            "source_analysis_runs": source_runs,
            "source_assets": source_assets,
            "created_at": _now(),
        }

    def validate(self, timeline: object) -> dict[str, object]:
        errors: list[dict[str, object]] = []
        warnings: list[dict[str, object]] = []
        if not isinstance(timeline, dict):
            return {
                "object": "timeline.validation",
                "schema_version": "1",
                "valid": False,
                "errors": [_issue("invalid_type", "", "Timeline must be an object.")],
                "warnings": [],
            }
        if timeline.get("schema_version") != "1.0":
            errors.append(_issue("unsupported_schema", "/schema_version", "Only timeline schema 1.0 is supported."))
        if not isinstance(timeline.get("id"), str) or not timeline.get("id"):
            errors.append(_issue("required", "/id", "Timeline id is required."))
        if not isinstance(timeline.get("project_id"), str) or not timeline.get("project_id"):
            errors.append(_issue("required", "/project_id", "Project id is required."))
        if (_integer(timeline.get("revision")) or 0) < 1:
            errors.append(_issue("invalid_revision", "/revision", "Revision must be positive."))

        fmt = timeline.get("format") if isinstance(timeline.get("format"), dict) else {}
        width, height, fps = _integer(fmt.get("width")), _integer(fmt.get("height")), _integer(fmt.get("fps"))
        declared_duration = _integer(fmt.get("duration_ms"))
        if width is None or not 240 <= width <= 3840:
            errors.append(_issue("out_of_range", "/format/width", "Width must be 240..3840."))
        if height is None or not 240 <= height <= 3840:
            errors.append(_issue("out_of_range", "/format/height", "Height must be 240..3840."))
        if fps not in SUPPORTED_FPS:
            errors.append(_issue("unsupported_fps", "/format/fps", "FPS must be 24, 25, 30, 50, or 60."))
        if declared_duration is None or not 0 < declared_duration <= MAX_TIMELINE_DURATION_MS:
            errors.append(_issue("out_of_range", "/format/duration_ms", "Duration must be 1 ms..30 minutes."))
        if not isinstance(fmt.get("background_color"), str) or not HEX_COLOR_RE.fullmatch(
            str(fmt.get("background_color"))
        ):
            errors.append(_issue("invalid_color", "/format/background_color", "Color must be #RRGGBB."))

        tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
        primary = [track for track in tracks if isinstance(track, dict) and track.get("role") == "primary"]
        if len(primary) != 1:
            errors.append(_issue("primary_track_count", "/tracks", "Exactly one primary visual track is required."))
        seen: set[str] = set()
        clip_lookup: dict[str, tuple[dict[str, object], int]] = {}
        max_end = 0
        for ti, track in enumerate(tracks):
            pointer = f"/tracks/{ti}"
            if not isinstance(track, dict):
                errors.append(_issue("invalid_type", pointer, "Track must be an object."))
                continue
            if track.get("type") not in {"video", "image", "text", "audio"}:
                errors.append(_issue("unsupported_track", pointer + "/type", "Unsupported track type."))
            clips = track.get("clips") if isinstance(track.get("clips"), list) else []
            intervals: list[tuple[int, int, str]] = []
            for ci, clip in enumerate(clips):
                cp = f"{pointer}/clips/{ci}"
                if not isinstance(clip, dict):
                    errors.append(_issue("invalid_type", cp, "Clip must be an object."))
                    continue
                clip_id = str(clip.get("id") or "")
                if not clip_id or clip_id in seen:
                    errors.append(_issue("duplicate_or_missing_id", cp + "/id", "Clip id must be unique."))
                seen.add(clip_id)
                clip_lookup[clip_id] = (clip, ti)
                start, duration = _integer(clip.get("timeline_start_ms")), _integer(clip.get("timeline_duration_ms"))
                if start is None or start < 0:
                    errors.append(_issue("out_of_range", cp + "/timeline_start_ms", "Start must be non-negative."))
                    start = 0
                if duration is None or duration <= 0:
                    errors.append(_issue("out_of_range", cp + "/timeline_duration_ms", "Duration must be positive."))
                    duration = 0
                end = start + duration
                max_end = max(max_end, end)
                intervals.append((start, end, clip_id))
                self._validate_material(clip, cp, errors)
            if track.get("role") == "primary":
                intervals.sort()
                for left, right in zip(intervals, intervals[1:]):
                    if right[0] < left[1] and not self._transition_matches(
                        timeline, left[2], right[2], left[1] - right[0]
                    ):
                        errors.append(
                            _issue(
                                "clip_overlap",
                                pointer + "/clips",
                                "Primary clips overlap without a matching crossfade.",
                            )
                        )

        transitions = timeline.get("transitions") if isinstance(timeline.get("transitions"), list) else []
        boundary_keys: set[tuple[str, str | None]] = set()
        for index, transition in enumerate(transitions):
            tp = f"/transitions/{index}"
            errors.append(
                _issue(
                    "unsupported_render_transition",
                    tp,
                    "Transitions are not renderable in this release.",
                )
            )
            if not isinstance(transition, dict):
                errors.append(_issue("invalid_type", tp, "Transition must be an object."))
                continue
            kind = transition.get("type")
            from_id = str(transition.get("from_clip_id") or "")
            to_id = transition.get("to_clip_id")
            duration = _integer(transition.get("duration_ms"))
            if kind not in SUPPORTED_TRANSITIONS:
                errors.append(_issue("unsupported_transition", tp + "/type", "Unsupported transition."))
            if duration is None or not 1 <= duration <= 1000:
                errors.append(_issue("out_of_range", tp + "/duration_ms", "Transition must be 1..1000 ms."))
            if from_id not in clip_lookup or (to_id is not None and str(to_id) not in clip_lookup):
                errors.append(_issue("unknown_clip", tp, "Transition references an unknown clip."))
            elif from_id in clip_lookup:
                from_clip, from_track_index = clip_lookup[from_id]
                transition_duration = duration or 0
                if transition_duration > int(from_clip.get("timeline_duration_ms") or 0) // 2:
                    errors.append(
                        _issue("transition_too_long", tp + "/duration_ms", "Transition exceeds half the clip.")
                    )
                if kind == "fade_to_black" and to_id is not None:
                    errors.append(
                        _issue(
                            "invalid_transition_target", tp + "/to_clip_id", "fade_to_black requires null to_clip_id."
                        )
                    )
                if kind == "crossfade":
                    if to_id is None or str(to_id) not in clip_lookup:
                        errors.append(
                            _issue("invalid_transition_target", tp + "/to_clip_id", "crossfade requires a target clip.")
                        )
                    else:
                        to_clip, to_track_index = clip_lookup[str(to_id)]
                        primary_track = tracks[from_track_index] if from_track_index < len(tracks) else None
                        primary_clips = primary_track.get("clips", []) if isinstance(primary_track, dict) else []
                        from_position = next(
                            (
                                i
                                for i, value in enumerate(primary_clips)
                                if isinstance(value, dict) and value.get("id") == from_id
                            ),
                            -1,
                        )
                        to_position = next(
                            (
                                i
                                for i, value in enumerate(primary_clips)
                                if isinstance(value, dict) and value.get("id") == str(to_id)
                            ),
                            -1,
                        )
                        overlap = (
                            int(from_clip.get("timeline_start_ms") or 0)
                            + int(from_clip.get("timeline_duration_ms") or 0)
                            - int(to_clip.get("timeline_start_ms") or 0)
                        )
                        if (
                            from_track_index != to_track_index
                            or not isinstance(primary_track, dict)
                            or primary_track.get("role") != "primary"
                            or to_position != from_position + 1
                            or overlap != transition_duration
                        ):
                            errors.append(
                                _issue(
                                    "invalid_crossfade_geometry",
                                    tp,
                                    "crossfade must bind adjacent primary clips with exact overlap.",
                                )
                            )
                        if transition_duration > int(to_clip.get("timeline_duration_ms") or 0) // 2:
                            errors.append(
                                _issue(
                                    "transition_too_long",
                                    tp + "/duration_ms",
                                    "Transition exceeds half the target clip.",
                                )
                            )
            key = (from_id, str(to_id) if to_id is not None else None)
            if key in boundary_keys:
                errors.append(_issue("duplicate_transition", tp, "A clip boundary can have only one transition."))
            boundary_keys.add(key)
        if declared_duration is not None and declared_duration != max_end:
            errors.append(
                _issue(
                    "duration_mismatch",
                    "/format/duration_ms",
                    "Duration must equal the maximum clip end.",
                    actual=max_end,
                )
            )
        if not isinstance(timeline.get("provenance"), dict):
            errors.append(_issue("required", "/provenance", "Timeline provenance is required."))
        return {
            "object": "timeline.validation",
            "schema_version": "1",
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
        }

    def _validate_material(self, clip: dict[str, object], pointer: str, errors: list[dict[str, object]]) -> None:
        kind = clip.get("kind")
        asset_id = str(clip.get("asset_id") or "")
        source_id = str(clip.get("asset_source_id") or "")
        asset = self.repository.get_asset(asset_id)
        source = self.repository.get_asset_source(source_id)
        if not asset:
            errors.append(_issue("unknown_asset", pointer + "/asset_id", "Asset does not exist."))
            return
        if not source or source.get("asset_id") != asset_id:
            errors.append(
                _issue("source_mismatch", pointer + "/asset_source_id", "Source does not belong to the asset.")
            )
            return
        if source.get("availability") != "available":
            errors.append(
                _issue(
                    "source_unavailable",
                    pointer + "/asset_source_id",
                    "Source is unavailable.",
                    alternatives=self.repository.available_sources(asset_id),
                )
            )
        if kind not in {"video", "image"} or kind != asset.get("kind"):
            errors.append(_issue("kind_mismatch", pointer + "/kind", "Clip kind does not match the asset."))
        duration = _integer(clip.get("timeline_duration_ms")) or 0
        if kind == "video":
            source_in, source_out = _integer(clip.get("source_in_ms")), _integer(clip.get("source_out_ms"))
            if source_in is None or source_in < 0 or source_out is None or source_out <= source_in:
                errors.append(
                    _issue("invalid_source_range", pointer + "/source_in_ms", "Video source range is invalid.")
                )
            elif source_out - source_in != duration:
                errors.append(
                    _issue(
                        "speed_not_supported", pointer + "/timeline_duration_ms", "P0 does not support speed changes."
                    )
                )
            elif isinstance(asset.get("duration_ms"), int) and source_out > int(asset["duration_ms"]):
                errors.append(
                    _issue("source_out_of_bounds", pointer + "/source_out_ms", "Source range exceeds asset duration.")
                )
            segment = self.repository.get_segment(str(clip.get("segment_id") or ""))
            tolerance = math.ceil(1000 / 24)
            if not segment or segment.get("asset_id") != asset_id:
                errors.append(
                    _issue("unknown_segment", pointer + "/segment_id", "Segment does not belong to the asset.")
                )
            elif (
                source_in is not None
                and source_out is not None
                and (
                    source_in < int(segment["start_ms"]) - tolerance or source_out > int(segment["end_ms"]) + tolerance
                )
            ):
                errors.append(
                    _issue("segment_range_mismatch", pointer + "/source_in_ms", "Source range leaves the segment.")
                )
        elif "source_in_ms" in clip or "source_out_ms" in clip:
            errors.append(_issue("unexpected_source_range", pointer, "Images must not contain a source range."))
        if clip.get("fit") not in SUPPORTED_FITS:
            errors.append(_issue("unsupported_fit", pointer + "/fit", "Fit must be contain, cover, or stretch."))
        crop = clip.get("crop")
        values = [crop.get(key) for key in ("x", "y", "width", "height")] if isinstance(crop, dict) else []
        if len(values) != 4 or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
        ):
            errors.append(_issue("invalid_crop", pointer + "/crop", "Crop must contain four numbers."))
        else:
            x, y, width, height = map(float, values)
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                errors.append(_issue("invalid_crop", pointer + "/crop", "Crop must fit inside normalized bounds."))
        volume = clip.get("volume_db")
        if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not -60 <= float(volume) <= 12:
            errors.append(_issue("out_of_range", pointer + "/volume_db", "Volume must be -60..12 dB."))

    @staticmethod
    def _transition_matches(timeline: dict[str, object], left: str, right: str, overlap: int) -> bool:
        return any(
            isinstance(item, dict)
            and item.get("type") == "crossfade"
            and item.get("from_clip_id") == left
            and item.get("to_clip_id") == right
            and item.get("duration_ms") == overlap
            for item in (timeline.get("transitions") or [])
        )

    def instruction_operations(self, timeline_id: str, base_revision: int, instruction: str) -> list[dict[str, object]]:
        row = self.repository.get_timeline(timeline_id, base_revision)
        if not row:
            raise LookupError("Timeline revision does not exist.")
        clips = list(clips_in_render_order(row["timeline"]))
        clauses = [
            value.strip()
            for value in re.split(r"[,;，；]|\b(?:and|then)\b|然后|再把", instruction, flags=re.I)
            if value.strip()
        ]
        operations: list[dict[str, object]] = []
        chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        for clause in clauses:
            index: int | None = None
            if re.search(r"最后|last", clause, re.I):
                index = len(clips) - 1
            else:
                match = re.search(r"第\s*([1-9一二两三四五六七八九])|(?:clip|shot)\s*#?\s*(\d+)", clause, re.I)
                if match:
                    ordinal = (
                        int(match.group(2))
                        if match.group(2)
                        else chinese.get(match.group(1), int(match.group(1)) if str(match.group(1)).isdigit() else 0)
                    )
                    index = ordinal - 1
            amount_match = re.search(r"(\d+(?:\.\d+)?)\s*(ms|毫秒|s|sec(?:ond)?s?|秒)", clause, re.I)
            if index is None or not 0 <= index < len(clips) or not amount_match:
                raise ValueError("instruction_not_safely_understood")
            delta = round(
                float(amount_match.group(1)) * (1 if amount_match.group(2).lower() in {"ms", "毫秒"} else 1000)
            )
            clip = clips[index]
            if re.search(r"缩短|减少|shorten|trim", clause, re.I):
                next_duration = int(clip["timeline_duration_ms"]) - delta
            elif re.search(r"延长|加长|extend|longer", clause, re.I):
                next_duration = int(clip["timeline_duration_ms"]) + delta
            else:
                raise ValueError("instruction_not_safely_understood")
            if next_duration <= 0:
                raise ValueError("instruction_would_create_invalid_duration")
            if clip.get("kind") == "video":
                source_in = int(clip["source_in_ms"])
                operation = {
                    "op": "trim_clip",
                    "clip_id": clip["id"],
                    "source_in_ms": source_in,
                    "source_out_ms": source_in + next_duration,
                }
            else:
                operation = {"op": "set_duration", "clip_id": clip["id"], "timeline_duration_ms": next_duration}
            operations.append(operation)
        return self.normalize_operations(timeline_id, base_revision, operations)

    def normalize_operations(
        self,
        timeline_id: str,
        base_revision: int,
        operations: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        aliases = {
            "delete_clip": "remove_clip",
            "set_clip_duration": "set_duration",
            "set_transition": "add_transition",
        }
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise ValueError(f"operations[{index}] must be an object.")
            operation = copy.deepcopy(raw)
            operation["op"] = aliases.get(str(operation.get("op")), operation.get("op"))
            operation.setdefault("op_id", _operation_id(timeline_id, base_revision, operation, index))
            operation.setdefault("preconditions", {"timeline_revision": base_revision})
            normalized.append(operation)
        return normalized

    def preview_revision(
        self,
        timeline_id: str,
        *,
        base_revision: int,
        operations: list[dict[str, object]],
    ) -> dict[str, object]:
        current = self.repository.get_timeline(timeline_id)
        if not current:
            raise LookupError("Timeline does not exist.")
        if int(current["revision"]) != base_revision:
            raise RuntimeError(f"revision_conflict:{current['revision']}")
        normalized = self.normalize_operations(timeline_id, base_revision, operations)
        timeline = copy.deepcopy(current["timeline"])
        diff: list[dict[str, object]] = []
        for operation in normalized:
            before = copy.deepcopy(timeline)
            self._apply_operation(timeline, operation)
            diff.append(
                {
                    "op": operation["op"],
                    "op_id": operation["op_id"],
                    "summary": self._operation_summary(operation),
                    "clip_id": operation.get("clip_id"),
                    "before_sha256": hashlib.sha256(canonical_json(before).encode()).hexdigest(),
                    "after_sha256": hashlib.sha256(canonical_json(timeline).encode()).hexdigest(),
                }
            )
        self._reflow(timeline)
        timeline["revision"] = base_revision + 1
        timeline["provenance"] = self._provenance(
            list(clips_in_render_order(timeline)),
            created_by="user",
            parent_revision=base_revision,
            brief_revision=int(current["brief_revision"]),
            operations=normalized,
        )
        validation = self.validate(timeline)
        if not validation["valid"]:
            raise TimelineValidationError(validation["errors"])
        return {
            "timeline": timeline,
            "operations": normalized,
            "diff": diff,
            "validation": validation,
            "content_sha256": hashlib.sha256(canonical_json(timeline).encode()).hexdigest(),
        }

    def revise(self, timeline_id: str, *, base_revision: int, operations: list[dict[str, object]]) -> dict[str, object]:
        preview = self.preview_revision(timeline_id, base_revision=base_revision, operations=operations)
        timeline = preview["timeline"]
        current = self.repository.get_timeline(timeline_id, base_revision)
        if not current:
            raise RuntimeError("revision_conflict:0")
        saved = self.repository.save_timeline_revision_cas(
            timeline_id=timeline_id,
            project_id=str(timeline["project_id"]),
            base_revision=base_revision,
            expected_base_sha256=str(current["content_sha256"]),
            timeline=timeline,
            provenance=timeline["provenance"],
            validation_status="valid",
        )
        return {**preview, "timeline": saved["timeline"], "content_sha256": saved["content_sha256"]}

    def _apply_operation(self, timeline: dict[str, object], operation: dict[str, object]) -> None:
        op = str(operation.get("op") or "")
        supported = {
            "remove_clip",
            "replace_clip",
            "relink_source",
            "trim_clip",
            "move_clip",
            "set_duration",
            "set_crop",
            "set_fit",
            "set_volume",
            "set_format",
            "add_transition",
            "remove_transition",
        }
        if op not in supported:
            raise ValueError(f"Unsupported timeline operation `{op}`.")
        preconditions = operation.get("preconditions")
        if not isinstance(preconditions, dict):
            raise ValueError("Operation preconditions are required.")
        expected_revision = preconditions.get("timeline_revision")
        if expected_revision != timeline.get("revision"):
            raise RuntimeError(f"revision_conflict:{timeline.get('revision')}")
        expected_sha = preconditions.get("timeline_content_sha256")
        if expected_sha is not None and expected_sha != hashlib.sha256(canonical_json(timeline).encode()).hexdigest():
            raise RuntimeError(f"revision_conflict:{timeline.get('revision')}")
        if op == "set_format":
            fmt = timeline.get("format")
            if not isinstance(fmt, dict):
                raise ValueError("Timeline format is invalid.")
            for key in ("width", "height", "fps", "background_color"):
                if key in operation:
                    fmt[key] = operation[key]
            return
        if op in {"add_transition", "remove_transition"}:
            transitions = timeline.setdefault("transitions", [])
            if not isinstance(transitions, list):
                raise ValueError("Timeline transitions are invalid.")
            if op == "remove_transition":
                transition_id = str(operation.get("transition_id") or "")
                timeline["transitions"] = [
                    item for item in transitions if not isinstance(item, dict) or item.get("id") != transition_id
                ]
            else:
                raise ValueError("unsupported_timeline_feature: transitions are not renderable in this release")
            return
        found = self._find_clip(timeline, str(operation.get("clip_id") or ""))
        if not found:
            raise ValueError("Operation references an unknown clip.")
        clips, index, clip = found
        if op == "remove_clip":
            clips.pop(index)
        elif op == "move_clip":
            target = _integer(operation.get("to_index"))
            if target is None or not 0 <= target < len(clips):
                raise ValueError("to_index is outside the track.")
            clips.insert(target, clips.pop(index))
        elif op == "trim_clip":
            if clip.get("kind") != "video":
                raise ValueError("Only video clips can be trimmed.")
            start, end = _integer(operation.get("source_in_ms")), _integer(operation.get("source_out_ms"))
            if start is None or end is None or end <= start:
                raise ValueError("Trim range is invalid.")
            clip.update(source_in_ms=start, source_out_ms=end, timeline_duration_ms=end - start)
        elif op == "set_duration":
            duration = _integer(operation.get("timeline_duration_ms"))
            if duration is None or duration <= 0:
                raise ValueError("Duration must be positive.")
            if clip.get("kind") == "video":
                clip["source_out_ms"] = int(clip["source_in_ms"]) + duration
            clip["timeline_duration_ms"] = duration
        elif op == "replace_clip":
            match_id = str(operation.get("match_id") or "")
            segment = self.repository.get_segment(match_id)
            match = (
                {"id": match_id, "result_type": "video_segment"}
                if segment
                else {"id": match_id, "asset_id": match_id, "result_type": "image_asset"}
            )
            replacement = self._clip_from_match(
                match,
                timeline_start_ms=int(clip["timeline_start_ms"]),
                duration_override_ms=int(clip["timeline_duration_ms"]),
            )
            if not replacement:
                raise ValueError("Replacement match is unavailable.")
            replacement["id"] = clip["id"]
            clips[index] = replacement
        elif op == "relink_source":
            source = self.repository.get_asset_source(str(operation.get("asset_source_id") or ""))
            if (
                not source
                or source.get("asset_id") != clip.get("asset_id")
                or source.get("availability") != "available"
            ):
                raise ValueError("Replacement source is unavailable or belongs to another asset.")
            clip["asset_source_id"] = source["id"]
        elif op == "set_crop":
            clip["crop"] = copy.deepcopy(operation.get("crop"))
        elif op == "set_fit":
            clip["fit"] = operation.get("fit")
        elif op == "set_volume":
            clip["volume_db"] = operation.get("volume_db")

    @staticmethod
    def _find_clip(
        timeline: dict[str, object], clip_id: str
    ) -> tuple[list[dict[str, object]], int, dict[str, object]] | None:
        for track in timeline.get("tracks") or []:
            if not isinstance(track, dict) or not isinstance(track.get("clips"), list):
                continue
            for index, clip in enumerate(track["clips"]):
                if isinstance(clip, dict) and clip.get("id") == clip_id:
                    return track["clips"], index, clip
        return None

    @staticmethod
    def _reflow(timeline: dict[str, object]) -> None:
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

    @staticmethod
    def _operation_summary(operation: dict[str, object]) -> str:
        return f"{operation.get('op')} {operation.get('clip_id') or operation.get('transition_id') or ''}".strip()


def clips_in_render_order(timeline: dict[str, object]) -> Iterable[dict[str, object]]:
    tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
    primary = [track for track in tracks if isinstance(track, dict) and track.get("role") == "primary"]
    if len(primary) != 1 or not isinstance(primary[0].get("clips"), list):
        return []
    return [clip for clip in primary[0]["clips"] if isinstance(clip, dict)]
