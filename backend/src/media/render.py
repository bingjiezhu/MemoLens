from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

from core.media_db import MediaRepository, sha256_path

from .timeline import TimelineService, clips_in_render_order
from .video import (
    MediaCancelled,
    MediaCapabilityError,
    binary_capability,
    ffprobe,
    resolve_binary,
    resolve_inside_root,
    terminate_process,
)


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.3f}"


def _even(value: int) -> int:
    return max(2, value if value % 2 == 0 else value - 1)


def _dimensions(fmt: dict[str, object], profile: str) -> tuple[int, int]:
    width, height = int(fmt["width"]), int(fmt["height"])
    if profile == "preview-low":
        target_width, target_height = (1280, 720) if width > height else (720, 1280) if height > width else (720, 720)
    else:
        target_width, target_height = 1920, 1920
    scale = min(1.0, target_width / width, target_height / height)
    return _even(round(width * scale)), _even(round(height * scale))


def _rotation_filters(rotation: object) -> list[str]:
    return {
        90: ["transpose=cclock"],
        180: ["hflip", "vflip"],
        270: ["transpose=clock"],
    }.get(rotation, [])


def _redact_command(argv: list[str], replacements: dict[str, str]) -> list[str]:
    redacted: list[str] = []
    for value in argv:
        normalized = value
        for path, label in replacements.items():
            normalized = normalized.replace(path, label)
        redacted.append(normalized)
    if redacted:
        redacted[0] = Path(redacted[0]).name
    return redacted


def _concat_line(path: Path) -> str:
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'\n"


_probe_lock = threading.Lock()
_probe_result: dict[str, object] | None = None
MIN_RENDER_FREE_BYTES = 256 * 1024 * 1024
ESTIMATED_RENDER_BYTES_PER_SECOND_720P = 3 * 1024 * 1024


def ffmpeg_encode_capability() -> dict[str, object]:
    """Run the required real H.264/AAC encode/decode smoke probe once per process."""
    global _probe_result
    with _probe_lock:
        if _probe_result is not None:
            return dict(_probe_result)
        ffmpeg_info = binary_capability("ffmpeg")
        ffprobe_info = binary_capability("ffprobe")
        if not ffmpeg_info.get("available") or not ffprobe_info.get("available"):
            _probe_result = {
                "available": False,
                "code": "ffmpeg_unsupported",
                "message": "FFmpeg and ffprobe 6+ are required.",
            }
            return dict(_probe_result)
        executable = resolve_binary("ffmpeg")
        assert executable
        with tempfile.TemporaryDirectory(prefix="memolens-codec-probe-") as directory:
            artifact = Path(directory) / "probe.mp4"
            argv = [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo:d=1",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-t",
                "1",
                str(artifact),
            ]
            completed = subprocess.run(argv, capture_output=True, timeout=20, check=False)
            if completed.returncode != 0:
                _probe_result = {
                    "available": False,
                    "code": "encoder_unavailable",
                    "message": "The required libx264/AAC software encoder probe failed.",
                }
            else:
                try:
                    payload = ffprobe(artifact)
                    _probe_result = {
                        "available": True,
                        "code": None,
                        "duration_ms": payload["duration_ms"],
                        "profiles": ["preview-low"],
                        "verified_preview_encode_decode": True,
                    }
                except MediaCapabilityError:
                    _probe_result = {
                        "available": False,
                        "code": "decoder_unavailable",
                        "message": "The encoded smoke artifact could not be decoded.",
                    }
        return dict(_probe_result)


class RenderJobRunner:
    """Single bounded deterministic renderer for app-managed preview artifacts."""

    def __init__(self, repository: MediaRepository, cache_root: Path):
        self.repository = repository
        self.cache_root = cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memolens-render")
        self._submitted: set[str] = set()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        self._reconcile_interrupted_storage()

    def _reconcile_interrupted_storage(self) -> None:
        """Remove only DB-owned leftovers for jobs that cannot be successful."""
        jobs = self.repository.render_storage_records()
        by_id = {str(job["id"]): job for job in jobs}
        for job in jobs:
            if job["status"] == "succeeded":
                continue
            filename = str(job["output_relative_path"])
            if not filename or filename in {".", ".."} or Path(filename).name != filename:
                continue
            try:
                _, _, descriptor = self.repository.open_output_root_fd(str(job["output_root_id"]))
            except ValueError:
                continue
            try:
                metadata = os.stat(filename, dir_fd=descriptor, follow_symlinks=False)
                if metadata.st_mode & 0o170000 == 0o100000:
                    os.unlink(filename, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            finally:
                os.close(descriptor)
        jobs_root = self.cache_root / "render-jobs"
        if not jobs_root.is_dir() or jobs_root.is_symlink():
            return
        for entry in jobs_root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            job_id = next((identifier for identifier in by_id if entry.name.startswith(f"{identifier}-")), None)
            if job_id is not None:
                shutil.rmtree(entry, ignore_errors=True)

    def shutdown(self) -> None:
        with self._lock:
            submitted = list(self._submitted)
            processes = list(self._processes.values())
        for job_id in submitted:
            self.repository.request_render_cancel(job_id)
        for process in processes:
            terminate_process(process)
        self._executor.shutdown(wait=True, cancel_futures=False)

    def submit(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._submitted:
                return
            self._submitted.add(job_id)
        self._executor.submit(self._run_and_release, job_id)

    def cancel(self, job_id: str) -> bool:
        accepted = self.repository.request_render_cancel(job_id)
        with self._lock:
            process = self._processes.get(job_id)
        if process is not None:
            terminate_process(process)
        return accepted

    def _run_and_release(self, job_id: str) -> None:
        try:
            self._run(job_id)
        finally:
            with self._lock:
                self._submitted.discard(job_id)
                self._processes.pop(job_id, None)

    def _cancelled(self, job_id: str) -> bool:
        job = self.repository.get_render_job(job_id)
        return job is None or bool(job.get("cancel_requested"))

    def _command(
        self,
        job_id: str,
        argv: list[str],
        *,
        replacements: dict[str, str],
        timeout_seconds: float,
        storage_path: Path,
    ) -> bytes:
        if self._cancelled(job_id):
            raise MediaCancelled("Render was cancelled.")
        process = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        with self._lock:
            self._processes[job_id] = process
        deadline = time.monotonic() + max(20.0, timeout_seconds)
        next_storage_check = 0.0
        while process.poll() is None:
            if self._cancelled(job_id):
                terminate_process(process)
                raise MediaCancelled("Render was cancelled.")
            if time.monotonic() > deadline:
                terminate_process(process)
                raise MediaCapabilityError("ffmpeg_timeout", "FFmpeg exceeded the bounded render timeout.")
            if time.monotonic() >= next_storage_check:
                next_storage_check = time.monotonic() + 2.0
                if shutil.disk_usage(storage_path).free < MIN_RENDER_FREE_BYTES:
                    terminate_process(process)
                    raise MediaCapabilityError(
                        "insufficient_storage",
                        "Render stopped before exhausting the app-managed cache volume.",
                    )
            time.sleep(0.1)
        self.repository.update_render_job(
            job_id,
            command=_redact_command(argv, replacements),
            stderr_tail="",
        )
        if process.returncode != 0:
            if self._cancelled(job_id):
                raise MediaCancelled("Render was cancelled.")
            raise MediaCapabilityError("ffmpeg_failed", "FFmpeg could not render the validated timeline.")
        return b""

    def _run(self, job_id: str) -> None:
        job = self.repository.get_render_job(job_id)
        if not job or job.get("status") not in {"queued", "interrupted"}:
            return
        timeline_row = self.repository.get_timeline(str(job["timeline_id"]), int(job["timeline_revision"]))
        if not timeline_row or timeline_row["content_sha256"] != job["timeline_content_sha256"]:
            self.repository.update_render_job(
                job_id,
                status="failed",
                stage="failed",
                error={"code": "timeline_hash_mismatch", "message": "Timeline revision changed."},
                finished=True,
            )
            return
        validation = TimelineService(self.repository).validate(timeline_row["timeline"])
        if not validation["valid"]:
            self.repository.update_render_job(
                job_id,
                status="failed",
                stage="failed",
                error={
                    "code": "invalid_timeline",
                    "message": "Saved timeline is no longer renderable.",
                    "details": validation["errors"],
                },
                finished=True,
            )
            return
        workspace: Path | None = None
        temporary: Path | None = None
        try:
            capability = ffmpeg_encode_capability()
            if not capability.get("available"):
                raise MediaCapabilityError(str(capability.get("code")), str(capability.get("message")))
            ffmpeg_binary = resolve_binary("ffmpeg")
            assert ffmpeg_binary
            try:
                root, output_root = self.repository.validate_output_root(str(job["output_root_id"]))
            except ValueError as exc:
                raise MediaCapabilityError("output_root_changed", "The app preview root identity changed.") from exc
            if root["kind"] != "app_preview":
                raise MediaCapabilityError("output_root_unavailable", "Preview requires the app output root.")
            filename = str(job["output_relative_path"])
            if not filename or filename in {".", ".."} or any(value in filename for value in ("/", "\\", "\x00")):
                raise MediaCapabilityError("invalid_output_name", "Output must be one safe filename.")
            final_output = output_root / filename
            if final_output.exists() or final_output.is_symlink():
                raise MediaCapabilityError("output_exists", "The app artifact filename already exists.")
            jobs_root = self.cache_root / "render-jobs"
            jobs_root.mkdir(parents=True, exist_ok=True)
            fmt = timeline_row["timeline"]["format"]
            width, height = _dimensions(fmt, str(job["profile"]))
            fps = int(fmt["fps"])
            background_color = "0x" + str(fmt["background_color"]).removeprefix("#")
            duration_seconds = int(fmt["duration_ms"]) / 1000
            pixel_ratio = max(0.25, width * height / (1280 * 720))
            estimated_bytes = int(
                duration_seconds * ESTIMATED_RENDER_BYTES_PER_SECOND_720P * pixel_ratio * 2
            )
            required_free = MIN_RENDER_FREE_BYTES + estimated_bytes
            if shutil.disk_usage(jobs_root).free < required_free:
                raise MediaCapabilityError(
                    "insufficient_storage",
                    "The app-managed cache does not have enough free space for this render.",
                )
            workspace = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=jobs_root))
            clips = list(clips_in_render_order(timeline_row["timeline"]))
            if timeline_row["timeline"].get("transitions"):
                raise MediaCapabilityError(
                    "unsupported_render_transition",
                    "This renderer does not yet support transitions; remove them before rendering.",
                )
            normalized: list[Path] = []
            replacements = {str(workspace): "$JOB_DIR"}
            verified_sources: dict[tuple[str, str], tuple[Path, tuple[int, int, int, int]]] = {}
            self.repository.update_render_job(job_id, status="running", stage="verify_sources", progress=0.02)
            for index, clip in enumerate(clips):
                source = self.repository.get_asset_source(str(clip["asset_source_id"]))
                if not source or source.get("availability") != "available":
                    raise MediaCapabilityError("source_unavailable", "A timeline source is unavailable.")
                source_path = resolve_inside_root(Path(str(source["root_path"])), str(source["relative_path"]))
                replacements[str(source_path)] = f"$SOURCE_{source['asset_id']}"
                metadata = source_path.stat(follow_symlinks=False)
                identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
                verification_key = (str(source["id"]), str(source["sha256"]))
                prior_verification = verified_sources.get(verification_key)
                if prior_verification is not None and prior_verification != (source_path, identity):
                    self.repository.mark_source_availability(str(source["id"]), "changed")
                    raise MediaCapabilityError("source_changed", "A timeline source changed during rendering.")
                if prior_verification is None and sha256_path(source_path) != source["sha256"]:
                    self.repository.mark_source_availability(str(source["id"]), "changed")
                    raise MediaCapabilityError("source_changed", "A timeline source changed after import.")
                verified_sources[verification_key] = (source_path, identity)
                duration_ms = int(clip["timeline_duration_ms"])
                output = workspace / f"clip-{index:04d}.mp4"
                source_codec = source.get("codec", {})
                selected_video_index = source_codec.get("video_stream_index")
                selected_audio_index = source_codec.get("audio_stream_index")
                has_audio = selected_audio_index is not None and bool(clip.get("audio_enabled", True))
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
                if clip["kind"] == "video":
                    argv += [
                        "-noautorotate",
                        "-ss",
                        _seconds(int(clip["source_in_ms"])),
                        "-t",
                        _seconds(duration_ms),
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
                    # Freeze a verified still image to a normalized first-frame PNG.
                    # This gives GIF/HEIC/TIFF and ordinary photos one deterministic
                    # render contract instead of depending on demuxer-specific loop flags.
                    frozen = workspace / f"source-{index:04d}.png"
                    with Image.open(source_path) as image:
                        try:
                            image.seek(0)
                        except EOFError:
                            pass
                        ImageOps.exif_transpose(image).convert("RGB").save(frozen, "PNG")
                    argv += [
                        "-loop",
                        "1",
                        "-framerate",
                        str(fps),
                        "-t",
                        _seconds(duration_ms),
                        "-i",
                        str(frozen),
                    ]
                if not has_audio:
                    argv += ["-f", "lavfi", "-t", _seconds(duration_ms), "-i", "anullsrc=r=48000:cl=stereo"]
                filters = _rotation_filters(source.get("rotation_degrees"))
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
                        f"scale={width}:{height}:force_original_aspect_ratio=increase",
                        f"crop={width}:{height}",
                    ]
                elif fit == "contain":
                    filters += [
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background_color}",
                    ]
                elif fit == "stretch":
                    filters.append(f"scale={width}:{height}")
                else:
                    raise MediaCapabilityError("unsupported_fit", "Clip fit is unsupported.")
                filters += [f"fps={fps}", "setsar=1", "format=yuv420p"]
                argv += [
                    "-map",
                    f"0:{int(selected_video_index)}" if clip["kind"] == "video" else "0:v:0",
                    "-map",
                    f"0:{int(selected_audio_index)}" if has_audio else "1:a:0",
                    "-vf",
                    ",".join(filters),
                ]
                if has_audio:
                    argv += [
                        "-af",
                        f"volume={float(clip.get('volume_db', 0.0))}dB,"
                        f"aresample=48000,apad,atrim=0:{_seconds(duration_ms)}",
                    ]
                argv += [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast" if job["profile"] == "preview-low" else "medium",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k" if job["profile"] == "preview-low" else "192k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-movflags",
                    "+faststart",
                    "-t",
                    _seconds(duration_ms),
                    str(output),
                ]
                self.repository.update_render_job(
                    job_id, stage="normalize_clips", progress=0.05 + 0.7 * index / max(len(clips), 1)
                )
                self._command(
                    job_id,
                    argv,
                    replacements=replacements,
                    timeout_seconds=max(60, duration_ms / 1000 * 8),
                    storage_path=workspace,
                )
                normalized.append(output)
            concat_file = workspace / "concat.txt"
            concat_file.write_text(
                "".join(_concat_line(path) for path in normalized),
                encoding="utf-8",
            )
            replacements[str(concat_file)] = "$JOB_DIR/concat.txt"
            temporary = workspace / "assembled.part.mp4"
            replacements[str(temporary)] = "$OUTPUT_PART"
            argv = [
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
                str(temporary),
            ]
            self.repository.update_render_job(job_id, stage="assemble", progress=0.8)
            self._command(
                job_id,
                argv,
                replacements=replacements,
                timeout_seconds=max(60, int(fmt["duration_ms"]) / 1000 * 4),
                storage_path=workspace,
            )
            probe = ffprobe(temporary)
            expected_duration_ms = int(fmt["duration_ms"])
            frame_tolerance_ms = max(50, int(2000 / max(fps, 1)))
            if abs(int(probe["duration_ms"]) - expected_duration_ms) > frame_tolerance_ms:
                raise MediaCapabilityError(
                    "render_duration_mismatch",
                    "Rendered duration differs from the validated timeline.",
                )
            if self._cancelled(job_id):
                raise MediaCancelled("Render was cancelled before publication.")
            # Revalidate immediately before publication, then atomically link without overwrite.
            _, verified_root, directory_fd = self.repository.open_output_root_fd(str(job["output_root_id"]))
            output_sha256 = sha256_path(temporary)
            output_size = temporary.stat().st_size
            linked = False
            committed = False
            try:
                if self._cancelled(job_id):
                    raise MediaCancelled("Render was cancelled before publication.")
                os.link(temporary, filename, dst_dir_fd=directory_fd, follow_symlinks=False)
                linked = True
            except FileExistsError as exc:
                raise MediaCapabilityError("output_exists", "The app artifact filename already exists.") from exc
            try:
                version = str(binary_capability("ffmpeg").get("version") or "")[:240]
                if not self.repository.complete_render_job_success(
                    job_id,
                    ffmpeg_version=version,
                    output_sha256=output_sha256,
                    size_bytes=output_size,
                    duration_ms=int(probe["duration_ms"]),
                ):
                    if linked:
                        os.unlink(filename, dir_fd=directory_fd)
                        linked = False
                    raise MediaCancelled("Render was cancelled during publication.")
                committed = True
            except BaseException:
                if linked and not committed:
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                raise
            finally:
                try:
                    os.close(directory_fd)
                except OSError:
                    if not committed:
                        raise
            # Publication is committed. Cleanup failures must never downgrade a
            # verified succeeded job; startup reconciliation removes the workspace.
            try:
                temporary.unlink()
            except OSError:
                pass
            temporary = None
        except MediaCancelled as exc:
            self.repository.update_render_job(
                job_id,
                status="cancelled",
                stage="cancelled",
                error={"code": "cancelled", "message": str(exc)},
                finished=True,
            )
        except MediaCapabilityError as exc:
            self.repository.update_render_job(
                job_id,
                status="failed",
                stage="failed",
                error={"code": exc.code, "message": str(exc)},
                finished=True,
            )
        except Exception:
            self.repository.update_render_job(
                job_id,
                status="failed",
                stage="failed",
                error={"code": "render_failed", "message": "The local render failed."},
                finished=True,
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)
