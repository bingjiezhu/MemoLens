from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.src import DESKTOP_TOKEN_HEADER, create_app, shutdown_runtime_extensions
from core.config import Settings
from core.schemas import parse_indexing_request


class LegacyImageIndexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-legacy-image-adapter-"
        )
        self.root = Path(self.temporary.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.desktop_token = "legacy-image-adapter-token"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(
                    Path(__file__).resolve().parents[1] / "config.yaml"
                ),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_NETWORK_PROFILE": "offline",
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()
        self.headers = {DESKTOP_TOKEN_HEADER: self.desktop_token}
        self.bundle = self.app.extensions["runtime_manager"].current_bundle
        self.repository = self.bundle.extension("media_repository")

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _image_bytes(color: tuple[int, int, int]) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (24, 16), color).save(buffer, format="PNG")
        return buffer.getvalue()

    def _root_identity(self) -> dict[str, str]:
        metadata = self.library.stat()
        return {
            "device": str(metadata.st_dev),
            "inode": str(metadata.st_ino),
        }

    def _payload(self, relative_path: str) -> dict[str, object]:
        return {
            "image_dir": str(self.library),
            "files": [relative_path],
            "library_root_identity": self._root_identity(),
            "persist_to_server": True,
            "recursive": False,
        }

    def _canonical_counts(self) -> tuple[int, ...]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "analysis_runs",
                    "media_jobs",
                    "image_analysis_job_bindings",
                    "image_analysis_attempt_authorities",
                    "image_analysis_attempt_states",
                    "image_index",
                )
            )

    def test_managed_source_enqueues_once_without_sync_provider_or_direct_index_write(
        self,
    ) -> None:
        source = self.library / "managed.png"
        source.write_bytes(self._image_bytes((30, 120, 210)))
        payload = self._payload(source.name)
        indexing_service = self.bundle.extension("indexing_service")
        image_repository = self.bundle.extension("image_index_repository")
        runner = self.bundle.extension("media_job_runner")

        with (
            patch.object(
                indexing_service,
                "run",
                side_effect=AssertionError("legacy route called synchronous indexing"),
            ) as synchronous_run,
            patch.object(
                image_repository,
                "upsert",
                side_effect=AssertionError("legacy route wrote image_index directly"),
            ) as direct_upsert,
            patch.object(runner, "submit") as submit,
        ):
            first = self.client.post(
                "/v1/indexing/jobs",
                json=payload,
                headers=self.headers,
            )

        self.assertEqual(first.status_code, 202, first.get_data(as_text=True))
        self.assertEqual(first.json["object"], "image_index.job")
        self.assertEqual(first.json["status"], "queued")
        self.assertEqual(first.json["data"], [])
        self.assertEqual(first.json["meta"]["indexed_count"], 0)
        self.assertEqual(first.json["meta"]["queued_count"], 1)
        self.assertEqual(first.json["job"]["kind"], "image_analysis")
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        synchronous_run.assert_not_called()
        direct_upsert.assert_not_called()
        # The runner may be wired to dispatch committed image work; it is
        # patched here so the durable admission ledger stays deterministic.
        self.assertLessEqual(submit.call_count, 1)
        self.assertEqual(self._canonical_counts(), (1, 1, 1, 1, 1, 0))

        before_replay = self._canonical_counts()
        source.unlink()
        with (
            patch.object(indexing_service, "run") as synchronous_replay,
            patch.object(image_repository, "upsert") as direct_replay,
            patch.object(runner, "submit"),
        ):
            replay = self.client.post(
                "/v1/indexing/jobs",
                json=payload,
                headers=self.headers,
            )

        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(self._canonical_counts(), before_replay)
        synchronous_replay.assert_not_called()
        direct_replay.assert_not_called()

    def test_inline_payload_fails_closed_without_any_canonical_or_legacy_row(self) -> None:
        response = self.client.post(
            "/v1/indexing/jobs",
            json={
                "input": {
                    "image": {
                        "filename": "inline.png",
                        "b64": "not-authoritative-bytes",
                    }
                },
                "persist_to_server": True,
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "legacy_inline_image_unmanaged")
        self.assertEqual(self._canonical_counts(), (0, 0, 0, 0, 0, 0))

    def test_legacy_pipeline_rejects_persistence_before_provider_or_direct_mutator(
        self,
    ) -> None:
        source = self.library / "pipeline.png"
        source.write_bytes(self._image_bytes((40, 200, 90)))
        indexing_service = self.bundle.extension("indexing_service")
        image_repository = self.bundle.extension("image_index_repository")
        request = parse_indexing_request(
            self._payload(source.name),
            default_image_dir=str(self.library),
            default_model=indexing_service.settings.vision_model,
        )

        with (
            patch.object(
                indexing_service.vision_client,
                "describe_image",
                side_effect=AssertionError("disabled persistence reached provider"),
            ) as provider,
            patch.object(
                image_repository,
                "upsert",
                side_effect=AssertionError("disabled persistence reached direct mutator"),
            ) as direct_upsert,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "legacy_direct_image_persistence_disabled",
            ):
                indexing_service.run(request)

        provider.assert_not_called()
        direct_upsert.assert_not_called()
        self.assertEqual(self._canonical_counts(), (0, 0, 0, 0, 0, 0))

    def test_image_resume_uses_generation_bound_core_path_and_replays_without_new_attempt(
        self,
    ) -> None:
        source = self.library / "resume.png"
        source.write_bytes(self._image_bytes((210, 80, 30)))
        runner = self.bundle.extension("media_job_runner")
        with patch.object(runner, "submit"):
            queued = self.client.post(
                "/v1/indexing/jobs",
                json=self._payload(source.name),
                headers=self.headers,
            )
        self.assertEqual(queued.status_code, 202, queued.get_data(as_text=True))
        job_id = str(queued.json["job_id"])
        self.assertTrue(
            self.repository.interrupt_image_analysis_attempt(
                job_id,
                runtime_generation=self.bundle.generation_id,
                expected_attempt=1,
                detail="resume-route-test",
            )
        )
        resume_headers = {
            **self.headers,
            "Idempotency-Key": "resume-image-analysis-once",
        }

        with (
            patch.object(
                self.repository,
                "reset_media_job_for_resume",
                side_effect=AssertionError("image job reached generic reset"),
            ) as generic_reset,
            patch.object(
                self.repository,
                "resume_image_analysis_job",
                wraps=self.repository.resume_image_analysis_job,
            ) as exact_resume,
            patch.object(runner, "submit") as submit,
        ):
            first = self.client.post(
                f"/v1/index/jobs/{job_id}/resume",
                json={"db_path": str(self.db_path)},
                headers=resume_headers,
            )
            replay = self.client.post(
                f"/v1/index/jobs/{job_id}/resume",
                json={"db_path": str(self.db_path)},
                headers=resume_headers,
            )

        self.assertEqual(first.status_code, 202, first.get_data(as_text=True))
        self.assertEqual(first.json["job"]["kind"], "image_analysis")
        self.assertEqual(first.json["job"]["status"], "queued")
        self.assertEqual(first.json["job"]["attempt"], 2)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        generic_reset.assert_not_called()
        exact_resume.assert_called_once_with(
            job_id,
            runtime_generation=self.bundle.generation_id,
            database_file_identity=self.repository.bind_image_database_identity(),
            reason="explicit_resume",
        )
        submit.assert_called_once_with(job_id)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_analysis_attempt_authorities WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_analysis_attempt_states WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
                2,
            )


if __name__ == "__main__":
    unittest.main()
