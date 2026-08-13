from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.media_db import MediaRepository, sha256_path

from . import render_plan as _render_plan
from .render_plan import (
    RenderPlan,
    build_assemble_command,
    build_clip_command,
    build_render_plan,
    concat_manifest,
    ensure_supported_features,
    validate_render_duration,
)
from .render_publish import (
    OutputTarget,
    publish_render,
    reconcile_interrupted_storage,
    validate_output_target,
)
from .render_sources import SourceVerifier, freeze_still_image
from .timeline import TimelineService
from .video import (
    MediaCancelled,
    MediaCapabilityError,
    binary_capability,
    ffprobe,
    resolve_binary,
    terminate_process,
)

MIN_RENDER_FREE_BYTES = _render_plan.MIN_RENDER_FREE_BYTES
ESTIMATED_RENDER_BYTES_PER_SECOND_720P = _render_plan.ESTIMATED_RENDER_BYTES_PER_SECOND_720P
_seconds = _render_plan.seconds
_even = _render_plan.even
_dimensions = _render_plan.dimensions
_rotation_filters = _render_plan.rotation_filters
_redact_command = _render_plan.redact_command
_concat_line = _render_plan.concat_line

_probe_lock = threading.Lock()
_probe_result: dict[str, object] | None = None


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

    def __init__(
        self,
        repository: MediaRepository,
        cache_root: Path,
        *,
        reconcile_on_start: bool = True,
    ):
        self.repository = repository
        self.cache_root = cache_root.expanduser().resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memolens-render")
        self._submitted: set[str] = set()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        if reconcile_on_start:
            self.reconcile_interrupted_storage()

    def reconcile_interrupted_storage(self) -> None:
        reconcile_interrupted_storage(self.repository, self.cache_root)

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

    def _load_validated_timeline(
        self,
        job_id: str,
        job: dict[str, object],
    ) -> dict[str, object] | None:
        timeline_row = self.repository.get_timeline(str(job["timeline_id"]), int(job["timeline_revision"]))
        if not timeline_row or timeline_row["content_sha256"] != job["timeline_content_sha256"]:
            self.repository.update_render_job(
                job_id,
                status="failed",
                stage="failed",
                error={"code": "timeline_hash_mismatch", "message": "Timeline revision changed."},
                finished=True,
            )
            return None
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
            return None
        return timeline_row["timeline"]

    def _prepare_render(
        self,
        job_id: str,
        job: dict[str, object],
        timeline: dict[str, object],
    ) -> tuple[str, OutputTarget, RenderPlan, Path, Path]:
        capability = ffmpeg_encode_capability()
        if not capability.get("available"):
            raise MediaCapabilityError(str(capability.get("code")), str(capability.get("message")))
        ffmpeg_binary = resolve_binary("ffmpeg")
        assert ffmpeg_binary
        target = validate_output_target(self.repository, job)
        jobs_root = self.cache_root / "render-jobs"
        jobs_root.mkdir(parents=True, exist_ok=True)
        plan = build_render_plan(timeline, str(job["profile"]))
        if shutil.disk_usage(jobs_root).free < plan.required_free_bytes:
            raise MediaCapabilityError(
                "insufficient_storage",
                "The app-managed cache does not have enough free space for this render.",
            )
        workspace = Path(tempfile.mkdtemp(prefix=f"{job_id}-", dir=jobs_root))
        return ffmpeg_binary, target, plan, workspace, workspace / "assembled.part.mp4"

    def _execute_render_plan(
        self,
        job_id: str,
        *,
        ffmpeg_binary: str,
        target: OutputTarget,
        plan: RenderPlan,
        workspace: Path,
        temporary: Path,
    ) -> None:
        ensure_supported_features(plan)
        replacements = {str(workspace): "$JOB_DIR"}
        verifier = SourceVerifier(self.repository, hash_path=sha256_path)
        normalized: list[Path] = []
        self.repository.update_render_job(job_id, status="running", stage="verify_sources", progress=0.02)
        for index, clip in enumerate(plan.clips):
            verified = verifier.verify(clip)
            source = verified.record
            source_path = verified.path
            replacements[str(source_path)] = f"$SOURCE_{source['asset_id']}"
            duration_ms = int(clip["timeline_duration_ms"])
            output = workspace / f"clip-{index:04d}.mp4"
            input_path = source_path
            if clip["kind"] != "video":
                input_path = workspace / f"source-{index:04d}.png"
                freeze_still_image(source_path, input_path)
            argv = build_clip_command(
                plan,
                clip,
                source=source,
                source_path=source_path,
                input_path=input_path,
                output_path=output,
                ffmpeg_binary=ffmpeg_binary,
            )
            self.repository.update_render_job(
                job_id,
                stage="normalize_clips",
                progress=0.05 + 0.7 * index / max(len(plan.clips), 1),
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
        concat_file.write_text(concat_manifest(normalized), encoding="utf-8")
        replacements[str(concat_file)] = "$JOB_DIR/concat.txt"
        replacements[str(temporary)] = "$OUTPUT_PART"
        argv = build_assemble_command(
            concat_file=concat_file,
            output_path=temporary,
            ffmpeg_binary=ffmpeg_binary,
        )
        self.repository.update_render_job(job_id, stage="assemble", progress=0.8)
        self._command(
            job_id,
            argv,
            replacements=replacements,
            timeout_seconds=max(60, plan.duration_ms / 1000 * 4),
            storage_path=workspace,
        )
        probe = ffprobe(temporary)
        validate_render_duration(plan, probe)
        publish_render(
            self.repository,
            job_id=job_id,
            target=target,
            temporary=temporary,
            duration_ms=int(probe["duration_ms"]),
            ffmpeg_version=lambda: str(binary_capability("ffmpeg").get("version") or "")[:240],
            cancelled=lambda: self._cancelled(job_id),
            hash_path=sha256_path,
        )

    def _run(self, job_id: str) -> None:
        job = self.repository.get_render_job(job_id)
        if not job or job.get("status") not in {"queued", "interrupted"}:
            return
        timeline = self._load_validated_timeline(job_id, job)
        if timeline is None:
            return

        workspace: Path | None = None
        temporary: Path | None = None
        try:
            ffmpeg_binary, target, plan, workspace, temporary = self._prepare_render(job_id, job, timeline)
            self._execute_render_plan(
                job_id,
                ffmpeg_binary=ffmpeg_binary,
                target=target,
                plan=plan,
                workspace=workspace,
                temporary=temporary,
            )
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
