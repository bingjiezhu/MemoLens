from __future__ import annotations

import hashlib
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

    def submit(self, job_id: str) -> None:
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
        self.assertIsNone(
            self.repository.get_asset_source(self.repository.source_id(self.root_id, image_path.name))
        )

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
        self.assertEqual(self.runner.submitted, [first.jobs[0]["id"], first.jobs[0]["id"]])

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
        app = Flask(__name__)
        app.config.update(
            DESKTOP_SESSION_TOKEN="desktop-token",
            SETTINGS=SimpleNamespace(image_library_dir=self.library),
        )
        app.extensions["media_repository"] = self.repository
        app.extensions["media_job_runner"] = self.runner
        app.register_blueprint(api_blueprint)
        client = app.test_client()
        auth = {"X-MemoLens-Desktop-Token": "desktop-token"}

        image_path = self.library / "still.jpg"
        Image.new("RGB", (16, 9), "green").save(image_path)
        dry_run = client.post(
            "/v1/assets/import",
            json={"relative_paths": [image_path.name], "recursive": False, "dry_run": True},
            headers={**auth, "Idempotency-Key": "dry-run"},
        )
        self.assertEqual(dry_run.status_code, 200)
        self.assertEqual(dry_run.json["status"], "dry_run")
        self.assertEqual(dry_run.json["assets"][0]["action"], "imported")
        self.assertEqual(dry_run.json["asset_ids"], [])

        video_path = self.library / "clip.mp4"
        video_path.write_bytes(b"route-video")
        video_payload = {
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

        replay = client.post(
            "/v1/assets/import",
            json=video_payload,
            headers={**auth, "Idempotency-Key": "queue-video"},
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, queued.json)
        self.assertEqual(self.runner.submitted, [queued.json["job_id"]])


if __name__ == "__main__":
    unittest.main()
