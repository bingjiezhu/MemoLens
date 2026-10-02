from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from PIL import Image

from backend.src.api.routes import api_blueprint
from backend.src.runtime import RuntimeBundle, RuntimeManager
from core.db import ImageIndexRepository
from core.media_db import MediaRepository


class RecordingMediaRunner:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def submit(self, job_id: str) -> None:
        self.submitted.append(job_id)


class ImageImportEnqueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-image-import-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.repository.bind_image_database_identity()
        self.root_id = str(self.repository.library_roots()[0]["id"])
        self.runner = RecordingMediaRunner()

        settings = SimpleNamespace(
            db_path=self.db_path,
            image_library_dir=self.library,
        )
        self.bundle = RuntimeBundle.freeze(
            settings,
            {
                "media_repository": self.repository,
                "media_job_runner": self.runner,
            },
        )
        manager = RuntimeManager(lambda _retired: None)
        manager.swap(self.bundle)
        self.app = Flask(__name__)
        self.app.config.update(
            DESKTOP_SESSION_TOKEN="desktop-token",
            SETTINGS=settings,
            TESTING=True,
        )
        self.app.extensions["runtime_manager"] = manager
        self.app.register_blueprint(api_blueprint)
        self.client = self.app.test_client()
        self.auth = {"X-MemoLens-Desktop-Token": "desktop-token"}

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _post(
        self,
        *,
        paths: list[str],
        key: str,
        kinds: list[str] | None = None,
        dry_run: bool = False,
        extra: dict[str, object] | None = None,
    ):
        payload: dict[str, object] = {
            "db_path": str(self.db_path),
            "library_root_id": self.root_id,
            "relative_paths": paths,
            "recursive": False,
            "dry_run": dry_run,
        }
        if kinds is not None:
            payload["kinds"] = kinds
        if extra:
            payload.update(extra)
        return self.client.post(
            "/v1/assets/import",
            json=payload,
            headers={**self.auth, "Idempotency-Key": key},
        )

    def _authority_counts(self) -> tuple[int, ...]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "analysis_runs",
                    "media_jobs",
                    "image_analysis_job_bindings",
                    "image_analysis_attempt_authorities",
                    "image_analysis_attempt_states",
                )
            )

    def test_clean_image_import_is_one_exact_bound_durable_job_and_http_replay(self) -> None:
        image_path = self.library / "still.png"
        Image.new("RGB", (32, 18), "purple").save(image_path)
        payload = image_path.read_bytes()
        input_sha256 = hashlib.sha256(payload).hexdigest()
        observed = image_path.stat()
        database = os.stat(self.db_path, follow_symlinks=False)

        first = self._post(paths=[image_path.name], key="image-import-one")

        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual(first.json["status"], "queued")
        self.assertEqual(len(first.json["jobs"]), 1)
        job = first.json["jobs"][0]
        self.assertEqual(job["kind"], "image_analysis")
        self.assertEqual(job["asset_id"], f"asset_{input_sha256[:24]}")
        self.assertEqual(first.json["job_id"], job["id"])
        self.assertEqual(self.runner.submitted, [job["id"]])
        self.assertEqual(self._authority_counts(), (1, 1, 1, 1, 1))

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            binding = connection.execute(
                "SELECT * FROM image_analysis_job_bindings WHERE job_id=?",
                (job["id"],),
            ).fetchone()
            authority = connection.execute(
                """SELECT * FROM image_analysis_attempt_authorities
                    WHERE job_id=? AND attempt=1""",
                (job["id"],),
            ).fetchone()
        assert binding is not None and authority is not None
        root = self.repository.library_root(self.root_id)
        assert root is not None
        source_id = self.repository.source_id(self.root_id, image_path.name)
        identity_material = (
            f"{observed.st_dev}\0{observed.st_ino}\0{observed.st_size}\0"
            f"{observed.st_mtime_ns}\0{observed.st_ctime_ns}"
        )
        expected_identity_sha256 = hashlib.sha256(identity_material.encode()).hexdigest()
        self.assertEqual(binding["database_device"], database.st_dev)
        self.assertEqual(binding["database_inode"], database.st_ino)
        self.assertEqual(binding["asset_id"], job["asset_id"])
        self.assertEqual(binding["source_id"], source_id)
        self.assertEqual(binding["library_root_id"], self.root_id)
        self.assertEqual(binding["relative_path"], image_path.name)
        self.assertEqual(binding["observed_size"], observed.st_size)
        self.assertEqual(binding["observed_mtime_ns"], observed.st_mtime_ns)
        self.assertEqual(binding["observed_ctime_ns"], observed.st_ctime_ns)
        self.assertEqual(binding["source_device"], observed.st_dev)
        self.assertEqual(binding["source_inode"], observed.st_ino)
        self.assertEqual(binding["file_identity_sha256"], expected_identity_sha256)
        self.assertEqual(binding["input_asset_sha256"], input_sha256)
        self.assertEqual(
            binding["root_permission_fingerprint"],
            root["permission_fingerprint"],
        )
        self.assertEqual(binding["analysis_profile_id"], "canonical-image-local-v1")
        self.assertEqual(binding["analysis_profile_version"], "1")
        self.assertEqual(
            binding["analysis_profile_sha256"],
            hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
        )
        self.assertEqual(binding["expected_head_state"], "missing")
        self.assertIsNone(binding["expected_head_run_id"])
        self.assertEqual(authority["runtime_generation"], self.bundle.generation_id)
        request_document = json.loads(binding["request_json"])
        self.assertEqual(request_document["expected_head"], None)
        self.assertEqual(
            request_document["database_binding"],
            {
                "database_uuid": self.repository.database_uuid,
                "database_device": database.st_dev,
                "database_inode": database.st_ino,
            },
        )

        before_replay = self._authority_counts()
        image_path.unlink()
        replay = self._post(paths=[image_path.name], key="image-import-one")
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(self._authority_counts(), before_replay)
        # Re-dispatching the same committed queued ID is intentional: HTTP
        # replay repairs a crash between commit and process-local submission.
        # The runner's admission set deduplicates concurrent execution.
        self.assertEqual(self.runner.submitted, [job["id"], job["id"]])

    def test_image_video_batch_creates_and_dispatches_each_registered_kind_once(self) -> None:
        image_path = self.library / "still.png"
        Image.new("RGB", (20, 10), "green").save(image_path)
        video_path = self.library / "clip.mp4"
        video_path.write_bytes(b"video-import-fixture")

        response = self._post(
            paths=[image_path.name, video_path.name],
            kinds=["image", "video"],
            key="mixed-import",
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        jobs_by_kind = {job["kind"]: job for job in response.json["jobs"]}
        self.assertEqual(set(jobs_by_kind), {"image_analysis", "video_index"})
        self.assertEqual(len(response.json["jobs"]), 2)
        self.assertEqual(
            set(self.runner.submitted),
            {jobs_by_kind["image_analysis"]["id"], jobs_by_kind["video_index"]["id"]},
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM media_jobs WHERE kind='image_analysis'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM media_jobs WHERE kind='video_index'"
                ).fetchone()[0],
                1,
            )

    def test_image_job_rolls_back_with_import_response_snapshot_failure(self) -> None:
        image_path = self.library / "rollback.png"
        Image.new("RGB", (20, 10), "orange").save(image_path)

        with patch.object(
            MediaRepository,
            "_idempotency_store_success",
            side_effect=RuntimeError("simulated response snapshot failure"),
        ), self.assertRaisesRegex(RuntimeError, "simulated response snapshot failure"):
            self._post(
                paths=[image_path.name],
                kinds=["image"],
                key="rollback-image",
            )

        self.assertEqual(self._authority_counts(), (0, 0, 0, 0, 0))
        self.assertIsNone(
            self.repository.get_asset_source(
                self.repository.source_id(self.root_id, image_path.name)
            )
        )
        self.assertEqual(self.runner.submitted, [])

    def test_dry_run_and_invalid_image_create_no_image_job_authority(self) -> None:
        valid_path = self.library / "dry.png"
        Image.new("RGB", (12, 8), "blue").save(valid_path)
        dry_run = self._post(
            paths=[valid_path.name],
            kinds=["image"],
            key="dry-image",
            dry_run=True,
        )
        self.assertEqual(dry_run.status_code, 200)
        self.assertEqual(dry_run.json["status"], "dry_run")
        self.assertEqual(dry_run.json["jobs"], [])
        self.assertEqual(self._authority_counts(), (0, 0, 0, 0, 0))

        invalid_path = self.library / "broken.jpg"
        invalid_path.write_bytes(b"not-an-image")
        invalid = self._post(
            paths=[invalid_path.name],
            kinds=["image"],
            key="invalid-image",
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.json["status"], "partial")
        self.assertEqual(invalid.json["jobs"], [])
        self.assertEqual(invalid.json["rejected"][0]["code"], "invalid_image")
        self.assertEqual(self._authority_counts(), (0, 0, 0, 0, 0))
        self.assertEqual(self.runner.submitted, [])

    def test_payload_cannot_supply_server_owned_image_authority(self) -> None:
        image_path = self.library / "forged.png"
        Image.new("RGB", (12, 8), "red").save(image_path)

        response = self._post(
            paths=[image_path.name],
            kinds=["image"],
            key="forged-authority",
            extra={
                "runtime_generation": f"runtime_generation_{'f' * 64}",
                "database_device": 1,
                "database_inode": 1,
                "analysis_profile": {
                    "profile_id": "forged",
                    "profile_version": "1",
                    "profile_sha256": "f" * 64,
                },
                "expected_head": None,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "invalid_import")
        self.assertIn("server-derived", response.json["message"])
        self.assertEqual(self._authority_counts(), (0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
