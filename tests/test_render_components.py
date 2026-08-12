from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.src.media.render import RenderJobRunner
from backend.src.media.render_plan import (
    MIN_RENDER_FREE_BYTES,
    build_clip_command,
    build_render_plan,
    ensure_supported_features,
    redact_command,
    validate_render_duration,
)
from backend.src.media.render_publish import publish_render, validate_output_target
from backend.src.media.render_sources import SourceVerifier, freeze_still_image
from backend.src.media.video import MediaCancelled, MediaCapabilityError


def _timeline(clip: dict[str, object]) -> dict[str, object]:
    return {
        "format": {
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "duration_ms": 2_500,
            "background_color": "#123456",
        },
        "tracks": [{"role": "primary", "clips": [clip]}],
        "transitions": [],
    }


def _video_clip() -> dict[str, object]:
    return {
        "kind": "video",
        "asset_source_id": "source-one",
        "source_in_ms": 125,
        "timeline_duration_ms": 1_500,
        "audio_enabled": True,
        "volume_db": -3.5,
        "crop": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.6},
        "fit": "contain",
    }


class _SourceRepository:
    def __init__(self, source: dict[str, object]):
        self.source = source
        self.marked: list[tuple[str, str]] = []

    def get_asset_source(self, source_id: str) -> dict[str, object] | None:
        return self.source if source_id == self.source["id"] else None

    def mark_source_availability(self, source_id: str, availability: str) -> None:
        self.marked.append((source_id, availability))


class _PublishRepository:
    def __init__(self, root: Path, *, complete: bool = True, kind: str = "app_preview"):
        self.root = root
        self.complete = complete
        self.kind = kind
        self.completions: list[tuple[str, dict[str, object]]] = []

    def validate_output_root(self, root_id: str) -> tuple[dict[str, object], Path]:
        if root_id != "output-one":
            raise ValueError("Output root identity changed.")
        return {"id": root_id, "kind": self.kind}, self.root

    def open_output_root_fd(self, root_id: str) -> tuple[dict[str, object], Path, int]:
        root, path = self.validate_output_root(root_id)
        return root, path, os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

    def complete_render_job_success(self, job_id: str, **values: object) -> bool:
        self.completions.append((job_id, values))
        return self.complete


class RenderPlanTests(unittest.TestCase):
    def test_plan_and_video_argv_are_stable(self) -> None:
        clip = _video_clip()
        plan = build_render_plan(_timeline(clip), "preview-low")

        self.assertEqual((plan.width, plan.height, plan.fps), (1280, 720, 30))
        self.assertEqual(plan.background_color, "0x123456")
        self.assertEqual(plan.duration_ms, 2_500)
        self.assertEqual(plan.required_free_bytes, MIN_RENDER_FREE_BYTES + 15_728_640)
        self.assertEqual(plan.clips, (clip,))

        argv = build_clip_command(
            plan,
            clip,
            source={
                "codec": {"video_stream_index": 2, "audio_stream_index": 3},
                "rotation_degrees": 90,
            },
            source_path=Path("/library/source.mp4"),
            input_path=Path("/library/source.mp4"),
            output_path=Path("/cache/clip-0000.mp4"),
            ffmpeg_binary="/opt/ffmpeg",
        )
        self.assertEqual(
            argv,
            [
                "/opt/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-protocol_whitelist",
                "file,pipe,fd",
                "-n",
                "-noautorotate",
                "-ss",
                "0.125",
                "-t",
                "1.500",
                "-f",
                "mov",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                "-i",
                "/library/source.mp4",
                "-map",
                "0:2",
                "-map",
                "0:3",
                "-vf",
                "transpose=cclock,crop=iw*0.50000000:ih*0.60000000:iw*0.10000000:ih*0.20000000,"
                "scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x123456,fps=30,setsar=1,format=yuv420p",
                "-af",
                "volume=-3.5dB,aresample=48000,apad,atrim=0:1.500",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                "-t",
                "1.500",
                "/cache/clip-0000.mp4",
            ],
        )

    def test_image_plan_uses_frozen_first_frame_and_silent_audio(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-components-") as directory:
            root = Path(directory)
            animated = root / "animated.gif"
            frozen = root / "frozen.png"
            frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
            frames[0].save(animated, save_all=True, append_images=frames[1:], duration=100, loop=0)

            freeze_still_image(animated, frozen)
            with Image.open(frozen) as image:
                self.assertEqual(image.convert("RGB").getpixel((0, 0)), (255, 0, 0))

            clip = {
                **_video_clip(),
                "kind": "image",
                "source_in_ms": 0,
                "audio_enabled": False,
                "crop": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                "fit": "cover",
            }
            plan = build_render_plan(_timeline(clip), "preview-low")
            argv = build_clip_command(
                plan,
                clip,
                source={"codec": {}, "rotation_degrees": 0},
                source_path=animated,
                input_path=frozen,
                output_path=root / "clip.mp4",
                ffmpeg_binary="ffmpeg",
            )
            self.assertIn(["-loop", "1", "-framerate", "30", "-t", "1.500", "-i", str(frozen)], _windows(argv, 8))
            self.assertIn(
                ["-f", "lavfi", "-t", "1.500", "-i", "anullsrc=r=48000:cl=stereo"],
                _windows(argv, 6),
            )
            self.assertIn(["-map", "0:v:0", "-map", "1:a:0"], _windows(argv, 4))

    def test_redaction_preserves_argv_shape_and_hides_paths(self) -> None:
        redacted = redact_command(
            ["/opt/homebrew/bin/ffmpeg", "-i", "/private/library/source.mp4", "/private/job/out.mp4"],
            {"/private/library/source.mp4": "$SOURCE_asset", "/private/job": "$JOB_DIR"},
        )
        self.assertEqual(redacted, ["ffmpeg", "-i", "$SOURCE_asset", "$JOB_DIR/out.mp4"])

    def test_plan_errors_keep_codes_and_messages(self) -> None:
        clip = _video_clip()
        transition_timeline = _timeline(clip)
        transition_timeline["transitions"] = [{"type": "fade"}]
        with self.assertRaises(MediaCapabilityError) as transition:
            ensure_supported_features(build_render_plan(transition_timeline, "preview-low"))
        self.assertEqual(
            (transition.exception.code, str(transition.exception)),
            (
                "unsupported_render_transition",
                "This renderer does not yet support transitions; remove them before rendering.",
            ),
        )

        invalid_crop = {**clip, "crop": None}
        with self.assertRaises(MediaCapabilityError) as crop:
            build_clip_command(
                build_render_plan(_timeline(invalid_crop), "preview-low"),
                invalid_crop,
                source={"codec": {"video_stream_index": 0, "audio_stream_index": None}},
                source_path=Path("source.mp4"),
                input_path=Path("source.mp4"),
                output_path=Path("output.mp4"),
                ffmpeg_binary="ffmpeg",
            )
        self.assertEqual((crop.exception.code, str(crop.exception)), ("invalid_crop", "Clip crop is invalid."))

        plan = build_render_plan(_timeline(clip), "preview-low")
        with self.assertRaises(MediaCapabilityError) as duration:
            validate_render_duration(plan, {"duration_ms": plan.duration_ms + 1_000})
        self.assertEqual(
            (duration.exception.code, str(duration.exception)),
            ("render_duration_mismatch", "Rendered duration differs from the validated timeline."),
        )


class RenderSourceTests(unittest.TestCase):
    def test_source_is_hashed_once_and_identity_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-source-") as directory:
            root = Path(directory)
            source_path = root / "source.mp4"
            source_path.write_bytes(b"original")
            source = {
                "id": "source-one",
                "asset_id": "asset-one",
                "availability": "available",
                "root_path": str(root),
                "relative_path": source_path.name,
                "sha256": "expected",
            }
            repository = _SourceRepository(source)
            digests: list[Path] = []

            def digest(path: Path) -> str:
                digests.append(path)
                return "expected"

            verifier = SourceVerifier(repository, hash_path=digest)
            first = verifier.verify(_video_clip())
            second = verifier.verify(_video_clip())
            self.assertEqual(first.path, source_path.resolve())
            self.assertEqual(second, first)
            self.assertEqual(digests, [source_path.resolve()])

            source_path.write_bytes(b"changed-and-longer")
            with self.assertRaises(MediaCapabilityError) as changed:
                verifier.verify(_video_clip())
            self.assertEqual(changed.exception.code, "source_changed")
            self.assertEqual(str(changed.exception), "A timeline source changed during rendering.")
            self.assertEqual(repository.marked, [("source-one", "changed")])
            self.assertEqual(digests, [source_path.resolve()])


class RenderPublishTests(unittest.TestCase):
    def test_publication_records_hash_and_cleanup_failure_cannot_downgrade_success(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-publish-") as directory:
            root = Path(directory)
            temporary = root / "assembled.part.mp4"
            temporary.write_bytes(b"artifact")
            repository = _PublishRepository(root)
            job = {"output_root_id": "output-one", "output_relative_path": "preview.mp4"}
            target = validate_output_target(repository, job)

            with patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
                publish_render(
                    repository,
                    job_id="render-one",
                    target=target,
                    temporary=temporary,
                    duration_ms=2_500,
                    ffmpeg_version=lambda: "8.0",
                    cancelled=lambda: False,
                    hash_path=lambda _: "frozen-hash",
                )

            self.assertEqual((root / "preview.mp4").read_bytes(), b"artifact")
            self.assertEqual(
                repository.completions,
                [
                    (
                        "render-one",
                        {
                            "ffmpeg_version": "8.0",
                            "output_sha256": "frozen-hash",
                            "size_bytes": 8,
                            "duration_ms": 2_500,
                        },
                    )
                ],
            )

    def test_publication_cas_loss_rolls_back_link(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-publish-") as directory:
            root = Path(directory)
            temporary = root / "assembled.part.mp4"
            temporary.write_bytes(b"artifact")
            repository = _PublishRepository(root, complete=False)
            target = validate_output_target(
                repository,
                {"output_root_id": "output-one", "output_relative_path": "preview.mp4"},
            )

            with self.assertRaises(MediaCancelled) as cancelled:
                publish_render(
                    repository,
                    job_id="render-one",
                    target=target,
                    temporary=temporary,
                    duration_ms=2_500,
                    ffmpeg_version=lambda: "8.0",
                    cancelled=lambda: False,
                    hash_path=lambda _: "frozen-hash",
                )
            self.assertEqual(str(cancelled.exception), "Render was cancelled during publication.")
            self.assertFalse((root / "preview.mp4").exists())
            self.assertTrue(temporary.exists())

    def test_publication_never_clobbers_a_racing_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-publish-") as directory:
            root = Path(directory)
            temporary = root / "assembled.part.mp4"
            temporary.write_bytes(b"new")
            repository = _PublishRepository(root)
            target = validate_output_target(
                repository,
                {"output_root_id": "output-one", "output_relative_path": "preview.mp4"},
            )
            output = root / "preview.mp4"
            output.write_bytes(b"existing")

            with self.assertRaises(MediaCapabilityError) as exists:
                publish_render(
                    repository,
                    job_id="render-one",
                    target=target,
                    temporary=temporary,
                    duration_ms=2_500,
                    ffmpeg_version=lambda: "8.0",
                    cancelled=lambda: False,
                    hash_path=lambda _: "frozen-hash",
                )
            self.assertEqual(exists.exception.code, "output_exists")
            self.assertEqual(str(exists.exception), "The app artifact filename already exists.")
            self.assertEqual(output.read_bytes(), b"existing")
            self.assertEqual(repository.completions, [])

    def test_output_target_contract_rejects_unsafe_names_and_wrong_root_kind(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-publish-") as directory:
            root = Path(directory)
            repository = _PublishRepository(root)
            with self.assertRaises(MediaCapabilityError) as unsafe:
                validate_output_target(
                    repository,
                    {"output_root_id": "output-one", "output_relative_path": "../escape.mp4"},
                )
            self.assertEqual(
                (unsafe.exception.code, str(unsafe.exception)),
                ("invalid_output_name", "Output must be one safe filename."),
            )

            repository.kind = "export"
            with self.assertRaises(MediaCapabilityError) as wrong_root:
                validate_output_target(
                    repository,
                    {"output_root_id": "output-one", "output_relative_path": "preview.mp4"},
                )
            self.assertEqual(
                (wrong_root.exception.code, str(wrong_root.exception)),
                ("output_root_unavailable", "Preview requires the app output root."),
            )


class RenderRunnerContractTests(unittest.TestCase):
    def test_component_failures_keep_terminal_status_stage_and_error_contract(self) -> None:
        class Repository:
            def __init__(self):
                self.updates: list[tuple[str, dict[str, object]]] = []

            @staticmethod
            def get_render_job(job_id: str) -> dict[str, object]:
                return {"id": job_id, "status": "queued"}

            def update_render_job(self, job_id: str, **values: object) -> None:
                self.updates.append((job_id, values))

        cases = [
            (
                MediaCancelled("Render was cancelled before publication."),
                {
                    "status": "cancelled",
                    "stage": "cancelled",
                    "error": {"code": "cancelled", "message": "Render was cancelled before publication."},
                    "finished": True,
                },
            ),
            (
                MediaCapabilityError("source_changed", "A timeline source changed after import."),
                {
                    "status": "failed",
                    "stage": "failed",
                    "error": {"code": "source_changed", "message": "A timeline source changed after import."},
                    "finished": True,
                },
            ),
            (
                RuntimeError("unexpected"),
                {
                    "status": "failed",
                    "stage": "failed",
                    "error": {"code": "render_failed", "message": "The local render failed."},
                    "finished": True,
                },
            ),
        ]
        for failure, expected in cases:
            with self.subTest(failure=failure):
                repository = Repository()
                runner = object.__new__(RenderJobRunner)
                runner.repository = repository
                with (
                    patch.object(runner, "_load_validated_timeline", return_value={}),
                    patch.object(runner, "_prepare_render", side_effect=failure),
                ):
                    runner._run("render-one")
                self.assertEqual(repository.updates, [("render-one", expected)])


def _windows(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(len(values) - size + 1)]


if __name__ == "__main__":
    unittest.main()
