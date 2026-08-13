from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.src import DESKTOP_TOKEN_HEADER, create_app
from backend.src.media.director import CreativeBriefError, CreativeDirector
from backend.src.media.render import RenderJobRunner, _rotation_filters
from backend.src.media.retrieval import MixedRetrievalService
from backend.src.media.timeline import TimelineService
from backend.src.media.video import (
    MAX_SEGMENTS,
    MAX_VIDEO_DURATION_MS,
    VIDEO_EXTENSIONS,
    MediaCapabilityError,
    ScanFrame,
    discover_media,
    ffprobe,
    parse_ffprobe_payload,
    segment_boundaries,
    sha256_file,
)
from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import MediaRepository, canonical_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _repository(root: Path) -> tuple[MediaRepository, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library


def _register(
    repository: MediaRepository,
    root_id: str,
    path: Path,
    *,
    kind: str,
) -> dict[str, object]:
    metadata = path.stat()
    return repository.upsert_asset_source(
        root_id=root_id,
        relative_path=path.name,
        filename=path.name,
        kind=kind,
        sha256=sha256_file(path),
        mime_type="video/mp4" if kind == "video" else "image/gif" if path.suffix == ".gif" else "image/jpeg",
        file_size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        source_file_id=str(metadata.st_ino),
    )


def _shutdown(app) -> None:
    for key in ("media_job_runner", "render_job_runner"):
        runner = app.extensions.get(key)
        if runner is not None:
            runner.shutdown()


def _wait(repository: MediaRepository, job_id: str, timeout: float = 60) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = repository.get_render_job(job_id)
        if job and job.get("status") in {"succeeded", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"render {job_id} did not finish")


def _image_project(
    repository: MediaRepository,
    asset: dict[str, object],
    *,
    duration_ms: int = 1_000,
) -> dict[str, object]:
    project = repository.create_project(
        "Image hardening fixture",
        {
            "schema_version": "1",
            "goal": "Image hardening fixture",
            "duration_ms": duration_ms,
            "aspect_ratio": "16:9",
            "candidate_refs": [
                {
                    "id": asset["id"],
                    "asset_id": asset["id"],
                    "asset_source_id": asset["asset_source_id"],
                    "result_type": "image_asset",
                }
            ],
        },
        {"created_by": "test", "external_model": False},
    )
    return TimelineService(repository).create_from_project(str(project["id"]))


class RetrievalAndRepositoryHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-hardening-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library = _repository(self.root)
        self.root_id = str(self.repository.library_roots()[0]["id"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fake_image(self, filename: str, content: bytes) -> dict[str, object]:
        path = self.library / filename
        path.write_bytes(content)
        asset = _register(self.repository, self.root_id, path, kind="image")
        self.repository.update_image_probe(str(asset["id"]), width=640, height=360)
        return asset

    def test_zero_score_is_not_grounding_and_constraints_cover_the_candidate_set(self) -> None:
        people = self._fake_image("people.jpg", b"people-image")
        beach = self._fake_image("beach.jpg", b"beach-image")
        retrieval = MixedRetrievalService(self.repository)

        self.assertEqual(retrieval.search({"query": "zzzz_nonexistent_42"})["results"], [])
        director = CreativeDirector(self.repository, retrieval)
        with self.assertRaises(CreativeBriefError) as missing:
            director.create_brief({"goal": "zzzz_nonexistent_42"})
        self.assertEqual(missing.exception.code, "no_grounded_matches")

        project, _ = director.create_brief(
            {
                "goal": "people beach",
                "candidate_refs": [people["id"], beach["id"]],
                "must_include": ["people", "beach"],
            }
        )
        self.assertEqual(len(project["candidates"]), 2)
        with self.assertRaises(CreativeBriefError) as conflict:
            director.create_brief(
                {
                    "goal": "people beach",
                    "candidate_refs": [people["id"], beach["id"]],
                    "must_exclude": ["people"],
                }
            )
        self.assertEqual(conflict.exception.code, "candidate_constraint_conflict")

    def test_video_interval_dedupe_is_independent_of_relevance_order(self) -> None:
        class Candidates:
            @staticmethod
            def mixed_candidates():
                base = {
                    "result_type": "video_segment",
                    "asset_id": "asset-one",
                    "asset_source_id": "source-one",
                    "filename": "keyword.mp4",
                    "summary": "keyword",
                    "combined_text": "keyword",
                    "tags": [],
                    "analysis_run_id": "run-one",
                    "analysis_revision": 1,
                    "source_availability": "available",
                    "width": 640,
                    "height": 360,
                    "duration_ms": 120_000,
                    "captured_at": None,
                }
                return (
                    [
                        {**base, "id": "a-late", "start_ms": 100_000, "end_ms": 110_000},
                        {**base, "id": "b-early", "start_ms": 0, "end_ms": 10_000},
                        {**base, "id": "c-overlap", "start_ms": 500, "end_ms": 9_000},
                    ],
                    {"asset-one": "run-one"},
                )

        results = MixedRetrievalService(Candidates()).search({"query": "keyword"})["results"]
        self.assertEqual([item["id"] for item in results], ["a-late", "b-early"])

    def test_mixed_search_response_contract_and_revision_are_stable(self) -> None:
        class Candidates:
            @staticmethod
            def mixed_candidates():
                video = {
                    "result_type": "video_segment",
                    "asset_id": "asset-video",
                    "asset_source_id": "source-video",
                    "filename": "trip.mp4",
                    "summary": "Beach Sunset subtitle moment",
                    "combined_text": "subtitle beach sunset",
                    "tags": [],
                    "analysis_run_id": "run-current",
                    "analysis_revision": 4,
                    "source_availability": "available",
                    "width": 1920,
                    "height": 1080,
                    "captured_at": None,
                }
                image = {
                    "result_type": "image_asset",
                    "id": "image-a",
                    "asset_id": "asset-image",
                    "asset_source_id": "source-image",
                    "filename": "Beach Sunset.jpg",
                    "summary": "",
                    "combined_text": "",
                    "tags": [],
                    "analysis_run_id": None,
                    "analysis_revision": None,
                    "source_availability": "available",
                    "width": 640,
                    "height": 360,
                    "start_ms": None,
                    "end_ms": None,
                    "captured_at": "2026-01-02T03:04:05+00:00",
                }
                return (
                    [
                        {**video, "id": "video-a", "start_ms": 0, "end_ms": 10_000},
                        {**video, "id": "video-b", "start_ms": 250, "end_ms": 9_000},
                        image,
                        {
                            **image,
                            "id": "zero-score",
                            "asset_id": "asset-zero",
                            "asset_source_id": "source-zero",
                            "filename": "unrelated.jpg",
                        },
                        {
                            **image,
                            "id": "excluded",
                            "asset_id": "asset-excluded",
                            "asset_source_id": "source-excluded",
                            "filename": "blocked Beach Sunset.jpg",
                        },
                        {
                            **image,
                            "id": "portrait",
                            "asset_id": "asset-portrait",
                            "asset_source_id": "source-portrait",
                            "width": 360,
                            "height": 640,
                        },
                        {**video, "id": "too-short", "start_ms": 0, "end_ms": 500},
                    ],
                    {"asset-video": "run-current"},
                )

        filters = {
            "orientation": "landscape",
            "excluded_terms": ["blocked"],
            "duration_min_ms": 1_000,
            "duration_max_ms": 12_000,
        }
        response = MixedRetrievalService(Candidates()).search(
            {
                "text": "  Beach Sunset  ",
                "types": ["video", "image", "unknown"],
                "top_k": 10,
                "filters": filters,
            }
        )

        self.assertIs(response["results"], response["data"])
        search_id = response.pop("id")
        created_at = response.pop("created_at")
        self.assertRegex(search_id, r"^search_[0-9a-f]{32}$")
        self.assertRegex(created_at, r"^\d{4}-\d{2}-\d{2}T.*\+00:00$")
        expected_results = [
            {
                "object": "creative_asset_match",
                "result_type": "video_segment",
                "id": "video-a",
                "asset_id": "asset-video",
                "asset_source_id": "source-video",
                "filename": "trip.mp4",
                "start_ms": 0,
                "end_ms": 10_000,
                "thumbnail_url": "/v1/video-segments/video-a/thumbnail",
                "media_url": "/v1/assets/asset-video/media",
                "summary": "Beach Sunset subtitle moment",
                "matched_terms": ["beach", "sunset"],
                "score": 1.0,
                "grounded": True,
                "confidence": None,
                "analysis_run_id": "run-current",
                "analysis_revision": 4,
                "score_components": {"lexical": 1.0, "semantic": None, "recency": 0.0},
                "source_availability": "available",
                "review": {
                    "revision": 0,
                    "inbox_state": "inbox",
                    "favorite": False,
                    "project_ready": False,
                },
                "provenance": ["local_keyframe", "sidecar_transcript"],
            },
            {
                "object": "creative_asset_match",
                "result_type": "image_asset",
                "id": "image-a",
                "asset_id": "asset-image",
                "asset_source_id": "source-image",
                "filename": "Beach Sunset.jpg",
                "start_ms": None,
                "end_ms": None,
                "thumbnail_url": "/v1/assets/asset-image/thumbnail",
                "media_url": None,
                "summary": "Local image file named Beach Sunset.jpg.",
                "matched_terms": ["beach", "sunset"],
                "score": 1.0,
                "grounded": True,
                "confidence": None,
                "analysis_run_id": None,
                "analysis_revision": None,
                "score_components": {"lexical": 1.0, "semantic": None, "recency": 0.0},
                "source_availability": "available",
                "review": {
                    "revision": 0,
                    "inbox_state": "inbox",
                    "favorite": False,
                    "project_ready": False,
                },
                "provenance": ["image_index"],
            },
        ]
        self.assertEqual(
            response,
            {
                "object": "mixed.search",
                "schema_version": "1",
                "status": "succeeded",
                "query": "Beach Sunset",
                "search_revision": "4abba978c53b2660845f844929512d37757b84264975d7677b3fd7204446d01e",
                "analysis_heads": {"asset-video": "run-current"},
                "results": expected_results,
                "data": expected_results,
                "candidate_count": 3,
                "considered_count": 4,
                "retrieval_mode": "lexical_local_fallback",
                "semantic_available": False,
                "external_analysis": False,
            },
        )

    def test_mixed_search_request_validation_messages_are_stable(self) -> None:
        class NoCandidates:
            @staticmethod
            def mixed_candidates():
                return [], {}

        retrieval = MixedRetrievalService(NoCandidates())
        cases = [
            ({}, "`query` must be a non-empty string."),
            ({"query": "valid", "top_k": True}, "`top_k` must be an integer from 1 to 100."),
            ({"query": "valid", "top_k": 0}, "`top_k` must be an integer from 1 to 100."),
            (
                {"query": "valid", "types": ["unknown"]},
                "`types` must include image/image_asset or video/video_segment.",
            ),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, re.escape(message)):
                retrieval.search(payload)

    def test_duplicate_content_rebind_preserves_one_preferred_source(self) -> None:
        first_path = self.library / "first.mp4"
        second_path = self.library / "second.mp4"
        first_path.write_bytes(b"old-content")
        second_path.write_bytes(b"new-content")
        old = _register(self.repository, self.root_id, first_path, kind="video")
        target = _register(self.repository, self.root_id, second_path, kind="video")

        first_path.write_bytes(second_path.read_bytes())
        rebound = _register(self.repository, self.root_id, first_path, kind="video")
        self.assertEqual(rebound["id"], target["id"])
        sources = self.repository.available_sources(str(target["id"]))
        self.assertEqual(len(sources), 2)
        self.assertEqual(sum(int(item["is_preferred"]) for item in sources), 1)
        self.assertIsNone(self.repository.get_asset(str(old["id"]))["asset_source_id"])

    def test_concurrent_import_job_creation_reuses_one_active_job(self) -> None:
        video = self.library / "same.mp4"
        video.write_bytes(b"same-video")
        asset = _register(self.repository, self.root_id, video, kind="video")

        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = list(
                executor.map(
                    lambda _: self.repository.create_analysis_job(
                        asset_id=str(asset["id"]), reuse_active=True
                    ),
                    range(2),
                )
            )
        self.assertEqual({str(job["id"]) for job in jobs}, {str(jobs[0]["id"])})
        self.assertEqual(sum(bool(job["reused"]) for job in jobs), 1)

    def test_same_size_and_mtime_content_replacement_is_not_streamed(self) -> None:
        source = self.library / "fixed.mp4"
        source.write_bytes(b"AAAA")
        asset = _register(self.repository, self.root_id, source, kind="video")
        original = source.stat()
        opened = self.repository.open_asset_file(str(asset["id"]))
        self.assertIsNotNone(opened)
        assert opened is not None
        opened[1].close()

        source.write_bytes(b"BBBB")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertIsNone(self.repository.open_asset_file(str(asset["id"])))
        changed = self.repository.get_asset_source(str(asset["asset_source_id"]))
        assert changed is not None
        self.assertEqual(changed["availability"], "changed")

    def test_eight_hour_high_motion_segmentation_is_thinned_not_rejected(self) -> None:
        duration = MAX_VIDEO_DURATION_MS
        interval = duration // 7_200
        frames = [
            ScanFrame(
                timestamp_ms=min(duration - 1, index * interval),
                path=Path(f"{index}.jpg"),
                brightness=0.5,
                sharpness=1.0,
                histogram=np.array([1.0]),
                novelty=1.0,
            )
            for index in range(7_200)
        ]
        segments = segment_boundaries(frames, duration)
        self.assertLessEqual(len(segments), MAX_SEGMENTS)
        self.assertEqual(segments[0][0], 0)
        self.assertEqual(segments[-1][1], duration)

    def test_video_only_discovery_ignores_large_photo_count(self) -> None:
        for index in range(501):
            (self.library / f"photo-{index:04d}.jpg").write_bytes(b"image")
        video = self.library / "only-video.mp4"
        video.write_bytes(b"video")
        found = discover_media(
            self.library,
            recursive=True,
            extensions=VIDEO_EXTENSIONS,
            max_files=1,
            max_total_bytes=1024,
        )
        self.assertEqual(found, [video])


class ProbeContractHardeningTests(unittest.TestCase):
    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "format": {"duration": "3", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 640,
                    "height": 360,
                    "disposition": {"default": 0},
                },
                {
                    "index": 1,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 320,
                    "height": 180,
                    "disposition": {"default": 1},
                },
                {"index": 2, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 0}},
                {"index": 3, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 1}},
            ],
        }

    def test_container_allowlist_and_default_stream_selection(self) -> None:
        payload = self._payload()
        probe = parse_ffprobe_payload(payload)
        self.assertEqual(probe["width"], 320)
        self.assertEqual(probe["codec"]["video_stream_index"], 1)
        self.assertEqual(probe["codec"]["audio_stream_index"], 3)
        payload["format"]["format_name"] = "concat"
        with self.assertRaisesRegex(MediaCapabilityError, "QuickTime"):
            parse_ffprobe_payload(payload)

    def test_display_rotation_uses_ffmpeg_counterclockwise_semantics(self) -> None:
        self.assertEqual(_rotation_filters(90), ["transpose=cclock"])
        self.assertEqual(_rotation_filters(180), ["hflip", "vflip"])
        self.assertEqual(_rotation_filters(270), ["transpose=clock"])


class RouteBoundaryHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-route-hardening-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        Image.new("RGB", (32, 18), "blue").save(self.library / "photo.jpg")
        self.token = "hardening-desktop-token"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(PROJECT_ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.root / "state" / "media.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.token,
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        _shutdown(self.app)
        self.environment.stop()
        self.temporary.cleanup()

    def test_database_mismatch_in_progress_and_legacy_library_boundaries(self) -> None:
        mismatch = self.client.get(
            "/v1/media/capabilities",
            query_string={"db_path": str(self.root / "other.db")},
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json["code"], "database_binding_mismatch")

        outside = self.root / "outside"
        outside.mkdir()
        Image.new("RGB", (16, 16), "red").save(outside / "secret.jpg")
        override = self.client.get(
            "/v1/library/previews/secret.jpg",
            query_string={"root_path": str(outside)},
        )
        self.assertEqual(override.status_code, 403)
        cross_site = self.client.get(
            "/v1/library/previews/photo.jpg",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(cross_site.status_code, 403)
        unauthenticated = self.client.get("/v1/library/files/photo.jpg")
        self.assertEqual(unauthenticated.status_code, 401)
        authenticated = self.client.get(
            "/v1/library/files/photo.jpg",
            headers={DESKTOP_TOKEN_HEADER: self.token, "Range": "bytes=0-15"},
        )
        self.assertEqual(authenticated.status_code, 206)
        self.assertEqual(len(authenticated.data), 16)
        authenticated.close()

        repository = self.app.extensions["media_repository"]
        payload = {
            "db_path": str(repository.db_path),
            "root_path": str(self.library),
            "recursive": False,
            "kinds": ["video"],
        }
        repository.claim_idempotency(
            scope="desktop:POST:/v1/assets/import",
            key="still-running",
            request_sha256=hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
            resource_type="POST:/v1/assets/import",
            resource_id=None,
        )
        in_progress = self.client.post(
            "/v1/assets/import",
            json=payload,
            headers={DESKTOP_TOKEN_HEADER: self.token, "Idempotency-Key": "still-running"},
        )
        self.assertEqual(in_progress.status_code, 409)
        self.assertEqual(in_progress.json["code"], "request_in_progress")
        self.assertTrue(in_progress.json["error"]["retryable"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class RealRenderHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-render-hardening-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library = _repository(self.root)
        self.root_id = str(self.repository.library_roots()[0]["id"])
        self.preview = self.repository.register_preview_root(self.root / "state" / "previews")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _render(self, timeline: dict[str, object], filename: str) -> tuple[dict[str, object], Path]:
        job = self.repository.create_render_job(
            timeline_id=str(timeline["timeline"]["id"]),
            timeline_revision=int(timeline["timeline"]["revision"]),
            profile="preview-low",
            output_root_id=str(self.preview["id"]),
            output_relative_path=filename,
            timeline_content_sha256=str(timeline["content_sha256"]),
        )
        runner = RenderJobRunner(self.repository, self.root / "state" / "media-cache")
        try:
            runner.submit(str(job["id"]))
            finished = _wait(self.repository, str(job["id"]))
        finally:
            runner.shutdown()
        return finished, self.root / "state" / "previews" / filename

    def test_gif_first_frame_renders_and_interrupted_artifact_is_reconciled(self) -> None:
        gif = self.library / "animated.gif"
        frames = [Image.new("RGB", (96, 64), color) for color in ("red", "blue")]
        frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
        asset = _register(self.repository, self.root_id, gif, kind="image")
        self.repository.update_image_probe(str(asset["id"]), width=96, height=64)
        timeline = _image_project(self.repository, asset)
        finished, artifact = self._render(timeline, "gif-preview.mp4")
        self.assertEqual(finished["status"], "succeeded", finished.get("error"))
        self.assertAlmostEqual(ffprobe(artifact)["duration_ms"], 1_000, delta=70)

        orphan_job = self.repository.create_render_job(
            timeline_id=str(timeline["timeline"]["id"]),
            timeline_revision=1,
            profile="preview-low",
            output_root_id=str(self.preview["id"]),
            output_relative_path="interrupted.mp4",
            timeline_content_sha256=str(timeline["content_sha256"]),
        )
        orphan = self.root / "state" / "previews" / "interrupted.mp4"
        orphan.write_bytes(b"orphan")
        self.repository.update_render_job(str(orphan_job["id"]), status="interrupted", finished=True)
        runner = RenderJobRunner(self.repository, self.root / "state" / "media-cache")
        runner.shutdown()
        self.assertFalse(orphan.exists())

    def test_short_audio_is_padded_and_same_source_is_hashed_once(self) -> None:
        video = self.library / "short-audio.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=320x180:r=30:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-t",
                "3",
                str(video),
            ],
            check=True,
            timeout=30,
        )
        asset = _register(self.repository, self.root_id, video, kind="video")
        probe = ffprobe(video)
        self.repository.update_asset_probe(str(asset["id"]), probe)
        analysis = self.repository.create_analysis_job(asset_id=str(asset["id"]))
        segment_id = "short-audio-segment"
        self.repository.commit_video_analysis(
            job_id=str(analysis["id"]),
            segments=[
                {
                    "id": segment_id,
                    "ordinal": 0,
                    "start_ms": 0,
                    "end_ms": 3_000,
                    "boundary_reason": "test",
                    "summary": "short audio green",
                    "semantic": {"tags": ["green"]},
                    "combined_text": "short audio green",
                    "visual_status": "local_fallback",
                    "transcript_status": "unavailable",
                    "confidence": None,
                }
            ],
            keyframes=[
                {
                    "id": "short-audio-keyframe",
                    "segment_id": segment_id,
                    "timestamp_ms": 1_000,
                    "cache_key": "keyframes/test.jpg",
                    "sha256": "0" * 64,
                    "width": 320,
                    "height": 180,
                    "selection_reason": "test",
                    "is_representative": True,
                }
            ],
            transcripts=[],
        )
        match = {
            "id": segment_id,
            "asset_id": asset["id"],
            "asset_source_id": asset["asset_source_id"],
            "result_type": "video_segment",
        }
        project = self.repository.create_project(
            "Two cuts from one source",
            {
                "schema_version": "1",
                "goal": "green",
                "duration_ms": 6_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [match, match],
            },
            {"created_by": "test", "external_model": False},
        )
        timeline = TimelineService(self.repository).create_from_project(str(project["id"]))
        source_path = video.resolve()
        from core.media_db import sha256_path as real_sha256_path

        with patch("backend.src.media.render.sha256_path", wraps=real_sha256_path) as digest:
            finished, artifact = self._render(timeline, "short-audio-preview.mp4")
        self.assertEqual(finished["status"], "succeeded", finished.get("error"))
        source_hashes = [call for call in digest.call_args_list if call.args and call.args[0] == source_path]
        self.assertEqual(len(source_hashes), 1)
        self.assertAlmostEqual(ffprobe(artifact)["duration_ms"], 6_000, delta=70)

    def test_fake_concat_mov_is_rejected_before_external_reference_can_open(self) -> None:
        outside = self.root / "private.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=64x64:d=1",
                "-c:v",
                "libx264",
                str(outside),
            ],
            check=True,
            timeout=30,
        )
        (self.library / "nested.mp4").symlink_to(outside)
        disguised = self.library / "concat.mov"
        disguised.write_text("ffconcat version 1.0\nfile nested.mp4\n", encoding="utf-8")
        with self.assertRaises(MediaCapabilityError):
            ffprobe(disguised)


if __name__ == "__main__":
    unittest.main()
