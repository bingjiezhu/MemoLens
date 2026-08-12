from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
from PIL import Image, ImageStat

from core.media_db import MediaRepository, new_id


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic", ".heif"}
SIDE_CAR_EXTENSIONS = (".srt", ".vtt")
MAX_VIDEO_DURATION_MS = 8 * 60 * 60 * 1000
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024 * 1024
MAX_SOURCE_DIMENSION = 16_384
MAX_SOURCE_PIXELS = 67_108_864
DEFAULT_MAX_SEGMENT_MS = 30_000
DEFAULT_FALLBACK_INTERVAL_MS = 5_000
DEFAULT_MIN_SEGMENT_MS = 500
MAX_SCAN_FRAMES = 7_200
MAX_SEGMENTS = 6_000
MIN_WORKSPACE_FREE_BYTES = 512 * 1024 * 1024
FFMPEG_TIMEOUT_SECONDS = 120
MAX_SCAN_TIMEOUT_SECONDS = 60 * 60
PROBE_TIMEOUT_SECONDS = 20
ALLOWED_VIDEO_DEMUXERS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}


class MediaCapabilityError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MediaCancelled(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seconds_for_ffmpeg(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_rotation(stream: dict[str, object]) -> int:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    rotation = _finite_float(tags.get("rotate")) if isinstance(tags, dict) else None
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and _finite_float(item.get("rotation")) is not None:
                rotation = _finite_float(item.get("rotation"))
                break
    if rotation is None:
        return 0
    normalized = int(round(rotation)) % 360
    return normalized if normalized in {0, 90, 180, 270} else 0


def _stream_sort_key(stream: dict[str, object]) -> tuple[int, int]:
    disposition = stream.get("disposition")
    is_default = isinstance(disposition, dict) and disposition.get("default") == 1
    return (0 if is_default else 1, int(stream.get("index") or 0))


def _streams_of_type(streams: list[object], codec_type: str) -> list[dict[str, object]]:
    return [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type
    ]


def _selected_stream(streams: list[dict[str, object]]) -> dict[str, object] | None:
    return min(streams, key=_stream_sort_key, default=None)


def _duration_ms(format_info: dict[str, object], video_stream: dict[str, object]) -> int:
    duration_seconds = _finite_float(format_info.get("duration"))
    if duration_seconds is None:
        duration_seconds = _finite_float(video_stream.get("duration"))
    if duration_seconds is None or duration_seconds <= 0:
        raise MediaCapabilityError("invalid_duration", "The video duration is missing or invalid.")
    duration = int(round(duration_seconds * 1000))
    if duration > MAX_VIDEO_DURATION_MS:
        raise MediaCapabilityError("media_too_long", "The video exceeds the eight-hour safety limit.")
    return duration


def _dimensions(video_stream: dict[str, object]) -> tuple[int, int]:
    width = video_stream.get("width")
    height = video_stream.get("height")
    if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
        raise MediaCapabilityError("invalid_dimensions", "The video dimensions are invalid.")
    if width > MAX_SOURCE_DIMENSION or height > MAX_SOURCE_DIMENSION or width * height > MAX_SOURCE_PIXELS:
        raise MediaCapabilityError("dimensions_too_large", "Video dimensions exceed local decode limits.")
    return width, height


def _container_name(format_info: dict[str, object]) -> object:
    format_name = format_info.get("format_name")
    demuxers = {
        value.strip().casefold()
        for value in str(format_name or "").split(",")
        if value.strip()
    }
    if not demuxers or not demuxers.issubset(ALLOWED_VIDEO_DEMUXERS):
        raise MediaCapabilityError(
            "unsupported_container",
            "Only native QuickTime/MOV/MP4-family containers are accepted.",
        )
    return format_name


def _codec_manifest(
    format_name: object,
    video_stream: dict[str, object],
    audio_streams: list[dict[str, object]],
    selected_audio: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "format_name": format_name,
        "video_codec": video_stream.get("codec_name"),
        "pixel_format": video_stream.get("pix_fmt"),
        "avg_frame_rate": video_stream.get("avg_frame_rate"),
        "time_base": video_stream.get("time_base"),
        "video_stream_index": video_stream.get("index"),
        "audio_stream_index": selected_audio.get("index") if selected_audio else None,
        "stream_selection": "default_disposition_then_lowest_index",
        "audio_streams": [
            {
                "index": stream.get("index"),
                "codec": stream.get("codec_name"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
            }
            for stream in audio_streams
        ],
    }


def parse_ffprobe_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise MediaCapabilityError("invalid_media", "ffprobe returned an invalid JSON object.")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        streams = []
    format_info = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    video_stream = _selected_stream(_streams_of_type(streams, "video"))
    if video_stream is None:
        raise MediaCapabilityError("missing_video_stream", "The file has no supported video stream.")
    duration_ms = _duration_ms(format_info, video_stream)
    width, height = _dimensions(video_stream)
    format_name = _container_name(format_info)
    audio_streams = _streams_of_type(streams, "audio")
    selected_audio = _selected_stream(audio_streams)
    tags = format_info.get("tags") if isinstance(format_info.get("tags"), dict) else {}
    captured_at = tags.get("creation_time") if isinstance(tags, dict) else None
    return {
        "duration_ms": duration_ms,
        "width": width,
        "height": height,
        "rotation_degrees": _parse_rotation(video_stream),
        "captured_at": captured_at if isinstance(captured_at, str) else None,
        "codec": _codec_manifest(format_name, video_stream, audio_streams, selected_audio),
    }


def resolve_binary(name: str) -> str | None:
    path = shutil.which(name)
    return str(Path(path).resolve()) if path else None


def binary_capability(name: str) -> dict[str, object]:
    path = resolve_binary(name)
    if path is None:
        return {"available": False, "version": None, "supported": False}
    try:
        completed = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "version": None, "supported": False}
    first_line = (completed.stdout or completed.stderr or "").splitlines()
    version = first_line[0][:240] if first_line else None
    major_match = re.search(r"\bversion\s+(\d+)", version or "", re.IGNORECASE)
    supported = bool(major_match and int(major_match.group(1)) >= 6)
    return {
        "available": completed.returncode == 0 and supported,
        "version": version,
        "supported": supported,
    }


def ffprobe(path: Path, binary: str | None = None) -> dict[str, object]:
    executable = binary or resolve_binary("ffprobe")
    if executable is None:
        raise MediaCapabilityError("ffprobe_missing", "ffprobe 6+ is required for video indexing.")
    if not binary_capability("ffprobe")["available"]:
        raise MediaCapabilityError("ffprobe_unsupported", "ffprobe 6+ is required for video indexing.")
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe,fd",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "-f",
                "mov",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                str(path),
            ],
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaCapabilityError("ffprobe_timeout", "ffprobe timed out.") from exc
    except OSError as exc:
        raise MediaCapabilityError("ffprobe_failed", "ffprobe could not be started.") from exc
    if completed.returncode != 0:
        raise MediaCapabilityError("invalid_media", "ffprobe rejected the video file.")
    if len(completed.stdout) > 8 * 1024 * 1024:
        raise MediaCapabilityError("probe_output_too_large", "ffprobe output exceeded the safety limit.")
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaCapabilityError("invalid_probe_json", "ffprobe returned invalid JSON.") from exc
    return parse_ffprobe_payload(payload)


def resolve_inside_root(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Relative path must be a non-empty string.")
    if "\x00" in relative_path or Path(relative_path).is_absolute():
        raise ValueError("Only relative paths inside an approved root are allowed.")
    canonical_root = root.expanduser().resolve(strict=True)
    relative = Path(relative_path)
    candidate_unresolved = canonical_root / relative
    current = canonical_root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("Media path escapes the approved library root.")
        current = current / part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError("Symbolic links are not accepted as media sources.")
    candidate = candidate_unresolved.resolve(strict=True)
    try:
        candidate.relative_to(canonical_root)
    except ValueError as exc:
        raise ValueError("Media path escapes the approved library root.") from exc
    if not candidate.is_file():
        raise ValueError("Media path must resolve to a regular file.")
    return candidate


def discover_media(
    root: Path,
    *,
    recursive: bool,
    files: Sequence[str] | None = None,
    extensions: set[str] | frozenset[str] | None = None,
    max_files: int | None = None,
    max_total_bytes: int | None = None,
    deadline: float | None = None,
) -> list[Path]:
    canonical_root = root.expanduser().resolve(strict=True)
    if not canonical_root.is_dir():
        raise ValueError("Library root must be an existing directory.")
    if files:
        candidates: Iterable[Path] = (resolve_inside_root(canonical_root, value) for value in files)
    else:
        pattern = "**/*" if recursive else "*"
        candidates = canonical_root.glob(pattern)
    supported = set(extensions) if extensions is not None else IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    if not supported or not supported.issubset(IMAGE_EXTENSIONS | VIDEO_EXTENSIONS):
        raise ValueError("Media discovery extensions are invalid.")
    resolved: list[Path] = []
    total_bytes = 0
    for candidate in candidates:
        if deadline is not None and time.monotonic() > deadline:
            raise ValueError("import_manifest_timeout: media discovery exceeded its time budget.")
        # Filter by name before touching metadata. In video-only mode, a large photo
        # library does not consume the video manifest count or byte budget.
        if candidate.suffix.casefold() not in supported:
            continue
        if not candidate.is_file() or candidate.is_symlink():
            continue
        try:
            path = resolve_inside_root(canonical_root, str(candidate.relative_to(canonical_root)))
        except (OSError, ValueError):
            continue
        size = path.stat().st_size
        if size <= MAX_FILE_SIZE_BYTES:
            total_bytes += size
            resolved.append(path)
            if max_files is not None and len(resolved) > max_files:
                raise ValueError("import_manifest_too_large: too many supported files.")
            if max_total_bytes is not None and total_bytes > max_total_bytes:
                raise ValueError("import_manifest_too_large: supported files exceed the byte budget.")
    return sorted(resolved)


def _timestamp_ms(value: str) -> int:
    cleaned = value.strip().replace(",", ".")
    parts = cleaned.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        raise ValueError("Invalid subtitle timestamp.")
    return int(round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000))


def parse_sidecar_subtitles(path: Path, asset_id: str, revision: int) -> list[dict[str, object]]:
    sidecar = next(
        (
            path.with_suffix(extension)
            for extension in SIDE_CAR_EXTENSIONS
            if path.with_suffix(extension).is_file() and not path.with_suffix(extension).is_symlink()
        ),
        None,
    )
    if sidecar is None:
        return []
    if sidecar.stat().st_size > 8 * 1024 * 1024:
        raise MediaCapabilityError("subtitle_too_large", "Sidecar subtitles exceed the 8 MiB safety limit.")
    try:
        raw = sidecar.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raw = sidecar.read_text(encoding="utf-8", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if raw.lstrip().startswith("WEBVTT"):
        raw = raw.lstrip()[len("WEBVTT") :].lstrip("\n")
    blocks = re.split(r"\n{2,}", raw.strip())
    parsed: list[dict[str, object]] = []
    time_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})"
    )
    for block in blocks[:100_000]:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if time_pattern.search(line)), None)
        if time_index is None:
            continue
        match = time_pattern.search(lines[time_index])
        assert match is not None
        try:
            start_ms = _timestamp_ms(match.group("start"))
            end_ms = _timestamp_ms(match.group("end"))
        except (ValueError, OverflowError):
            continue
        text = re.sub(r"<[^>]+>", "", " ".join(lines[time_index + 1 :])).strip()
        if not text or end_ms <= start_ms:
            continue
        parsed.append(
            {
                "id": new_id("tr"),
                "asset_id": asset_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text[:4000],
                "source": "sidecar_subtitle",
                "provider": "local_sidecar",
                "model": None,
                "analysis_revision": revision,
            }
        )
    return parsed


def subtitle_text_for_range(transcripts: Sequence[dict[str, object]], start_ms: int, end_ms: int) -> str:
    return " ".join(
        str(item.get("text") or "").strip()
        for item in transcripts
        if int(item.get("end_ms") or 0) > start_ms and int(item.get("start_ms") or 0) < end_ms
    ).strip()


def _start_process(argv: list[str], **kwargs) -> subprocess.Popen:
    if not argv or any(not isinstance(value, str) for value in argv):
        raise ValueError("Subprocess arguments must be a non-empty string array.")
    process_kwargs = dict(kwargs)
    if os.name == "posix":
        process_kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **process_kwargs)


def terminate_process(process: subprocess.Popen, grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass


def _image_signal(path: Path) -> tuple[float, float, np.ndarray]:
    with Image.open(path) as image:
        gray = image.convert("L")
        brightness = float(ImageStat.Stat(gray).mean[0]) / 255.0
        small = gray.resize((32, 18), Image.Resampling.BILINEAR)
        array = np.asarray(small, dtype=np.float32) / 255.0
    sharpness = float(np.var(np.diff(array, axis=0)) + np.var(np.diff(array, axis=1)))
    hist, _ = np.histogram(array, bins=16, range=(0, 1), density=True)
    hist = hist.astype(np.float32)
    hist /= max(float(np.linalg.norm(hist)), 1e-8)
    return brightness, sharpness, hist


@dataclass
class ScanFrame:
    timestamp_ms: int
    path: Path
    brightness: float
    sharpness: float
    histogram: np.ndarray
    novelty: float = 0.0


def scan_video_frames(
    *,
    video_path: Path,
    workspace: Path,
    duration_ms: int,
    ffmpeg_binary: str,
    cancel_requested: Callable[[], bool],
) -> list[ScanFrame]:
    scan_dir = workspace / "scan"
    scan_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(workspace).free < MIN_WORKSPACE_FREE_BYTES:
        raise MediaCapabilityError("insufficient_storage", "At least 512 MiB of workspace is required.")
    output_pattern = scan_dir / "%08d.jpg"
    sample_fps = min(4.0, MAX_SCAN_FRAMES / max(duration_ms / 1000, 1.0))
    sample_fps = max(sample_fps, 0.01)
    argv = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "file,pipe,fd",
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
        "-i",
        str(video_path),
        "-t",
        _seconds_for_ffmpeg(duration_ms),
        "-vf",
        f"fps={sample_fps:.8f},scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'",
        "-q:v",
        "5",
        "-frames:v",
        str(MAX_SCAN_FRAMES),
        str(output_pattern),
    ]
    process = _start_process(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + MAX_SCAN_TIMEOUT_SECONDS
    while process.poll() is None:
        if cancel_requested():
            terminate_process(process)
            raise MediaCancelled("Video indexing was cancelled.")
        if time.monotonic() > deadline:
            terminate_process(process)
            raise MediaCapabilityError("ffmpeg_timeout", "Frame scanning timed out.")
        if shutil.disk_usage(workspace).free < 128 * 1024 * 1024:
            terminate_process(process)
            raise MediaCapabilityError("insufficient_storage", "Workspace free space fell below 128 MiB.")
        time.sleep(0.1)
    if process.returncode != 0:
        raise MediaCapabilityError("ffmpeg_scan_failed", "FFmpeg could not decode this video locally.")

    paths = sorted(scan_dir.glob("*.jpg"))
    if not paths:
        raise MediaCapabilityError("no_decoded_frames", "FFmpeg decoded no frames from the video.")
    frame_interval = max(1, round(1000 / sample_fps))
    frames: list[ScanFrame] = []
    previous_histogram: np.ndarray | None = None
    for index, path in enumerate(paths):
        brightness, sharpness, histogram = _image_signal(path)
        novelty = 0.0 if previous_histogram is None else float(1.0 - np.dot(previous_histogram, histogram))
        frames.append(
            ScanFrame(
                timestamp_ms=min(duration_ms - 1, index * frame_interval),
                path=path,
                brightness=brightness,
                sharpness=sharpness,
                histogram=histogram,
                novelty=max(0.0, novelty),
            )
        )
        previous_histogram = histogram
    return frames


def segment_boundaries(frames: Sequence[ScanFrame], duration_ms: int) -> list[tuple[int, int, str]]:
    candidates = [0]
    for frame in frames[1:]:
        if frame.novelty >= 0.22:
            candidates.append(frame.timestamp_ms)
    fallback_interval = max(
        DEFAULT_FALLBACK_INTERVAL_MS,
        math.ceil(duration_ms / MAX_SEGMENTS),
    )
    for timestamp in range(fallback_interval, duration_ms, fallback_interval):
        candidates.append(timestamp)
    candidates = sorted(set(max(0, min(value, duration_ms)) for value in candidates))
    # Boundaries are an internal plan and must fit the same limit for every
    # otherwise supported duration. Thin deterministically while retaining both
    # endpoints instead of rejecting a valid high-motion video.
    if len(candidates) > MAX_SEGMENTS:
        final_index = len(candidates) - 1
        candidates = [
            candidates[round(index * final_index / (MAX_SEGMENTS - 1))]
            for index in range(MAX_SEGMENTS)
        ]
        candidates = sorted(set(candidates))
    bounded = [0]
    for candidate in candidates[1:]:
        while candidate - bounded[-1] > DEFAULT_MAX_SEGMENT_MS:
            bounded.append(bounded[-1] + DEFAULT_MAX_SEGMENT_MS)
        if candidate - bounded[-1] >= DEFAULT_MIN_SEGMENT_MS:
            bounded.append(candidate)
    while duration_ms - bounded[-1] > DEFAULT_MAX_SEGMENT_MS:
        bounded.append(bounded[-1] + DEFAULT_MAX_SEGMENT_MS)
    if bounded[-1] != duration_ms:
        bounded.append(duration_ms)
    if len(bounded) == 1:
        bounded.append(duration_ms)
    adaptive_points = {round(frame.timestamp_ms / 250) for frame in frames if frame.novelty >= 0.22}
    result = []
    for index in range(len(bounded) - 1):
        start, end = bounded[index], bounded[index + 1]
        if end <= start:
            continue
        near_adaptive = any(round(start / 250) + offset in adaptive_points for offset in (-1, 0, 1))
        result.append((start, end, "adaptive_scene" if near_adaptive else "fallback_interval"))
    if len(result) > MAX_SEGMENTS:
        stride = len(result) / MAX_SEGMENTS
        result = [result[min(len(result) - 1, int(index * stride))] for index in range(MAX_SEGMENTS)]
    return result


def representative_frame(frames: Sequence[ScanFrame], start_ms: int, end_ms: int) -> ScanFrame:
    eligible = [frame for frame in frames if start_ms <= frame.timestamp_ms < end_ms]
    if not eligible:
        eligible = list(frames)
    midpoint = (start_ms + end_ms) / 2
    return max(
        eligible,
        key=lambda frame: (
            0.55 * frame.sharpness
            + 0.25 * frame.novelty
            + 0.2 * (1.0 - min(abs(frame.brightness - 0.5) * 2, 1.0))
            - 0.15 * min(abs(frame.timestamp_ms - midpoint) / max(end_ms - start_ms, 1), 1.0)
        ),
    )


def representatives_for_boundaries(
    frames: Sequence[ScanFrame], boundaries: Sequence[tuple[int, int, str]]
) -> list[ScanFrame]:
    """Select representatives in one monotonic pass, avoiding O(segments*frames)."""
    if not frames:
        raise MediaCapabilityError("no_decoded_frames", "FFmpeg decoded no frames from the video.")
    ordered_frames = sorted(frames, key=lambda value: value.timestamp_ms)
    frame_index = 0
    selected: list[ScanFrame] = []
    for start_ms, end_ms, _ in boundaries:
        eligible: list[ScanFrame] = []
        while frame_index < len(ordered_frames) and ordered_frames[frame_index].timestamp_ms < start_ms:
            frame_index += 1
        cursor = frame_index
        while cursor < len(ordered_frames) and ordered_frames[cursor].timestamp_ms < end_ms:
            eligible.append(ordered_frames[cursor])
            cursor += 1
        if eligible:
            selected.append(representative_frame(eligible, start_ms, end_ms))
        else:
            selected.append(ordered_frames[min(frame_index, len(ordered_frames) - 1)])
        frame_index = cursor
    return selected


def _copy_keyframe(frame: ScanFrame, cache_root: Path, segment_id: str) -> dict[str, object]:
    destination_dir = cache_root / "keyframes"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{segment_id}-{frame.timestamp_ms}.jpg"
    shutil.copyfile(frame.path, destination)
    with Image.open(destination) as image:
        width, height = image.size
    return {
        "id": new_id("kf"),
        "segment_id": segment_id,
        "timestamp_ms": frame.timestamp_ms,
        "cache_key": str(destination.relative_to(cache_root)),
        "sha256": sha256_file(destination),
        "width": width,
        "height": height,
        "selection_reason": "Highest local clarity/novelty score near the segment midpoint.",
        "clarity_score": round(frame.sharpness, 6),
        "novelty_score": round(frame.novelty, 6),
        "is_representative": True,
    }


class MediaJobRunner:
    """One bounded local worker for video indexing; state lives in SQLite."""

    def __init__(self, repository: MediaRepository, cache_root: Path):
        self.repository = repository
        self.cache_root = cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memolens-media")
        self._submitted: set[str] = set()
        self._lock = threading.Lock()

    def shutdown(self) -> None:
        with self._lock:
            submitted = list(self._submitted)
        for job_id in submitted:
            self.repository.request_media_job_cancel(job_id)
        self._executor.shutdown(wait=True, cancel_futures=False)

    def submit(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._submitted:
                return
            self._submitted.add(job_id)
        self._executor.submit(self._run_and_release, job_id)

    def _run_and_release(self, job_id: str) -> None:
        try:
            self._run(job_id)
        finally:
            with self._lock:
                self._submitted.discard(job_id)

    def cancel(self, job_id: str) -> bool:
        return self.repository.request_media_job_cancel(job_id)

    def resume(self, job_id: str) -> bool:
        if not self.repository.reset_media_job_for_resume(job_id):
            return False
        self.submit(job_id)
        return True

    def _cancel_requested(self, job_id: str) -> bool:
        job = self.repository.get_media_job(job_id)
        return job is None or bool(job.get("cancel_requested"))

    def _run(self, job_id: str) -> None:
        job = self.repository.get_media_job(job_id)
        if (
            job is None
            or job.get("kind") != "video_index"
            or job.get("status") not in {"queued", "interrupted"}
            or job.get("cancel_requested")
        ):
            return
        asset_id = str(job.get("asset_id") or "")
        asset = self.repository.get_asset(asset_id)
        if asset is None:
            self.repository.update_media_job(
                job_id,
                status="failed",
                stage="failed",
                error={"code": "asset_missing", "message": "Asset does not exist."},
                finished=True,
            )
            return
        workspace: Path | None = None
        pending_keyframes: list[Path] = []
        committed = False
        try:
            source = resolve_inside_root(Path(str(asset["root_path"])), str(asset["relative_path"]))
            if sha256_file(source) != asset["sha256"]:
                raise MediaCapabilityError("source_changed", "Video bytes changed after import; import it again.")
            self.repository.update_media_job(job_id, status="running", stage="probe", progress=0.05)
            probe = ffprobe(source)
            self.repository.update_asset_probe(asset_id, probe)
            if self._cancel_requested(job_id):
                raise MediaCancelled("Video indexing was cancelled.")

            self.repository.update_media_job(job_id, stage="scan", progress=0.12, checkpoint={"probe": probe})
            ffmpeg_binary = resolve_binary("ffmpeg")
            if ffmpeg_binary is None or not binary_capability("ffmpeg")["available"]:
                raise MediaCapabilityError("ffmpeg_missing", "FFmpeg 6+ is required for video indexing.")
            jobs_root = self.cache_root / "jobs"
            jobs_root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=jobs_root))
            frames = scan_video_frames(
                video_path=source,
                workspace=workspace,
                duration_ms=int(probe["duration_ms"]),
                ffmpeg_binary=ffmpeg_binary,
                cancel_requested=lambda: self._cancel_requested(job_id),
            )
            revision = int(job.get("analysis_revision") or 1)
            boundaries = segment_boundaries(frames, int(probe["duration_ms"]))
            representatives = representatives_for_boundaries(frames, boundaries)
            transcripts = parse_sidecar_subtitles(source, asset_id, revision)
            segments: list[dict[str, object]] = []
            keyframes: list[dict[str, object]] = []
            self.repository.update_media_job(job_id, stage="segment", progress=0.65)
            for ordinal, (start_ms, end_ms, reason) in enumerate(boundaries):
                if self._cancel_requested(job_id):
                    raise MediaCancelled("Video indexing was cancelled.")
                segment_id = f"seg_{asset_id.removeprefix('asset_')}_{revision}_{ordinal}"
                frame = representatives[ordinal]
                keyframe = _copy_keyframe(frame, self.cache_root, segment_id)
                keyframes.append(keyframe)
                pending_keyframes.append(self.cache_root / str(keyframe["cache_key"]))
                transcript = subtitle_text_for_range(transcripts, start_ms, end_ms)
                time_label = f"{start_ms / 1000:.1f}-{end_ms / 1000:.1f}s"
                summary = f"Local video segment from {asset['filename']} at {time_label}."
                if transcript:
                    summary += f" Subtitle: {transcript[:300]}"
                semantic = {
                    "tags": ["video", "local media", "segment"],
                    "analysis_profile": "adaptive-local-v1",
                    "external_analysis": False,
                    "has_subtitle": bool(transcript),
                }
                segments.append(
                    {
                        "id": segment_id,
                        "ordinal": ordinal,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "boundary_reason": reason,
                        "summary": summary,
                        "semantic": semantic,
                        "visible_text": None,
                        "combined_text": " ".join(
                            [str(asset["filename"]), summary, transcript, "video local media segment"]
                        ).strip(),
                        "visual_status": "local_fallback",
                        "transcript_status": "available" if transcript else "unavailable",
                        # Local technical fallback has no calibrated semantic score.
                        "confidence": None,
                    }
                )
            self.repository.update_media_job(job_id, stage="commit", progress=0.9)
            self.repository.commit_video_analysis(
                job_id=job_id,
                segments=segments,
                keyframes=keyframes,
                transcripts=transcripts,
            )
            committed = True
        except MediaCancelled as exc:
            self.repository.update_media_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                error={"code": "cancelled", "message": str(exc)},
                finished=True,
            )
        except MediaCapabilityError as exc:
            self.repository.mark_asset_failed(asset_id, exc.code)
            self.repository.update_media_job(
                job_id, status="failed", stage="failed", error={"code": exc.code, "message": str(exc)}, finished=True
            )
        except RuntimeError as exc:
            current_job = self.repository.get_media_job(job_id)
            if (
                str(exc) == "analysis_commit_rejected"
                and current_job
                and (current_job.get("cancel_requested") or current_job.get("status") in {"cancelling", "cancelled"})
            ):
                self.repository.update_media_job(
                    job_id,
                    status="cancelled",
                    stage="cancelled",
                    error={"code": "cancelled", "message": "Video indexing was cancelled before commit."},
                    finished=True,
                )
            else:
                self.repository.update_media_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    error={"code": "analysis_commit_rejected", "message": "Video analysis commit was rejected."},
                    finished=True,
                )
        except Exception:
            self.repository.mark_asset_failed(asset_id, "video_index_failed")
            self.repository.update_media_job(
                job_id,
                status="failed",
                stage="failed",
                error={"code": "video_index_failed", "message": "Video indexing failed locally."},
                finished=True,
            )
        finally:
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)
            if not committed:
                for path in pending_keyframes:
                    path.unlink(missing_ok=True)


def mime_type_for(path: Path, kind: str) -> str:
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed:
        return guessed
    return "video/mp4" if kind == "video" else "application/octet-stream"
