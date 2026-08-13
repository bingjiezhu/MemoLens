from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.src import DESKTOP_TOKEN_HEADER, create_app
from backend.src.media.director import CreativeDirector
from backend.src.media.timeline import TimelineService
from core.db import ImageIndexRepository
from core.config import Settings
from core.media_db import MediaRepository, canonical_json


class _FixtureRetrieval:
    def __init__(self, candidate: dict[str, object]):
        self.candidate = candidate

    def search(self, _payload: dict[str, object]) -> dict[str, object]:
        return {
            "object": "mixed.search",
            "schema_version": "1",
            "results": [self.candidate],
            "search_revision": "fixture-search",
            "analysis_heads": {},
        }


class AtomicMediaWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-atomic-media-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)

        content = b"atomic-media-image"
        image = self.library / "memory.jpg"
        image.write_bytes(content)
        metadata = image.stat()
        root_id = str(self.repository.library_roots()[0]["id"])
        self.asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=image.name,
            filename=image.name,
            kind="image",
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type="image/jpeg",
            file_size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            source_file_id=str(metadata.st_ino),
        )
        self.repository.update_image_probe(str(self.asset["id"]), width=640, height=480)
        self.candidate = {
            "id": str(self.asset["id"]),
            "asset_id": str(self.asset["id"]),
            "result_type": "image_asset",
            "kind": "image",
            "score": 1.0,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_domain_write_rolls_back_if_response_freeze_fails(self) -> None:
        def mutation(connection: sqlite3.Connection):
            connection.execute("INSERT INTO creative_projects VALUES('proj_fault','Fault','active','now','now')")
            return {"id": "proj_fault"}, 201, "proj_fault"

        with patch.object(
            self.repository,
            "_idempotency_store_success",
            side_effect=RuntimeError("injected_freeze_failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected_freeze_failure"):
                self.repository.execute_idempotent_write(
                    scope="desktop:test:fault",
                    key="fault-key",
                    request_sha256="a" * 64,
                    resource_type="creative_project",
                    mutation=mutation,
                )

        with self.repository._connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_projects WHERE id='proj_fault'").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM idempotency_records WHERE key='fault-key'").fetchone()[0],
                0,
            )

    def test_brief_lost_response_replays_exactly_and_creates_one_project(self) -> None:
        director = CreativeDirector(
            self.repository,
            _FixtureRetrieval(self.candidate),  # type: ignore[arg-type]
        )
        payload = {"goal": "A calm memory", "duration_ms": 3_000}
        request_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        first = director.create_brief_idempotent(
            payload,
            idempotency_scope="desktop:POST:/v1/creative/briefs",
            idempotency_key="brief-lost-response",
            request_sha256=request_hash,
        )
        replay = director.create_brief_idempotent(
            payload,
            idempotency_scope="desktop:POST:/v1/creative/briefs",
            idempotency_key="brief-lost-response",
            request_sha256=request_hash,
        )

        self.assertEqual(first.response_status, 201)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response_status, 201)
        self.assertEqual(replay.response, first.response)
        with self.repository._connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0],
                1,
            )

    def test_timeline_revision_replay_wins_before_cas_conflict(self) -> None:
        project = self.repository.create_project(
            "Timeline",
            {
                "duration_ms": 3_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [self.candidate],
            },
            {"created_by": "fixture"},
        )
        service = TimelineService(self.repository)
        created = service.create_from_project(str(project["id"]))
        timeline_id = str(created["timeline"]["id"])
        clip_id = str(created["timeline"]["tracks"][0]["clips"][0]["id"])
        operations = service.normalize_operations(
            timeline_id,
            1,
            [{"op": "set_volume", "clip_id": clip_id, "volume_db": -3}],
        )
        payload = {"base_revision": 1, "operations": operations, "apply": True}
        request_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        first = service.revise_idempotent(
            timeline_id,
            base_revision=1,
            operations=operations,
            idempotency_scope=f"desktop:POST:/v1/timelines/{timeline_id}/revise",
            idempotency_key="revision-lost-response",
            request_sha256=request_hash,
        )
        replay = service.revise_idempotent(
            timeline_id,
            base_revision=1,
            operations=operations,
            idempotency_scope=f"desktop:POST:/v1/timelines/{timeline_id}/revise",
            idempotency_key="revision-lost-response",
            request_sha256=request_hash,
        )

        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(
            [row["revision"] for row in self.repository.timeline_revisions(timeline_id)],
            [2, 1],
        )

    def test_resume_replay_increments_attempt_once_and_cancel_replay_mutates_once(self) -> None:
        video_content = b"fixture-video"
        video = self.library / "clip.mp4"
        video.write_bytes(video_content)
        metadata = video.stat()
        root_id = str(self.repository.library_roots()[0]["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=video.name,
            filename=video.name,
            kind="video",
            sha256=hashlib.sha256(video_content).hexdigest(),
            mime_type="video/mp4",
            file_size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            source_file_id=str(metadata.st_ino),
        )
        job = self.repository.create_analysis_job(asset_id=str(asset["id"]))
        job_id = str(job["id"])
        self.repository.update_media_job(
            job_id,
            status="interrupted",
            stage="interrupted",
            finished=True,
        )

        def resume(connection: sqlite3.Connection):
            self.assertTrue(self.repository.reset_media_job_for_resume(job_id, connection=connection))
            current = self.repository.get_media_job_in_transaction(connection, job_id)
            return {"job": current}, 202, job_id

        resume_args = {
            "scope": f"desktop:POST:/v1/index/jobs/{job_id}/resume",
            "key": "resume-once",
            "request_sha256": "b" * 64,
            "resource_type": "media_job_resume",
            "mutation": resume,
        }
        first_resume = self.repository.execute_idempotent_write(**resume_args)
        replay_resume = self.repository.execute_idempotent_write(**resume_args)
        self.assertEqual(replay_resume.response, first_resume.response)
        self.assertEqual(self.repository.get_media_job(job_id)["attempt"], 2)

        def cancel(connection: sqlite3.Connection):
            self.assertTrue(self.repository.request_media_job_cancel(job_id, connection=connection))
            current = self.repository.get_media_job_in_transaction(connection, job_id)
            return {"job": current}, 202, job_id

        cancel_args = {
            "scope": f"desktop:POST:/v1/index/jobs/{job_id}/cancel",
            "key": "cancel-once",
            "request_sha256": "c" * 64,
            "resource_type": "media_job_cancel",
            "mutation": cancel,
        }
        first_cancel = self.repository.execute_idempotent_write(**cancel_args)
        replay_cancel = self.repository.execute_idempotent_write(**cancel_args)
        self.assertEqual(replay_cancel.response, first_cancel.response)
        current = self.repository.get_media_job(job_id)
        self.assertEqual(current["attempt"], 2)
        self.assertTrue(current["cancel_requested"])
        self.assertEqual(current["status"], "cancelled")

    def test_render_job_uses_real_id_for_filename_without_follow_up_update(self) -> None:
        project = self.repository.create_project(
            "Render",
            {
                "duration_ms": 3_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [self.candidate],
            },
            {"created_by": "fixture"},
        )
        timeline = TimelineService(self.repository).create_from_project(str(project["id"]))
        preview_root = self.repository.register_preview_root(self.root / "preview")
        job = self.repository.create_render_job(
            timeline_id=str(timeline["timeline"]["id"]),
            timeline_revision=1,
            profile="preview-low",
            output_root_id=str(preview_root["id"]),
            output_relative_path=None,
            timeline_content_sha256=str(timeline["content_sha256"]),
        )
        self.assertEqual(job["output_relative_path"], f"{job['id']}.mp4")

    def test_render_route_replays_before_current_timeline_or_root_validation(self) -> None:
        project = self.repository.create_project(
            "Render replay",
            {
                "duration_ms": 3_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [self.candidate],
            },
            {"created_by": "fixture"},
        )
        timeline = TimelineService(self.repository).create_from_project(str(project["id"]))
        token = "atomic-render-token"
        environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(Path(__file__).resolve().parents[1] / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": token,
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
            },
            clear=False,
        )
        environment.start()
        app = create_app(Settings.from_env())
        client = app.test_client()
        repository = app.extensions["media_repository"]
        runner = app.extensions["render_job_runner"]
        runner.submit = lambda _job_id: None
        payload = {
            "db_path": str(repository.db_path),
            "timeline_id": str(timeline["timeline"]["id"]),
            "timeline_revision": 1,
            "expected_timeline_sha256": str(timeline["content_sha256"]),
            "profile": "preview-low",
            "output": {"root_id": "app-preview-root"},
        }
        headers = {
            DESKTOP_TOKEN_HEADER: token,
            "Idempotency-Key": "render-lost-response",
        }
        try:
            first = client.post("/v1/renders", json=payload, headers=headers)
            self.assertEqual(first.status_code, 202, first.json)
            with repository.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE timelines SET content_sha256=? WHERE id=?",
                    ("0" * 64, timeline["timeline"]["id"]),
                )
                connection.execute(
                    "UPDATE output_roots SET status='revoked' WHERE id=?",
                    (app.extensions["app_preview_root_id"],),
                )
            replay = client.post("/v1/renders", json=payload, headers=headers)
            self.assertEqual(replay.status_code, 202)
            self.assertEqual(replay.json, first.json)
        finally:
            runner.shutdown()
            app.extensions["media_job_runner"].shutdown()
            environment.stop()


if __name__ == "__main__":
    unittest.main()
