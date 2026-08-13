from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from PIL import Image

from backend.src.api.routes import api_blueprint
from backend.src.media.importing import MediaImportService
from core.db import ImageIndexRepository
from core.media_db import MediaRepository


class RecordingJobRunner:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.submit_attempts: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submit_attempts.append(job_id)
        # Match MediaJobRunner's process-local job-id deduplication contract.
        if job_id not in self.submitted:
            self.submitted.append(job_id)


class FailOnceJobRunner(RecordingJobRunner):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def submit(self, job_id: str) -> None:
        self.submit_attempts.append(job_id)
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated process exit before job dispatch")
        if job_id not in self.submitted:
            self.submitted.append(job_id)


class MediaImportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-import-service-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        db_path = self.root / "state" / "media.db"
        db_path.parent.mkdir()
        ImageIndexRepository(db_path).ensure_schema()
        self.repository = MediaRepository(db_path)
        self.repository.ensure_schema(self.library)
        self.root_id = str(self.repository.library_roots()[0]["id"])
        self.runner = RecordingJobRunner()
        self.service = MediaImportService(self.repository, self.runner)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dry_run_reports_actions_without_writing_or_scheduling(self) -> None:
        image_path = self.library / "still.jpg"
        Image.new("RGB", (32, 18), "red").save(image_path)
        video_path = self.library / "clip.mp4"
        video_path.write_bytes(b"not-probed-during-import")

        result = self.service.import_assets(
            root_id=self.root_id,
            root=self.library,
            payload={
                "relative_paths": [image_path.name, video_path.name],
                "recursive": False,
                "dry_run": True,
            },
        )

        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.kinds, ["image", "video"])
        self.assertEqual(result.imported, 2)
        self.assertEqual(result.skipped, 0)
        self.assertEqual([asset["action"] for asset in result.assets], ["imported", "imported"])
        self.assertTrue(all("id" not in asset for asset in result.assets))
        self.assertEqual(result.jobs, [])
        self.assertEqual(result.rejected, [])
        self.assertEqual(self.runner.submitted, [])
        self.assertIsNone(self.repository.get_asset_source(self.repository.source_id(self.root_id, image_path.name)))

    def test_prepare_is_filesystem_only_and_never_dispatches(self) -> None:
        image_path = self.library / "prepared.jpg"
        Image.new("RGB", (24, 12), "purple").save(image_path)
        video_path = self.library / "prepared.mp4"
        video_path.write_bytes(b"prepared-video")
        with sqlite3.connect(self.repository.db_path) as connection:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("assets", "asset_sources", "analysis_runs", "media_jobs")
            }

        plan = self.service.prepare_import(
            root=self.library,
            payload={
                "relative_paths": [image_path.name, video_path.name],
                "recursive": False,
            },
        )

        self.assertEqual(len(plan.assets), 2)
        with sqlite3.connect(self.repository.db_path) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("assets", "asset_sources", "analysis_runs", "media_jobs")
            }
        self.assertEqual(after, before)
        self.assertEqual(self.runner.submit_attempts, [])

    def test_imported_unchanged_rebound_and_invalid_image_results_are_stable(self) -> None:
        image_path = self.library / "still.jpg"
        Image.new("RGB", (32, 18), "red").save(image_path)
        payload = {"relative_paths": [image_path.name], "recursive": False}

        imported = self.service.import_assets(
            root_id=self.root_id,
            root=self.library,
            payload=payload,
        )
        self.assertEqual(imported.status, "succeeded")
        self.assertEqual(imported.imported, 1)
        self.assertEqual(imported.skipped, 0)
        self.assertEqual(imported.assets[0]["action"], "imported")
        self.assertEqual(imported.assets[0]["probe_status"], "ready")
        stored = self.repository.get_asset(str(imported.assets[0]["id"]))
        assert stored is not None
        self.assertEqual((stored["width"], stored["height"]), (32, 18))

        unchanged = self.service.import_assets(
            root_id=self.root_id,
            root=self.library,
            payload=payload,
        )
        self.assertEqual(unchanged.assets[0]["action"], "unchanged")
        self.assertEqual(unchanged.imported, 0)
        self.assertEqual(unchanged.skipped, 1)

        Image.new("RGB", (64, 36), "blue").save(image_path)
        rebound = self.service.import_assets(
            root_id=self.root_id,
            root=self.library,
            payload=payload,
        )
        self.assertEqual(rebound.assets[0]["action"], "rebound")
        self.assertEqual(rebound.imported, 1)
        self.assertNotEqual(rebound.assets[0]["id"], imported.assets[0]["id"])

        broken_path = self.library / "broken.jpg"
        broken_path.write_bytes(b"not-an-image")
        partial = self.service.import_assets(
            root_id=self.root_id,
            root=self.library,
            payload={"relative_paths": [broken_path.name], "recursive": False},
        )
        self.assertEqual(partial.status, "partial")
        self.assertEqual(partial.assets[0]["action"], "imported")
        self.assertEqual(partial.rejected[0]["relative_path"], broken_path.name)
        self.assertEqual(partial.rejected[0]["code"], "invalid_image")
        self.assertFalse(partial.rejected[0]["retryable"])
        failed = self.repository.get_asset(str(partial.assets[0]["id"]))
        assert failed is not None
        self.assertEqual(failed["probe_status"], "failed")
        self.assertEqual(failed["error_code"], "invalid_image")

    def test_unchanged_video_reuses_the_active_job(self) -> None:
        video_path = self.library / "clip.mp4"
        video_path.write_bytes(b"video-content")
        payload = {
            "relative_paths": [video_path.name],
            "recursive": False,
            "kinds": ["video"],
        }

        first = self.service.import_assets(root_id=self.root_id, root=self.library, payload=payload)
        second = self.service.import_assets(root_id=self.root_id, root=self.library, payload=payload)

        self.assertEqual(first.status, "queued")
        self.assertEqual(second.status, "queued")
        self.assertEqual(second.assets[0]["action"], "unchanged")
        self.assertEqual(second.jobs[0]["id"], first.jobs[0]["id"])
        self.assertTrue(second.jobs[0]["reused"])
        self.assertEqual(self.runner.submitted, [first.jobs[0]["id"]])
        self.assertEqual(self.runner.submit_attempts, [first.jobs[0]["id"]] * 2)

    def test_video_only_discovery_does_not_charge_photos_to_manifest_limit(self) -> None:
        for index in range(501):
            (self.library / f"photo-{index:04d}.jpg").write_bytes(b"image")
        video_path = self.library / "only-video.mp4"
        video_path.write_bytes(b"video")

        result = self.service.import_assets(
            root_id=self.root_id,
            root=self.library,
            payload={"recursive": False, "kinds": ["video"]},
        )

        self.assertEqual([asset["relative_path"] for asset in result.assets], [video_path.name])
        self.assertEqual(result.kinds, ["video"])
        self.assertEqual(len(result.jobs), 1)

        with self.assertRaisesRegex(
            ValueError,
            "import_manifest_too_large: too many supported files\\.",
        ):
            self.service.import_assets(
                root_id=self.root_id,
                root=self.library,
                payload={"recursive": False, "kinds": ["image"]},
            )

    def test_discovery_receives_the_existing_time_file_and_byte_budgets(self) -> None:
        service = MediaImportService(self.repository, self.runner, clock=lambda: 12.5)
        with patch("backend.src.media.importing.discover_media", return_value=[]) as discover:
            result = service.import_assets(
                root_id=self.root_id,
                root=self.library,
                payload={"recursive": False},
            )

        self.assertEqual(result.status, "succeeded")
        discover.assert_called_once_with(
            self.library,
            recursive=False,
            files=None,
            extensions=unittest.mock.ANY,
            max_files=500,
            max_total_bytes=20 * 1024 * 1024 * 1024,
            deadline=42.5,
        )
        self.assertEqual(
            discover.call_args.kwargs["extensions"],
            MediaImportService.supported_extensions(["image", "video"]),
        )

    def test_synchronous_deadline_and_validation_messages_are_stable(self) -> None:
        video_path = self.library / "clip.mp4"
        video_path.write_bytes(hashlib.sha256(b"video").digest())
        ticks = iter((10.0, 40.1))
        service = MediaImportService(self.repository, self.runner, clock=lambda: next(ticks))
        with patch("backend.src.media.importing.discover_media", return_value=[video_path]):
            with self.assertRaisesRegex(
                ValueError,
                "import_manifest_timeout: synchronous import exceeded 30 seconds; "
                "use smaller relative_paths batches\\.",
            ):
                service.import_assets(
                    root_id=self.root_id,
                    root=self.library,
                    payload={"relative_paths": [video_path.name], "recursive": False},
                )

        cases = (
            (
                {"recursive": 1},
                "`recursive` and `dry_run` must be booleans.",
            ),
            (
                {"relative_paths": []},
                "`relative_paths` must be a non-empty string array when set.",
            ),
            (
                {"kinds": ["audio"]},
                "`kinds` must be a non-empty array containing only image and/or video.",
            ),
        )
        for payload, message in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, message):
                self.service.import_assets(root_id=self.root_id, root=self.library, payload=payload)

    def test_route_keeps_http_status_public_jobs_and_idempotent_replay(self) -> None:
        app, client, auth = self._route_client(self.runner)

        image_path = self.library / "still.jpg"
        Image.new("RGB", (16, 9), "green").save(image_path)
        dry_run = client.post(
            "/v1/assets/import",
            json={
                "db_path": str(self.repository.db_path),
                "relative_paths": [image_path.name],
                "recursive": False,
                "dry_run": True,
            },
            headers={**auth, "Idempotency-Key": "dry-run"},
        )
        self.assertEqual(dry_run.status_code, 200)
        self.assertEqual(dry_run.json["status"], "dry_run")
        self.assertEqual(dry_run.json["assets"][0]["action"], "imported")
        self.assertEqual(dry_run.json["asset_ids"], [])

        video_path = self.library / "clip.mp4"
        video_path.write_bytes(b"route-video")
        video_payload = {
            "db_path": str(self.repository.db_path),
            "relative_paths": [video_path.name],
            "recursive": False,
            "kinds": ["video"],
        }
        queued = client.post(
            "/v1/assets/import",
            json=video_payload,
            headers={**auth, "Idempotency-Key": "queue-video"},
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.json["status"], "queued")
        self.assertEqual(queued.json["job_id"], queued.json["job"]["id"])
        self.assertNotIn("database_uuid", queued.json["job"])
        self.assertEqual(self.runner.submitted, [queued.json["job_id"]])

        # Exact replay is resolved before touching the source filesystem, and a
        # job already owned by a running worker is not dispatched again.
        self.repository.update_media_job(
            queued.json["job_id"],
            status="running",
            stage="probing",
        )
        video_path.unlink()
        replay = client.post(
            "/v1/assets/import",
            json=video_payload,
            headers={**auth, "Idempotency-Key": "queue-video"},
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, queued.json)
        self.assertEqual(self.runner.submitted, [queued.json["job_id"]])
        self.assertEqual(self.runner.submit_attempts, [queued.json["job_id"]])

        conflict = client.post(
            "/v1/assets/import",
            json={**video_payload, "recursive": True},
            headers={**auth, "Idempotency-Key": "queue-video"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["code"], "idempotency_conflict")

        with sqlite3.connect(self.repository.db_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM media_jobs WHERE asset_id=?",
                    (queued.json["asset_ids"][0],),
                ).fetchone()[0],
                1,
            )

    def test_route_rolls_back_domain_and_snapshot_when_apply_fails(self) -> None:
        _, client, auth = self._route_client(self.runner, testing=False)
        video_path = self.library / "rollback.mp4"
        video_path.write_bytes(b"rollback-video")
        payload = {
            "db_path": str(self.repository.db_path),
            "relative_paths": [video_path.name],
            "recursive": False,
            "kinds": ["video"],
        }
        original_apply = MediaImportService.apply_prepared

        def fail_after_apply(connection, *, root_id, plan):
            original_apply(connection, root_id=root_id, plan=plan)
            raise RuntimeError("simulated domain/store crash")

        with patch.object(MediaImportService, "apply_prepared", side_effect=fail_after_apply):
            response = client.post(
                "/v1/assets/import",
                json=payload,
                headers={**auth, "Idempotency-Key": "rollback-import"},
            )
        self.assertEqual(response.status_code, 500)
        source_id = self.repository.source_id(self.root_id, video_path.name)
        self.assertIsNone(self.repository.get_asset_source(source_id))
        with sqlite3.connect(self.repository.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotency_records WHERE scope=? AND key=?",
                    ("desktop:POST:/v1/assets/import", "rollback-import"),
                ).fetchone()[0],
                0,
            )

    def test_route_rolls_back_domain_when_snapshot_freeze_fails(self) -> None:
        _, client, auth = self._route_client(self.runner, testing=False)
        video_path = self.library / "freeze-failure.mp4"
        video_path.write_bytes(b"freeze-failure-video")
        payload = {
            "db_path": str(self.repository.db_path),
            "relative_paths": [video_path.name],
            "recursive": False,
            "kinds": ["video"],
        }
        with patch.object(
            MediaRepository,
            "_idempotency_store_success",
            side_effect=RuntimeError("simulated snapshot store crash"),
        ):
            response = client.post(
                "/v1/assets/import",
                json=payload,
                headers={**auth, "Idempotency-Key": "freeze-failure"},
            )
        self.assertEqual(response.status_code, 500)
        source_id = self.repository.source_id(self.root_id, video_path.name)
        self.assertIsNone(self.repository.get_asset_source(source_id))
        with sqlite3.connect(self.repository.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotency_records WHERE scope=? AND key=?",
                    ("desktop:POST:/v1/assets/import", "freeze-failure"),
                ).fetchone()[0],
                0,
            )

    def test_route_replay_recovers_commit_before_submit_without_duplicate_job(self) -> None:
        runner = FailOnceJobRunner()
        _, client, auth = self._route_client(runner, testing=False)
        video_path = self.library / "recover.mp4"
        video_path.write_bytes(b"recover-video")
        payload = {
            "db_path": str(self.repository.db_path),
            "relative_paths": [video_path.name],
            "recursive": False,
            "kinds": ["video"],
        }
        headers = {**auth, "Idempotency-Key": "recover-import"}

        lost_response = client.post("/v1/assets/import", json=payload, headers=headers)
        self.assertEqual(lost_response.status_code, 500)
        with sqlite3.connect(self.repository.db_path) as connection:
            row = connection.execute(
                """SELECT response_json,response_status
                     FROM idempotency_records WHERE scope=? AND key=?""",
                ("desktop:POST:/v1/assets/import", "recover-import"),
            ).fetchone()
            self.assertIsNotNone(row)
            frozen = json.loads(row[0])
            self.assertEqual(row[1], 202)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0], 1)

        # A retry after process restart must not need the source to still exist.
        video_path.unlink()
        recovered = client.post("/v1/assets/import", json=payload, headers=headers)
        self.assertEqual(recovered.status_code, 202)
        self.assertEqual(recovered.json, frozen)
        self.assertEqual(runner.submitted, [recovered.json["job_id"]])
        with sqlite3.connect(self.repository.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0], 1)

    def _route_client(
        self,
        runner: RecordingJobRunner,
        *,
        testing: bool = True,
    ):
        app = Flask(__name__)
        app.config.update(
            DESKTOP_SESSION_TOKEN="desktop-token",
            SETTINGS=SimpleNamespace(image_library_dir=self.library),
            TESTING=testing,
        )
        app.extensions["media_repository"] = self.repository
        app.extensions["media_job_runner"] = runner
        app.register_blueprint(api_blueprint)
        if not testing:
            app.logger.disabled = True
        return app, app.test_client(), {"X-MemoLens-Desktop-Token": "desktop-token"}


if __name__ == "__main__":
    unittest.main()
