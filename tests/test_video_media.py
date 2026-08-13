from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from backend.src import DESKTOP_TOKEN_HEADER, create_app
from backend.src.media.director import CreativeDirector
from backend.src.media.render import RenderJobRunner
from backend.src.media.retrieval import MixedRetrievalService
from backend.src.media.timeline import TimelineService
from backend.src.media.video import (
    MAX_VIDEO_DURATION_MS,
    MediaCapabilityError,
    MediaJobRunner,
    binary_capability,
    ffprobe,
    parse_ffprobe_payload,
    parse_sidecar_subtitles,
    resolve_inside_root,
    sha256_file,
)
from core.db import ImageIndexRepository
from core.config import Settings
from core.media_db import (
    IdempotencyConflictError,
    MediaMigrationError,
    MediaRepository,
    sha256_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = PROJECT_ROOT / "scripts" / "create_demo_library.py"
TERMINAL_JOB_STATES = {
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "interrupted",
    "blocked_source_unavailable",
}


def _wait_for_job(getter, job_id: str, *, timeout: float = 90.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] | None = None
    while time.monotonic() < deadline:
        last = getter(job_id)
        if last and last.get("status") in TERMINAL_JOB_STATES:
            return last
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s; last={last!r}")


def _execute_render_job(
    repository: MediaRepository,
    cache_root: Path,
    job: dict[str, object],
) -> dict[str, object]:
    runner = RenderJobRunner(repository, cache_root)
    try:
        runner.submit(str(job["id"]))
        return _wait_for_job(repository.get_render_job, str(job["id"]), timeout=120)
    finally:
        runner.shutdown()


def _initialize_repository(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library, db_path


def _test_environment(root: Path, library: Path, token: str):
    return patch.dict(
        os.environ,
        {
            "APP_CONFIG_PATH": str(PROJECT_ROOT / "config.yaml"),
            "MEMOLENS_APP_STATE_DIR": str(root / "state"),
            "IMAGE_LIBRARY_DIR": str(library),
            "SQLITE_DB_PATH": str(root / "state" / "media.db"),
            "MEMOLENS_DESKTOP_SESSION_TOKEN": token,
            "MINIMAX_KEY": "",
            "OPENAI_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "VERTEX_ACCESS_TOKEN": "",
            "GOOGLE_OAUTH_ACCESS_TOKEN": "",
        },
        clear=False,
    )


def _shutdown_app(app) -> None:
    for extension in ("media_job_runner", "render_job_runner"):
        runner = app.extensions.get(extension)
        if runner is not None:
            runner.shutdown()


def _register_bytes(
    repository: MediaRepository,
    root_id: str,
    root: Path,
    relative_path: str,
    *,
    content: bytes,
    kind: str = "video",
) -> dict[str, object]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stat_result = path.stat()
    return repository.upsert_asset_source(
        root_id=root_id,
        relative_path=relative_path,
        filename=path.name,
        kind=kind,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="video/mp4" if kind == "video" else "image/jpeg",
        file_size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        source_file_id=str(stat_result.st_ino),
    )


def _analysis_payload(job: dict[str, object], revision: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    segment_id = f"segment-r{revision}"
    segment = {
        "id": segment_id,
        "ordinal": 0,
        "start_ms": 0,
        "end_ms": 2_000,
        "boundary_reason": "test_fixture",
        "summary": f"analysis revision {revision}",
        "semantic": {"tags": ["fixture", f"revision-{revision}"]},
        "combined_text": f"fixture analysis revision {revision}",
        "visual_status": "local_fallback",
        "transcript_status": "unavailable",
        "confidence": None,
    }
    frame = {
        "id": f"keyframe-r{revision}",
        "segment_id": segment_id,
        "timestamp_ms": 1_000,
        "cache_key": f"keyframes/{segment_id}.jpg",
        "sha256": hashlib.sha256(f"frame-{revision}".encode()).hexdigest(),
        "width": 320,
        "height": 180,
        "selection_reason": "test_fixture",
        "is_representative": True,
    }
    assert int(job["analysis_revision"]) == revision
    return [segment], [frame]


class MediaRepositoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-media-db-")
        self.root = Path(self.temporary_directory.name).resolve()
        self.repository, self.library, self.db_path = _initialize_repository(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_additive_schema_has_stable_database_identity_and_verified_migrations(self) -> None:
        database_uuid = self.repository.database_uuid
        self.repository.ensure_schema(self.library)

        self.assertEqual(self.repository.database_uuid, database_uuid)
        with sqlite3.connect(self.db_path) as connection:
            migrations = connection.execute(
                "SELECT version,name FROM schema_migrations ORDER BY version"
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            meta = connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton=1"
            ).fetchone()

        self.assertEqual(
            migrations,
            [
                (1, "image_index_baseline"),
                (2, "video_creative_workbench"),
                (3, "creator_memory_media_inbox"),
            ],
        )
        self.assertEqual(meta, (3,))
        self.assertTrue(
            {
                "image_index",
                "assets",
                "asset_sources",
                "analysis_runs",
                "asset_analysis_heads",
                "video_segments",
                "creative_briefs",
                "timelines",
                "media_jobs",
                "render_jobs",
                "idempotency_records",
                "asset_review_revisions",
                "creator_profile_revisions",
            }.issubset(tables)
        )

    def test_migration_checksum_tampering_fails_closed(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum='tampered' WHERE version=2"
            )

        with self.assertRaisesRegex(MediaMigrationError, "migration_checksum_mismatch"):
            self.repository.ensure_schema(self.library)

    def test_identical_asset_can_have_multiple_sources_but_only_one_preferred_source(self) -> None:
        root_one = self.repository.library_roots()[0]
        second_root = self.root / "second-library"
        second_root.mkdir()
        root_two = self.repository.register_library_root(second_root)
        content = b"the same immutable video bytes"

        first = _register_bytes(
            self.repository,
            str(root_one["id"]),
            self.library,
            "one.mp4",
            content=content,
        )
        second = _register_bytes(
            self.repository,
            str(root_two["id"]),
            second_root,
            "nested/two.mp4",
            content=content,
        )

        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["asset_source_id"], second["asset_source_id"])
        sources = self.repository.available_sources(str(first["id"]))
        self.assertEqual(len(sources), 2)
        self.assertEqual(sum(int(source["is_preferred"]) for source in sources), 1)
        self.assertEqual({source["library_root_id"] for source in sources}, {root_one["id"], root_two["id"]})

    def test_same_path_with_new_content_atomically_rebinds_source_without_rewriting_old_asset(self) -> None:
        root_id = str(self.repository.library_roots()[0]["id"])
        original = _register_bytes(
            self.repository,
            root_id,
            self.library,
            "mutable-name.mp4",
            content=b"original immutable asset bytes",
        )
        replacement = _register_bytes(
            self.repository,
            root_id,
            self.library,
            "mutable-name.mp4",
            content=b"replacement immutable asset bytes",
        )

        self.assertNotEqual(original["id"], replacement["id"])
        self.assertEqual(original["asset_source_id"], replacement["asset_source_id"])
        rebound = self.repository.get_asset_source(str(replacement["asset_source_id"]))
        assert rebound is not None
        self.assertEqual(rebound["asset_id"], replacement["id"])
        self.assertEqual(rebound["availability"], "available")
        self.assertTrue(rebound["is_preferred"])
        old_asset = self.repository.get_asset(str(original["id"]))
        assert old_asset is not None
        self.assertIsNone(old_asset["asset_source_id"])
        self.assertEqual(old_asset["sha256"], hashlib.sha256(b"original immutable asset bytes").hexdigest())

    def test_out_of_order_analysis_completion_never_rolls_back_current_head(self) -> None:
        root_id = str(self.repository.library_roots()[0]["id"])
        asset = _register_bytes(
            self.repository,
            root_id,
            self.library,
            "head-order.mp4",
            content=b"synthetic bytes for head ordering",
        )
        self.repository.update_asset_probe(
            str(asset["id"]),
            {
                "duration_ms": 2_000,
                "width": 320,
                "height": 180,
                "rotation_degrees": 0,
                "captured_at": None,
                "codec": {"video_codec": "fixture", "audio_streams": []},
            },
        )
        revision_one = self.repository.create_analysis_job(asset_id=str(asset["id"]))
        revision_two = self.repository.create_analysis_job(asset_id=str(asset["id"]))

        segments_two, frames_two = _analysis_payload(revision_two, 2)
        self.repository.commit_video_analysis(
            job_id=str(revision_two["id"]),
            segments=segments_two,
            keyframes=frames_two,
            transcripts=[],
        )
        segments_one, frames_one = _analysis_payload(revision_one, 1)
        self.repository.commit_video_analysis(
            job_id=str(revision_one["id"]),
            segments=segments_one,
            keyframes=frames_one,
            transcripts=[],
        )

        candidates, heads = self.repository.mixed_candidates()
        current_video_ids = {
            str(candidate["id"])
            for candidate in candidates
            if candidate["result_type"] == "video_segment"
        }
        self.assertEqual(heads[str(asset["id"])], revision_two["analysis_run_id"])
        self.assertEqual(current_video_ids, {"segment-r2"})

    def test_idempotency_replays_frozen_response_and_rejects_payload_conflict(self) -> None:
        claim = self.repository.claim_idempotency(
            scope="asset.import",
            key="request-123",
            request_sha256="a" * 64,
            resource_type="media_job",
            resource_id=None,
        )
        self.assertIsNone(claim)
        frozen_response = {"object": "asset.import", "status": "accepted", "job_ids": ["job-1"]}
        self.repository.complete_idempotency(
            scope="asset.import",
            key="request-123",
            response_status=202,
            response=frozen_response,
        )

        replay = self.repository.claim_idempotency(
            scope="asset.import",
            key="request-123",
            request_sha256="a" * 64,
            resource_type="media_job",
            resource_id=None,
        )
        assert replay is not None
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(replay["response_status"], 202)
        self.assertEqual(replay["response"], frozen_response)
        with self.assertRaises(IdempotencyConflictError):
            self.repository.claim_idempotency(
                scope="asset.import",
                key="request-123",
                request_sha256="b" * 64,
                resource_type="media_job",
                resource_id=None,
            )

        self.assertIsNone(
            self.repository.claim_idempotency(
                scope="render.start",
                key="failed-request-456",
                request_sha256="c" * 64,
                resource_type="render_job",
                resource_id=None,
            )
        )
        frozen_failure = {
            "object": "error",
            "code": "timeline_hash_mismatch",
            "message": "Expected timeline SHA-256 does not match.",
        }
        self.repository.complete_idempotency(
            scope="render.start",
            key="failed-request-456",
            response_status=409,
            response=frozen_failure,
            failed=True,
        )
        failed_replay = self.repository.claim_idempotency(
            scope="render.start",
            key="failed-request-456",
            request_sha256="c" * 64,
            resource_type="render_job",
            resource_id=None,
        )
        assert failed_replay is not None
        self.assertEqual(failed_replay["state"], "failed")
        self.assertEqual(failed_replay["response_status"], 409)
        self.assertEqual(failed_replay["response"], frozen_failure)

    def test_render_success_compare_and_swap_loses_to_cancellation(self) -> None:
        root_id = str(self.repository.library_roots()[0]["id"])
        image = _register_bytes(
            self.repository,
            root_id,
            self.library,
            "render-race.jpg",
            content=b"render-race-image",
            kind="image",
        )
        self.repository.update_image_probe(str(image["id"]), width=640, height=360)
        project = self.repository.create_project(
            "Render race",
            {
                "schema_version": "1",
                "goal": "Render race",
                "duration_ms": 1_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [
                    {
                        "id": image["id"],
                        "asset_id": image["id"],
                        "asset_source_id": image["asset_source_id"],
                        "result_type": "image_asset",
                    }
                ],
            },
            {"created_by": "test", "external_model": False},
        )
        timeline = TimelineService(self.repository).create_from_project(str(project["id"]))
        preview_root = self.repository.register_preview_root(self.root / "preview-race")
        job = self.repository.create_render_job(
            timeline_id=str(timeline["timeline"]["id"]),
            timeline_revision=1,
            profile="preview-low",
            output_root_id=str(preview_root["id"]),
            output_relative_path="race.mp4",
            timeline_content_sha256=str(timeline["content_sha256"]),
        )
        self.repository.update_render_job(str(job["id"]), status="running")
        self.assertTrue(self.repository.request_render_cancel(str(job["id"])))
        won = self.repository.complete_render_job_success(
            str(job["id"]),
            ffmpeg_version="ffmpeg version 8",
            output_sha256="a" * 64,
            size_bytes=1,
            duration_ms=1_000,
        )

        self.assertFalse(won)
        current = self.repository.get_render_job(str(job["id"]))
        assert current is not None
        self.assertEqual(current["status"], "cancelling")
        self.assertTrue(current["cancel_requested"])

    def test_cancelled_video_job_cannot_publish_segments_or_replace_head(self) -> None:
        root_id = str(self.repository.library_roots()[0]["id"])
        video = _register_bytes(
            self.repository,
            root_id,
            self.library,
            "analysis-race.mp4",
            content=b"analysis-race-video-bytes",
        )
        self.repository.update_asset_probe(
            str(video["id"]),
            {
                "duration_ms": 2_000,
                "width": 320,
                "height": 180,
                "rotation_degrees": 0,
                "captured_at": None,
                "codec": {"video_codec": "fixture", "audio_streams": []},
            },
        )
        job = self.repository.create_analysis_job(asset_id=str(video["id"]))
        segments, frames = _analysis_payload(job, 1)
        self.repository.update_media_job(str(job["id"]), status="running")
        self.assertTrue(self.repository.request_media_job_cancel(str(job["id"])))

        with self.assertRaisesRegex(RuntimeError, "analysis_commit_rejected"):
            self.repository.commit_video_analysis(
                job_id=str(job["id"]),
                segments=segments,
                keyframes=frames,
                transcripts=[],
            )

        with self.repository._connect() as connection:
            segment_count = connection.execute(
                "SELECT COUNT(*) FROM video_segments WHERE asset_id=?",
                (video["id"],),
            ).fetchone()[0]
            head_count = connection.execute(
                "SELECT COUNT(*) FROM asset_analysis_heads WHERE asset_id=?",
                (video["id"],),
            ).fetchone()[0]
        self.assertEqual(segment_count, 0)
        self.assertEqual(head_count, 0)


class MediaPathAndProbeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-media-path-")
        self.root = Path(self.temporary_directory.name).resolve() / "library"
        self.root.mkdir()
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"fixture")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_resolve_inside_root_rejects_traversal_absolute_and_every_symlink_component(self) -> None:
        self.assertEqual(resolve_inside_root(self.root, "clip.mp4"), self.video.resolve())
        outside = self.root.parent / "outside.mp4"
        outside.write_bytes(b"outside")
        (self.root / "linked.mp4").symlink_to(outside)
        real_directory = self.root.parent / "real-directory"
        real_directory.mkdir()
        (real_directory / "nested.mp4").write_bytes(b"nested")
        (self.root / "linked-directory").symlink_to(real_directory, target_is_directory=True)

        for unsafe_path in (
            "../outside.mp4",
            str(outside),
            "linked.mp4",
            "linked-directory/nested.mp4",
            "clip.mp4\x00ignored",
        ):
            with self.subTest(path=unsafe_path), self.assertRaises((OSError, ValueError)):
                resolve_inside_root(self.root, unsafe_path)

    def test_sidecar_parser_rejects_symlink_and_size_limit_but_parses_bounded_srt(self) -> None:
        subtitle = self.video.with_suffix(".srt")
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,250\nHello <b>MemoLens</b>\n\n"
            "2\n00:00:01,250 --> 00:00:02,500\nLocal-only media\n",
            encoding="utf-8",
        )
        parsed = parse_sidecar_subtitles(self.video, "asset_fixture", 1)
        self.assertEqual([item["text"] for item in parsed], ["Hello MemoLens", "Local-only media"])
        self.assertTrue(all(item["provider"] == "local_sidecar" for item in parsed))

        subtitle.unlink()
        outside_subtitle = self.root.parent / "outside.srt"
        outside_subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nsecret\n",
            encoding="utf-8",
        )
        subtitle.symlink_to(outside_subtitle)
        self.assertEqual(parse_sidecar_subtitles(self.video, "asset_fixture", 1), [])

        subtitle.unlink()
        with subtitle.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(MediaCapabilityError, "8 MiB"):
            parse_sidecar_subtitles(self.video, "asset_fixture", 1)

    def test_ffprobe_payload_is_normalized_and_invalid_or_excessive_media_fails_closed(self) -> None:
        payload = {
            "format": {
                "duration": "9.125",
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "tags": {"creation_time": "2026-08-12T01:02:03Z"},
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": 1280,
                    "height": 720,
                    "avg_frame_rate": "24/1",
                    "time_base": "1/12288",
                    "side_data_list": [{"rotation": -90}],
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }
        probe = parse_ffprobe_payload(payload)
        self.assertEqual(probe["duration_ms"], 9_125)
        self.assertEqual((probe["width"], probe["height"], probe["rotation_degrees"]), (1280, 720, 270))
        self.assertEqual(probe["codec"]["audio_streams"][0]["codec"], "aac")

        invalid_payloads = (
            {},
            {"format": {"duration": "NaN"}, "streams": [{"codec_type": "video", "width": 1, "height": 1}]},
            {
                "format": {"duration": str((MAX_VIDEO_DURATION_MS + 1) / 1000)},
                "streams": [{"codec_type": "video", "width": 1, "height": 1}],
            },
        )
        for invalid in invalid_payloads:
            with self.subTest(payload=invalid), self.assertRaises(MediaCapabilityError):
                parse_ffprobe_payload(invalid)

    def test_ffmpeg_major_version_gate_rejects_v5_and_accepts_v6_plus(self) -> None:
        def completed(version: str) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout=f"ffmpeg version {version}\n", stderr="")

        with patch("backend.src.media.video.resolve_binary", return_value="/usr/bin/ffmpeg"):
            with patch("backend.src.media.video.subprocess.run", return_value=completed("5.1.6")):
                version_five = binary_capability("ffmpeg")
            with patch("backend.src.media.video.subprocess.run", return_value=completed("6.0.1")):
                version_six = binary_capability("ffmpeg")

        self.assertFalse(version_five["available"])
        self.assertFalse(version_five["supported"])
        self.assertTrue(version_six["available"])
        self.assertTrue(version_six["supported"])


class MediaRouteSecurityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-media-routes-")
        self.root = Path(self.temporary_directory.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.token = "route-security-desktop-token"
        self.environment = _test_environment(self.root, self.library, self.token)
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        _shutdown_app(self.app)
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_every_privileged_media_surface_requires_desktop_token_even_for_originless_loopback(self) -> None:
        requests = (
            ("POST", "/v1/assets/import"),
            ("GET", "/v1/assets/not-found/media"),
            ("POST", "/v1/index/jobs/not-found/cancel"),
            ("POST", "/v1/index/jobs/not-found/resume"),
            ("POST", "/v1/creative/briefs"),
            ("POST", "/v1/creative/projects/not-found/timelines"),
            ("POST", "/v1/timelines/not-found/validate"),
            ("POST", "/v1/timelines/not-found/revise"),
            ("POST", "/v1/renders"),
            ("POST", "/v1/renders/not-found/cancel"),
            ("GET", "/v1/renders/not-found/download"),
        )
        for method, path in requests:
            with self.subTest(method=method, path=path):
                response = self.client.open(path, method=method, json={} if method == "POST" else None)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json["code"], "desktop_auth_required")

        for path in (
            "/v1/assets/not-found/media?db_path=/tmp/not-memolens.db",
            "/v1/timelines/not-found/validate?db_path=/tmp/not-memolens.db",
            "/v1/renders/not-found/download?db_path=/tmp/not-memolens.db",
        ):
            with self.subTest(path=path):
                response = self.client.open(
                    path,
                    method="POST" if "/validate" in path else "GET",
                    json={} if "/validate" in path else None,
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json["code"], "desktop_auth_required")

    def test_every_media_mutation_accepts_a_trusted_browser_preflight_without_a_json_body(self) -> None:
        mutation_paths = (
            ("PUT", "/v1/inbox/assets/not-found"),
            ("PUT", "/v1/creator/profile"),
            ("POST", "/v1/assets/import"),
            ("POST", "/v1/index/jobs/not-found/cancel"),
            ("POST", "/v1/index/jobs/not-found/resume"),
            ("POST", "/v1/creative/briefs"),
            ("POST", "/v1/creative/projects/not-found/timelines"),
            ("POST", "/v1/timelines/not-found/revise"),
            ("POST", "/v1/renders"),
            ("POST", "/v1/renders/not-found/cancel"),
        )
        for method, path in mutation_paths:
            with self.subTest(method=method, path=path):
                response = self.client.options(
                    path,
                    headers={
                        "Origin": "http://127.0.0.1:5173",
                        "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": (
                            "content-type, idempotency-key, x-memolens-desktop-token"
                        ),
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["Access-Control-Allow-Origin"],
                    "http://127.0.0.1:5173",
                )
                allowed_headers = response.headers["Access-Control-Allow-Headers"].casefold()
                self.assertIn("idempotency-key", allowed_headers)
                self.assertIn("x-memolens-desktop-token", allowed_headers)

        packaged_renderer = self.client.options(
            "/v1/inbox/assets/not-found",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": (
                    "content-type, idempotency-key, x-memolens-desktop-token"
                ),
            },
        )
        self.assertEqual(packaged_renderer.status_code, 200)
        self.assertEqual(packaged_renderer.headers["Access-Control-Allow-Origin"], "null")

    def test_read_only_media_surfaces_keep_loopback_codex_and_trusted_browser_access(self) -> None:
        requests = (
            ("GET", "/v1/media/capabilities", None, 200),
            ("GET", "/v1/index/jobs", None, 200),
            ("GET", "/v1/index/jobs/not-found", None, 404),
            ("POST", "/v1/search/mixed", {}, 400),
            ("GET", "/v1/assets/not-found/thumbnail", None, 404),
            ("GET", "/v1/video-segments/not-found", None, 404),
            ("GET", "/v1/video-segments/not-found/thumbnail", None, 404),
            ("GET", "/v1/keyframes/not-found", None, 404),
            ("GET", "/v1/creative/projects/not-found", None, 404),
            ("GET", "/v1/timelines/not-found", None, 404),
            ("GET", "/v1/timelines/not-found/revisions", None, 404),
            ("GET", "/v1/renders", None, 200),
            ("GET", "/v1/renders/not-found", None, 404),
        )
        for method, path, payload, expected_status in requests:
            with self.subTest(method=method, path=path):
                response = self.client.open(path, method=method, json=payload)
                self.assertEqual(response.status_code, expected_status)

        response = self.client.get(
            "/v1/media/capabilities",
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5173")

        untrusted = self.client.get(
            "/v1/media/capabilities",
            headers={"Origin": "http://127.0.0.1:9999"},
        )
        self.assertEqual(untrusted.status_code, 403)

        non_loopback = self.client.get(
            "/v1/media/capabilities",
            environ_overrides={"REMOTE_ADDR": "10.0.0.8"},
        )
        self.assertEqual(non_loopback.status_code, 403)

    def test_trusted_browser_origin_does_not_turn_read_access_into_write_authority(self) -> None:
        response = self.client.post(
            "/v1/assets/import",
            json={
                "db_path": str(self.app.extensions["media_repository"].db_path),
                "relative_paths": [],
                "recursive": False,
            },
            headers={"Origin": "http://127.0.0.1:5173", "Idempotency-Key": "no-authority"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json["code"], "desktop_auth_required")

    def test_authenticated_write_requires_idempotency_key(self) -> None:
        response = self.client.post(
            "/v1/assets/import",
            json={
                "db_path": str(self.app.extensions["media_repository"].db_path),
                "relative_paths": [],
                "recursive": False,
            },
            headers={DESKTOP_TOKEN_HEADER: self.token},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Idempotency-Key", response.json["message"])


class StartupRecoveryContractTests(unittest.TestCase):
    def test_startup_interrupts_queued_work_and_lists_only_public_jobs_from_current_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-media-recovery-") as temporary:
            root = Path(temporary).resolve()
            repository, library, _ = _initialize_repository(root)
            root_id = str(repository.library_roots()[0]["id"])
            video = _register_bytes(
                repository,
                root_id,
                library,
                "queued-video.mp4",
                content=b"queued video bytes never sent to a worker",
            )
            media_job = repository.create_analysis_job(asset_id=str(video["id"]))

            image = _register_bytes(
                repository,
                root_id,
                library,
                "timeline-image.jpg",
                content=b"local image fixture bytes",
                kind="image",
            )
            repository.update_image_probe(str(image["id"]), width=640, height=360)
            project = repository.create_project(
                "Recovery fixture",
                {
                    "schema_version": "1",
                    "goal": "Recovery fixture",
                    "duration_ms": 1_000,
                    "aspect_ratio": "16:9",
                    "candidate_refs": [
                        {
                            "id": image["id"],
                            "asset_id": image["id"],
                            "asset_source_id": image["asset_source_id"],
                            "result_type": "image_asset",
                        }
                    ],
                },
                {"created_by": "test", "external_model": False},
            )
            timeline = TimelineService(repository).create_from_project(str(project["id"]))
            preview_root = repository.register_preview_root(root / "state" / "media-cache" / "previews")
            render_job = repository.create_render_job(
                timeline_id=str(timeline["timeline"]["id"]),
                timeline_revision=1,
                profile="preview-low",
                output_root_id=str(preview_root["id"]),
                output_relative_path="queued-render.mp4",
                timeline_content_sha256=str(timeline["content_sha256"]),
            )
            self.assertEqual(repository.active_job_count(), 2)

            other_root = root / "other-database"
            other_repository, other_library, _ = _initialize_repository(other_root)
            other_root_id = str(other_repository.library_roots()[0]["id"])
            other_video = _register_bytes(
                other_repository,
                other_root_id,
                other_library,
                "other.mp4",
                content=b"job from another database",
            )
            other_job = other_repository.create_analysis_job(asset_id=str(other_video["id"]))

            token = "startup-recovery-desktop-token"
            environment = _test_environment(root, library, token)
            environment.start()
            app = create_app(Settings.from_env())
            client = app.test_client()
            try:
                recovered_repository = app.extensions["media_repository"]
                recovered_media = recovered_repository.get_media_job(str(media_job["id"]))
                recovered_render = recovered_repository.get_render_job(str(render_job["id"]))
                assert recovered_media is not None and recovered_render is not None
                self.assertEqual(recovered_media["status"], "interrupted")
                self.assertEqual(recovered_media["error"]["code"], "process_interrupted")
                self.assertEqual(recovered_render["status"], "interrupted")
                self.assertEqual(recovered_render["error"]["code"], "process_interrupted")
                self.assertEqual(recovered_repository.active_job_count(), 0)
                with recovered_repository._connect() as connection:
                    analysis_status = connection.execute(
                        "SELECT status FROM analysis_runs WHERE id=?",
                        (media_job["analysis_run_id"],),
                    ).fetchone()[0]
                self.assertEqual(analysis_status, "interrupted")

                media_list = client.get("/v1/index/jobs", query_string={"active": "true", "limit": 50})
                render_list = client.get("/v1/renders", query_string={"active": "true", "limit": 50})
                self.assertEqual(media_list.status_code, 200)
                self.assertEqual(render_list.status_code, 200)
                self.assertEqual(media_list.json["object"], "media.job.list")
                self.assertEqual(render_list.json["object"], "render.job.list")
                self.assertEqual([job["id"] for job in media_list.json["jobs"]], [media_job["id"]])
                self.assertEqual([job["id"] for job in render_list.json["jobs"]], [render_job["id"]])

                public_payload = {"media": media_list.json, "renders": render_list.json}
                serialized = json.dumps(public_payload, sort_keys=True)
                self.assertNotIn(str(root), serialized)
                self.assertNotIn(str(other_job["id"]), serialized)
                self.assertNotIn(repository.database_uuid, serialized)
                forbidden_keys = {
                    "canonical_path",
                    "root_path",
                    "database_uuid",
                    "ffmpeg_command",
                    "checkpoint",
                    "stderr_tail",
                }

                def keys(value: object) -> set[str]:
                    if isinstance(value, dict):
                        return set(value) | set().union(*(keys(item) for item in value.values()))
                    if isinstance(value, list):
                        return set().union(*(keys(item) for item in value)) if value else set()
                    return set()

                self.assertFalse(keys(public_payload) & forbidden_keys)
            finally:
                _shutdown_app(app)
                environment.stop()


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and ffprobe are required for the real video pipeline",
)
class RealVideoPipelineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-real-video-")
        cls.root = Path(cls.temporary_directory.name).resolve()
        cls.demo_library = cls.root / "demo-library"
        subprocess.run(
            [sys.executable, str(DEMO_SCRIPT), "--output", str(cls.demo_library)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def test_synthetic_mp4_import_index_timeline_revision_and_preview_render(self) -> None:
        state = self.root / "pipeline-state"
        state.mkdir()
        db_path = state / "media.db"
        ImageIndexRepository(db_path).ensure_schema()
        repository = MediaRepository(db_path)
        repository.ensure_schema(self.demo_library)
        root_id = str(repository.library_roots()[0]["id"])
        video_path = self.demo_library / "demo_mountain_to_coast.mp4"
        source_hash_before = sha256_file(video_path)
        video_stat = video_path.stat()
        imported = repository.upsert_asset_source(
            root_id=root_id,
            relative_path=video_path.name,
            filename=video_path.name,
            kind="video",
            sha256=source_hash_before,
            mime_type="video/mp4",
            file_size=video_stat.st_size,
            mtime_ns=video_stat.st_mtime_ns,
            source_file_id=str(video_stat.st_ino),
        )
        subtitle = video_path.with_suffix(".srt")
        subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:03,000\nMountain morning\n\n"
            "2\n00:00:06,000 --> 00:00:09,000\nCoastal golden hour\n",
            encoding="utf-8",
        )

        media_job = repository.create_analysis_job(asset_id=str(imported["id"]))
        media_runner = MediaJobRunner(repository, state / "media-cache")
        try:
            media_runner.submit(str(media_job["id"]))
            completed = _wait_for_job(repository.get_media_job, str(media_job["id"]), timeout=120)
        finally:
            media_runner.shutdown()

        self.assertEqual(completed["status"], "succeeded", completed.get("error"))
        asset = repository.get_asset(str(imported["id"]))
        assert asset is not None
        self.assertEqual(asset["probe_status"], "ready")
        self.assertAlmostEqual(int(asset["duration_ms"]), 9_000, delta=150)
        search_service = MixedRetrievalService(repository)
        image_path = self.demo_library / "2022-04-18_quiet_mountain_sunrise.jpg"
        image_stat = image_path.stat()
        image_asset = repository.upsert_asset_source(
            root_id=root_id,
            relative_path=image_path.name,
            filename=image_path.name,
            kind="image",
            sha256=sha256_file(image_path),
            mime_type="image/jpeg",
            file_size=image_stat.st_size,
            mtime_ns=image_stat.st_mtime_ns,
            source_file_id=str(image_stat.st_ino),
        )
        repository.update_image_probe(str(image_asset["id"]), width=1_440, height=960)
        mixed = search_service.search(
            {"query": "mountain", "types": ["image", "video"], "top_k": 100}
        )
        self.assertEqual(
            {result["result_type"] for result in mixed["results"]},
            {"image_asset", "video_segment"},
        )
        search = search_service.search({"query": "Coastal golden hour", "types": ["video"], "top_k": 10})
        self.assertEqual(search["status"], "succeeded")
        self.assertEqual(search["retrieval_mode"], "lexical_local_fallback")
        self.assertTrue(search["results"])
        first_match = search["results"][0]
        self.assertEqual(first_match["result_type"], "video_segment")
        segment = repository.get_segment(str(first_match["id"]))
        assert segment is not None
        self.assertTrue(segment["keyframes"])
        self.assertTrue(segment["transcripts"])

        director = CreativeDirector(repository, search_service)
        project, _ = director.create_brief(
            {
                "title": "Synthetic coast story",
                "goal": "Coastal golden hour",
                "duration_ms": min(3_000, int(segment["end_ms"]) - int(segment["start_ms"])),
                "aspect_ratio": "16:9",
                "candidate_refs": [str(first_match["id"])],
            }
        )
        timeline_service = TimelineService(repository)
        initial = timeline_service.create_from_project(str(project["id"]))
        timeline = initial["timeline"]
        self.assertTrue(initial["validation"]["valid"])
        clip = timeline["tracks"][0]["clips"][0]
        duration = int(clip["timeline_duration_ms"])
        revised_duration = max(1, duration - min(250, max(1, duration // 2)))
        operations = [
            {
                "op": "trim_clip",
                "clip_id": clip["id"],
                "source_in_ms": clip["source_in_ms"],
                "source_out_ms": int(clip["source_in_ms"]) + revised_duration,
                "preconditions": {"timeline_revision": 1},
            }
        ]
        preview = timeline_service.preview_revision(str(timeline["id"]), base_revision=1, operations=operations)
        self.assertEqual(preview["timeline"]["revision"], 2)
        self.assertTrue(preview["validation"]["valid"])
        revised = timeline_service.revise(str(timeline["id"]), base_revision=1, operations=operations)
        self.assertEqual(revised["timeline"]["revision"], 2)
        self.assertEqual(repository.timeline_revisions(str(timeline["id"]))[0]["revision"], 2)
        with self.assertRaisesRegex(RuntimeError, "revision_conflict"):
            timeline_service.preview_revision(str(timeline["id"]), base_revision=1, operations=operations)

        invalid = copy.deepcopy(revised["timeline"])
        invalid["tracks"][0]["clips"][0]["fit"] = "unsafe-filter-expression"
        validation = timeline_service.validate(invalid)
        self.assertFalse(validation["valid"])
        self.assertIn("unsupported_fit", {error["code"] for error in validation["errors"]})

        preview_root = repository.register_preview_root(state / "previews")
        with self.assertRaisesRegex(ValueError, "content hash"):
            repository.create_render_job(
                timeline_id=str(timeline["id"]),
                timeline_revision=2,
                profile="preview-low",
                output_root_id=str(preview_root["id"]),
                output_relative_path="wrong-hash.mp4",
                timeline_content_sha256="0" * 64,
            )
        render_job = repository.create_render_job(
            timeline_id=str(timeline["id"]),
            timeline_revision=2,
            profile="preview-low",
            output_root_id=str(preview_root["id"]),
            output_relative_path="synthetic-preview.mp4",
            timeline_content_sha256=str(revised["content_sha256"]),
        )
        render_runner = RenderJobRunner(repository, state / "media-cache")
        try:
            render_runner.submit(str(render_job["id"]))
            rendered = _wait_for_job(repository.get_render_job, str(render_job["id"]), timeout=120)
        finally:
            render_runner.shutdown()

        self.assertEqual(rendered["status"], "succeeded", rendered.get("error"))
        resolved = repository.resolve_render_artifact(str(render_job["id"]))
        self.assertIsNotNone(resolved)
        assert resolved is not None
        stored_job, artifact = resolved
        self.assertEqual(stored_job["output_sha256"], sha256_path(artifact))
        # Container duration is quantized to the 30 fps timeline time base.
        self.assertAlmostEqual(ffprobe(artifact)["duration_ms"], revised_duration, delta=50)
        self.assertFalse(Path(str(stored_job["ffmpeg_command"][0])).is_absolute())
        self.assertNotIn(str(video_path), " ".join(stored_job["ffmpeg_command"]))
        self.assertEqual(sha256_file(video_path), source_hash_before)

        transition_duration = max(1, min(500, revised_duration // 2))
        with self.assertRaisesRegex(ValueError, "unsupported_timeline_feature"):
            timeline_service.preview_revision(
                str(timeline["id"]),
                base_revision=2,
                operations=[
                    {
                        "op": "add_transition",
                        "transition_id": "fade-to-black-test",
                        "type": "fade_to_black",
                        "from_clip_id": clip["id"],
                        "to_clip_id": None,
                        "duration_ms": transition_duration,
                        "preconditions": {"timeline_revision": 2},
                    }
                ],
            )
        self.assertEqual(repository.get_timeline(str(timeline["id"]))["revision"], 2)
        transition_timeline = copy.deepcopy(revised["timeline"])
        transition_timeline["revision"] = 3
        transition_timeline["transitions"] = [
            {
                "id": "fade-to-black-test",
                "type": "fade_to_black",
                "from_clip_id": transition_timeline["tracks"][0]["clips"][0]["id"],
                "to_clip_id": None,
                "duration_ms": transition_duration,
            }
        ]
        legacy_transition_validation = timeline_service.validate(transition_timeline)
        self.assertFalse(legacy_transition_validation["valid"])
        self.assertIn(
            "unsupported_render_transition",
            {error["code"] for error in legacy_transition_validation["errors"]},
        )
        with_transition = repository.save_timeline(
            timeline_id=str(timeline["id"]),
            project_id=str(timeline["project_id"]),
            revision=3,
            timeline=transition_timeline,
            provenance=transition_timeline["provenance"],
            validation_status="valid",
        )
        transition_job = repository.create_render_job(
            timeline_id=str(timeline["id"]),
            timeline_revision=3,
            profile="preview-low",
            output_root_id=str(preview_root["id"]),
            output_relative_path="unsupported-transition.mp4",
            timeline_content_sha256=str(with_transition["content_sha256"]),
        )
        transition_result = _execute_render_job(repository, state / "media-cache", transition_job)
        self.assertEqual(transition_result["status"], "failed")
        self.assertEqual(transition_result["error"]["code"], "invalid_timeline")
        self.assertIn(
            ("unsupported_render_transition", "/transitions/0"),
            {
                (detail["code"], detail["pointer"])
                for detail in transition_result["error"]["details"]
            },
        )
        self.assertFalse((state / "previews" / "unsupported-transition.mp4").exists())

        symlink_root_path = state / "symlink-preview-root"
        symlink_root = repository.register_preview_root(symlink_root_path)
        symlink_job = repository.create_render_job(
            timeline_id=str(timeline["id"]),
            timeline_revision=2,
            profile="preview-low",
            output_root_id=str(symlink_root["id"]),
            output_relative_path="must-not-escape.mp4",
            timeline_content_sha256=str(revised["content_sha256"]),
        )
        held_root = state / "held-preview-root"
        escaped_root = state / "escaped-preview-root"
        escaped_root.mkdir()
        symlink_root_path.rename(held_root)
        symlink_root_path.symlink_to(escaped_root, target_is_directory=True)
        symlink_result = _execute_render_job(repository, state / "media-cache", symlink_job)
        self.assertEqual(symlink_result["status"], "failed")
        self.assertEqual(symlink_result["error"]["code"], "output_root_changed")
        self.assertFalse((escaped_root / "must-not-escape.mp4").exists())

        replaced_root_path = state / "replaced-preview-root"
        replaced_root = repository.register_preview_root(replaced_root_path)
        replaced_job = repository.create_render_job(
            timeline_id=str(timeline["id"]),
            timeline_revision=2,
            profile="preview-low",
            output_root_id=str(replaced_root["id"]),
            output_relative_path="must-not-write.mp4",
            timeline_content_sha256=str(revised["content_sha256"]),
        )
        replaced_root_path.rmdir()
        replaced_root_path.mkdir()
        replaced_result = _execute_render_job(repository, state / "media-cache", replaced_job)
        self.assertEqual(replaced_result["status"], "failed")
        self.assertEqual(replaced_result["error"]["code"], "output_root_changed")
        self.assertFalse((replaced_root_path / "must-not-write.mp4").exists())

        base = repository.get_timeline(str(timeline["id"]), 3)
        assert base is not None
        barrier = Barrier(2)

        def save_competing_revision(volume_db: int) -> tuple[str, object]:
            candidate = copy.deepcopy(base["timeline"])
            candidate["revision"] = 4
            candidate["tracks"][0]["clips"][0]["volume_db"] = volume_db
            barrier.wait(timeout=5)
            try:
                saved = repository.save_timeline_revision_cas(
                    timeline_id=str(timeline["id"]),
                    project_id=str(timeline["project_id"]),
                    base_revision=3,
                    expected_base_sha256=str(base["content_sha256"]),
                    timeline=candidate,
                    provenance=candidate["provenance"],
                    validation_status="valid",
                )
                return "saved", saved["revision"]
            except RuntimeError as exc:
                return "conflict", str(exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(save_competing_revision, (-2, -6)))
        self.assertEqual(sorted(status for status, _ in outcomes), ["conflict", "saved"])
        self.assertEqual(next(value for status, value in outcomes if status == "saved"), 4)
        self.assertEqual(
            next(value for status, value in outcomes if status == "conflict"),
            "revision_conflict:4",
        )
        self.assertEqual([row["revision"] for row in repository.timeline_revisions(str(timeline["id"]))], [4, 3, 2, 1])

    def test_authenticated_api_import_to_render_and_range_download(self) -> None:
        root = self.root / "api-pipeline"
        token = "real-api-desktop-token"
        source_video = self.demo_library / "demo_vertical_city_story.mp4"
        library = root / "library"
        library.mkdir(parents=True)
        copied_video = library / source_video.name
        shutil.copyfile(source_video, copied_video)
        source_hash_before = sha256_file(copied_video)
        environment = _test_environment(root, library, token)
        environment.start()
        app = create_app(Settings.from_env())
        client = app.test_client()
        auth = {DESKTOP_TOKEN_HEADER: token}
        db_path = str(app.extensions["media_repository"].db_path)
        try:
            capabilities = client.get("/v1/media/capabilities", headers=auth)
            self.assertEqual(capabilities.status_code, 200)
            self.assertEqual(capabilities.json["status"], "ready")
            self.assertTrue(capabilities.json["write_requires_desktop_token"])

            import_payload = {
                "db_path": db_path,
                "relative_paths": [copied_video.name],
                "recursive": False,
            }
            import_headers = {**auth, "Idempotency-Key": "real-import-1"}
            imported = client.post(
                "/v1/assets/import",
                json=import_payload,
                headers=import_headers,
            )
            self.assertEqual(imported.status_code, 202, imported.json)
            self.assertEqual(imported.json["status"], "queued")
            job_id = imported.json["job_id"]
            replay = client.post(
                "/v1/assets/import",
                json=import_payload,
                headers=import_headers,
            )
            self.assertEqual(replay.status_code, 202)
            self.assertEqual(replay.json, imported.json)
            conflict = client.post(
                "/v1/assets/import",
                json={
                    "db_path": db_path,
                    "relative_paths": [copied_video.name],
                    "recursive": True,
                },
                headers=import_headers,
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json["code"], "idempotency_conflict")

            completed = _wait_for_job(
                lambda identifier: client.get(
                    f"/v1/index/jobs/{identifier}", headers=auth
                ).json["job"],
                job_id,
                timeout=120,
            )
            self.assertEqual(completed["status"], "succeeded", completed.get("error"))

            search = client.post(
                "/v1/search/mixed",
                json={"query": "vertical city story", "types": ["video"], "top_k": 5},
                headers=auth,
            )
            self.assertEqual(search.status_code, 200)
            self.assertTrue(search.json["results"])
            match = search.json["results"][0]

            source_range = client.get(
                f"/v1/assets/{match['asset_id']}/media",
                headers={**auth, "Range": "bytes=0-127"},
            )
            self.assertEqual(source_range.status_code, 206)
            self.assertEqual(len(source_range.data), 128)
            self.assertEqual(source_range.headers["Accept-Ranges"], "bytes")
            source_range.close()

            brief = client.post(
                "/v1/creative/briefs",
                json={
                    "db_path": db_path,
                    "title": "Vertical city cut",
                    "goal": "vertical city story",
                    "duration_ms": 1_000,
                    "aspect_ratio": "9:16",
                    "candidate_refs": [match["id"]],
                },
                headers={**auth, "Idempotency-Key": "real-brief-1"},
            )
            self.assertEqual(brief.status_code, 201, brief.json)
            project_id = brief.json["project"]["id"]
            timeline_response = client.post(
                f"/v1/creative/projects/{project_id}/timelines",
                json={"db_path": db_path, "brief_revision": 1},
                headers={**auth, "Idempotency-Key": "real-timeline-1"},
            )
            self.assertEqual(timeline_response.status_code, 201, timeline_response.json)
            timeline = timeline_response.json["timeline"]
            validation = client.post(
                f"/v1/timelines/{timeline['id']}/validate",
                json={"revision": 1},
                headers=auth,
            )
            self.assertEqual(validation.status_code, 200)
            self.assertTrue(validation.json["valid"])

            clip = timeline["tracks"][0]["clips"][0]
            preview_revision = client.post(
                f"/v1/timelines/{timeline['id']}/revise",
                json={
                    "db_path": db_path,
                    "base_revision": 1,
                    "apply": False,
                    "operations": [
                        {
                            "op": "set_volume",
                            "clip_id": clip["id"],
                            "volume_db": -3,
                        }
                    ],
                },
                headers=auth,
            )
            self.assertEqual(preview_revision.status_code, 200, preview_revision.json)
            self.assertEqual(preview_revision.json["object"], "timeline.revision_preview")
            self.assertEqual(preview_revision.json["preview"]["timeline"]["revision"], 2)
            self.assertTrue(preview_revision.json["diff"])

            applied = client.post(
                f"/v1/timelines/{timeline['id']}/revise",
                json={
                    "db_path": db_path,
                    "base_revision": 1,
                    "apply": True,
                    "operations": [
                        {
                            "op": "set_volume",
                            "clip_id": clip["id"],
                            "volume_db": -3,
                        }
                    ],
                },
                headers={**auth, "Idempotency-Key": "real-revision-1"},
            )
            self.assertEqual(applied.status_code, 201, applied.json)
            self.assertEqual(applied.json["timeline"]["revision"], 2)

            render_payload = {
                "db_path": db_path,
                "timeline_id": timeline["id"],
                "timeline_revision": 2,
                "expected_timeline_sha256": applied.json["content_sha256"],
                "profile": "preview-low",
                "output": {"root_id": "app-preview-root"},
            }
            wrong_hash_payload = {**render_payload, "expected_timeline_sha256": "0" * 64}
            wrong_hash_headers = {**auth, "Idempotency-Key": "real-render-wrong-hash"}
            wrong_hash = client.post(
                "/v1/renders",
                json=wrong_hash_payload,
                headers=wrong_hash_headers,
            )
            self.assertEqual(wrong_hash.status_code, 409)
            self.assertEqual(wrong_hash.json["code"], "timeline_hash_mismatch")
            wrong_hash_replay = client.post(
                "/v1/renders",
                json=wrong_hash_payload,
                headers=wrong_hash_headers,
            )
            self.assertEqual(wrong_hash_replay.status_code, 409)
            self.assertEqual(wrong_hash_replay.json, wrong_hash.json)

            render = client.post(
                "/v1/renders",
                json=render_payload,
                headers={**auth, "Idempotency-Key": "real-render-1"},
            )
            self.assertEqual(render.status_code, 202, render.json)
            render_id = render.json["id"]
            rendered = _wait_for_job(
                lambda identifier: client.get(
                    f"/v1/renders/{identifier}", headers=auth
                ).json["job"],
                render_id,
                timeout=120,
            )
            self.assertEqual(rendered["status"], "succeeded", rendered.get("error"))
            self.assertNotIn("ffmpeg_command", rendered)
            self.assertNotIn("database_uuid", rendered)
            artifact_range = client.get(
                f"/v1/renders/{render_id}/download",
                headers={**auth, "Range": "bytes=0-255"},
            )
            self.assertEqual(artifact_range.status_code, 206)
            self.assertEqual(len(artifact_range.data), 256)
            self.assertEqual(artifact_range.headers["Accept-Ranges"], "bytes")
            artifact_range.close()
            self.assertEqual(sha256_file(copied_video), source_hash_before)
        finally:
            _shutdown_app(app)
            environment.stop()


if __name__ == "__main__":
    unittest.main()
