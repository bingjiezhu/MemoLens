from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .timeline import clips_in_render_order
from .video import MediaCapabilityError


MIN_RENDER_FREE_BYTES = 256 * 1024 * 1024
ESTIMATED_RENDER_BYTES_PER_SECOND_720P = 3 * 1024 * 1024


@dataclass(frozen=True)
class RenderPlan:
    profile: str
    width: int
    height: int
    fps: int
    background_color: str
    duration_ms: int
    required_free_bytes: int
    clips: tuple[dict[str, object], ...]
    has_transitions: bool


def seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def even(value: int) -> int:
    return max(2, value if value % 2 == 0 else value - 1)


def dimensions(fmt: dict[str, object], profile: str) -> tuple[int, int]:
    width, height = int(fmt["width"]), int(fmt["height"])
    if profile == "preview-low":
        target_width, target_height = (1280, 720) if width > height else (720, 1280) if height > width else (720, 720)
    else:
        target_width, target_height = 1920, 1920
    scale = min(1.0, target_width / width, target_height / height)
    return even(round(width * scale)), even(round(height * scale))


def rotation_filters(rotation: object) -> list[str]:
    return {
        90: ["transpose=cclock"],
        180: ["hflip", "vflip"],
        270: ["transpose=clock"],
    }.get(rotation, [])


def redact_command(argv: list[str], replacements: dict[str, str]) -> list[str]:
    redacted: list[str] = []
    for value in argv:
        normalized = value
        for path, label in replacements.items():
            normalized = normalized.replace(path, label)
        redacted.append(normalized)
    if redacted:
        redacted[0] = Path(redacted[0]).name
    return redacted


def concat_line(path: Path) -> str:
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def concat_manifest(paths: Iterable[Path]) -> str:
    return "".join(concat_line(path) for path in paths)


def build_render_plan(timeline: dict[str, object], profile: str) -> RenderPlan:
    fmt = timeline["format"]
    width, height = dimensions(fmt, profile)
    duration_ms = int(fmt["duration_ms"])
    duration_seconds = duration_ms / 1000
    pixel_ratio = max(0.25, width * height / (1280 * 720))
    estimated_bytes = int(duration_seconds * ESTIMATED_RENDER_BYTES_PER_SECOND_720P * pixel_ratio * 2)
    return RenderPlan(
        profile=profile,
        width=width,
        height=height,
        fps=int(fmt["fps"]),
        background_color="0x" + str(fmt["background_color"]).removeprefix("#"),
        duration_ms=duration_ms,
        required_free_bytes=MIN_RENDER_FREE_BYTES + estimated_bytes,
        clips=tuple(clips_in_render_order(timeline)),
        has_transitions=bool(timeline.get("transitions")),
    )


def ensure_supported_features(plan: RenderPlan) -> None:
    if plan.has_transitions:
        raise MediaCapabilityError(
            "unsupported_render_transition",
            "This renderer does not yet support transitions; remove them before rendering.",
        )


@dataclass(frozen=True)
class _StreamSelection:
    video_index: object
    audio_index: object
    has_audio: bool


def _stream_selection(clip: dict[str, object], source: dict[str, object]) -> _StreamSelection:
    source_codec = source.get("codec", {})
    video_index = source_codec.get("video_stream_index")
    audio_index = source_codec.get("audio_stream_index")
    return _StreamSelection(
        video_index=video_index,
        audio_index=audio_index,
        has_audio=audio_index is not None and bool(clip.get("audio_enabled", True)),
    )


def _input_arguments(
    plan: RenderPlan,
    clip: dict[str, object],
    *,
    source_path: Path,
    input_path: Path,
    has_audio: bool,
) -> list[str]:
    duration = seconds(int(clip["timeline_duration_ms"]))
    if clip["kind"] == "video":
        arguments = [
            "-noautorotate",
            "-ss",
            seconds(int(clip["source_in_ms"])),
            "-t",
            duration,
            "-f",
            "mov",
            "-enable_drefs",
            "0",
            "-use_absolute_path",
            "0",
            "-i",
            str(source_path),
        ]
    else:
        arguments = ["-loop", "1", "-framerate", str(plan.fps), "-t", duration, "-i", str(input_path)]
    if not has_audio:
        arguments += ["-f", "lavfi", "-t", duration, "-i", "anullsrc=r=48000:cl=stereo"]
    return arguments


def _filter_chain(
    plan: RenderPlan,
    clip: dict[str, object],
    source: dict[str, object],
) -> str:
    filters = rotation_filters(source.get("rotation_degrees"))
    crop = clip.get("crop")
    if not isinstance(crop, dict):
        raise MediaCapabilityError("invalid_crop", "Clip crop is invalid.")
    if crop != {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}:
        filters.append(
            "crop="
            f"iw*{float(crop['width']):.8f}:ih*{float(crop['height']):.8f}:"
            f"iw*{float(crop['x']):.8f}:ih*{float(crop['y']):.8f}"
        )
    fit = clip.get("fit")
    if fit == "cover":
        filters += [
            f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=increase",
            f"crop={plan.width}:{plan.height}",
        ]
    elif fit == "contain":
        filters += [
            f"scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease",
            f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color={plan.background_color}",
        ]
    elif fit == "stretch":
        filters.append(f"scale={plan.width}:{plan.height}")
    else:
        raise MediaCapabilityError("unsupported_fit", "Clip fit is unsupported.")
    filters += [f"fps={plan.fps}", "setsar=1", "format=yuv420p"]
    return ",".join(filters)


def _mapping_arguments(clip: dict[str, object], streams: _StreamSelection) -> list[str]:
    return [
        "-map",
        f"0:{int(streams.video_index)}" if clip["kind"] == "video" else "0:v:0",
        "-map",
        f"0:{int(streams.audio_index)}" if streams.has_audio else "1:a:0",
    ]


def _audio_filter_arguments(clip: dict[str, object], streams: _StreamSelection) -> list[str]:
    if not streams.has_audio:
        return []
    duration = seconds(int(clip["timeline_duration_ms"]))
    return [
        "-af",
        f"volume={float(clip.get('volume_db', 0.0))}dB,aresample=48000,apad,atrim=0:{duration}",
    ]


def _encoding_arguments(plan: RenderPlan, duration_ms: int, output_path: Path) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        "fast" if plan.profile == "preview-low" else "medium",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k" if plan.profile == "preview-low" else "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-t",
        seconds(duration_ms),
        str(output_path),
    ]


def build_clip_command(
    plan: RenderPlan,
    clip: dict[str, object],
    *,
    source: dict[str, object],
    source_path: Path,
    input_path: Path,
    output_path: Path,
    ffmpeg_binary: str,
) -> list[str]:
    streams = _stream_selection(clip, source)
    duration_ms = int(clip["timeline_duration_ms"])
    argv = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "file,pipe,fd",
        "-n",
    ]
    argv += _input_arguments(
        plan,
        clip,
        source_path=source_path,
        input_path=input_path,
        has_audio=streams.has_audio,
    )
    argv += _mapping_arguments(clip, streams)
    argv += ["-vf", _filter_chain(plan, clip, source)]
    argv += _audio_filter_arguments(clip, streams)
    argv += _encoding_arguments(plan, duration_ms, output_path)
    return argv


def build_assemble_command(
    *,
    concat_file: Path,
    output_path: Path,
    ffmpeg_binary: str,
) -> list[str]:
    return [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-protocol_whitelist",
        "file,pipe,fd",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def validate_render_duration(plan: RenderPlan, probe: dict[str, object]) -> None:
    frame_tolerance_ms = max(50, int(2000 / max(plan.fps, 1)))
    if abs(int(probe["duration_ms"]) - plan.duration_ms) > frame_tolerance_ms:
        raise MediaCapabilityError(
            "render_duration_mismatch",
            "Rendered duration differs from the validated timeline.",
        )
