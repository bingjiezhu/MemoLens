from __future__ import annotations

import math
from typing import Protocol

from core.media_db import MediaRepository


SUPPORTED_FITS = {"contain", "cover", "stretch"}


class ValidationIssues(Protocol):
    def issue(self, code: str, pointer: str, message: str, **details: object) -> None: ...


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class TimelineMaterialValidator:
    """Validate repository-backed media identity and clip presentation values."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    def validate(
        self,
        clip: dict[str, object],
        pointer: str,
        issues: ValidationIssues,
    ) -> None:
        kind = clip.get("kind")
        asset_id = str(clip.get("asset_id") or "")
        source_id = str(clip.get("asset_source_id") or "")
        asset = self.repository.get_asset(asset_id)
        source = self.repository.get_asset_source(source_id)
        if not asset:
            issues.issue("unknown_asset", pointer + "/asset_id", "Asset does not exist.")
            return
        if not source or source.get("asset_id") != asset_id:
            issues.issue(
                "source_mismatch",
                pointer + "/asset_source_id",
                "Source does not belong to the asset.",
            )
            return
        if source.get("availability") != "available":
            issues.issue(
                "source_unavailable",
                pointer + "/asset_source_id",
                "Source is unavailable.",
                alternatives=self.repository.available_sources(asset_id),
            )
        if kind not in {"video", "image"} or kind != asset.get("kind"):
            issues.issue("kind_mismatch", pointer + "/kind", "Clip kind does not match the asset.")
        self._validate_source_range(clip, asset, asset_id, kind, pointer, issues)
        self._validate_presentation(clip, pointer, issues)

    def _validate_source_range(
        self,
        clip: dict[str, object],
        asset: dict[str, object],
        asset_id: str,
        kind: object,
        pointer: str,
        issues: ValidationIssues,
    ) -> None:
        if kind != "video":
            if "source_in_ms" in clip or "source_out_ms" in clip:
                issues.issue(
                    "unexpected_source_range",
                    pointer,
                    "Images must not contain a source range.",
                )
            return
        source_in, source_out = self._validate_video_source_range(
            clip,
            asset,
            pointer,
            issues,
        )
        self._validate_segment_range(
            clip,
            asset_id,
            source_in,
            source_out,
            pointer,
            issues,
        )

    @staticmethod
    def _validate_video_source_range(
        clip: dict[str, object],
        asset: dict[str, object],
        pointer: str,
        issues: ValidationIssues,
    ) -> tuple[int | None, int | None]:
        source_in = _integer(clip.get("source_in_ms"))
        source_out = _integer(clip.get("source_out_ms"))
        duration = _integer(clip.get("timeline_duration_ms")) or 0
        if source_in is None or source_in < 0 or source_out is None or source_out <= source_in:
            issues.issue(
                "invalid_source_range",
                pointer + "/source_in_ms",
                "Video source range is invalid.",
            )
        elif source_out - source_in != duration:
            issues.issue(
                "speed_not_supported",
                pointer + "/timeline_duration_ms",
                "P0 does not support speed changes.",
            )
        elif isinstance(asset.get("duration_ms"), int) and source_out > int(asset["duration_ms"]):
            issues.issue(
                "source_out_of_bounds",
                pointer + "/source_out_ms",
                "Source range exceeds asset duration.",
            )
        return source_in, source_out

    def _validate_segment_range(
        self,
        clip: dict[str, object],
        asset_id: str,
        source_in: int | None,
        source_out: int | None,
        pointer: str,
        issues: ValidationIssues,
    ) -> None:
        segment = self.repository.get_segment(str(clip.get("segment_id") or ""))
        if not segment or segment.get("asset_id") != asset_id:
            issues.issue(
                "unknown_segment",
                pointer + "/segment_id",
                "Segment does not belong to the asset.",
            )
            return
        tolerance = math.ceil(1000 / 24)
        if (
            source_in is not None
            and source_out is not None
            and (
                source_in < int(segment["start_ms"]) - tolerance
                or source_out > int(segment["end_ms"]) + tolerance
            )
        ):
            issues.issue(
                "segment_range_mismatch",
                pointer + "/source_in_ms",
                "Source range leaves the segment.",
            )

    @staticmethod
    def _validate_presentation(
        clip: dict[str, object],
        pointer: str,
        issues: ValidationIssues,
    ) -> None:
        if clip.get("fit") not in SUPPORTED_FITS:
            issues.issue(
                "unsupported_fit",
                pointer + "/fit",
                "Fit must be contain, cover, or stretch.",
            )
        TimelineMaterialValidator._validate_crop(clip.get("crop"), pointer, issues)
        TimelineMaterialValidator._validate_volume(clip.get("volume_db"), pointer, issues)

    @staticmethod
    def _validate_crop(crop: object, pointer: str, issues: ValidationIssues) -> None:
        values = [crop.get(key) for key in ("x", "y", "width", "height")] if isinstance(crop, dict) else []
        if len(values) != 4 or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            issues.issue("invalid_crop", pointer + "/crop", "Crop must contain four numbers.")
            return
        x, y, width, height = map(float, values)
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
            issues.issue(
                "invalid_crop",
                pointer + "/crop",
                "Crop must fit inside normalized bounds.",
            )

    @staticmethod
    def _validate_volume(volume: object, pointer: str, issues: ValidationIssues) -> None:
        if (
            not isinstance(volume, (int, float))
            or isinstance(volume, bool)
            or not -60 <= float(volume) <= 12
        ):
            issues.issue(
                "out_of_range",
                pointer + "/volume_db",
                "Volume must be -60..12 dB.",
            )
