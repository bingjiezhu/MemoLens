from __future__ import annotations

import hmac
import hashlib
import inspect
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import yaml

from backend.src import (
    DESKTOP_TOKEN_HEADER,
    MAIN_AUTHORITY_HEADER,
    MEMOLENS_API_VERSION,
    MEMOLENS_SERVICE_ID,
    build_runtime_extensions,
    create_app,
    shutdown_runtime_extensions,
    swap_runtime,
)
from backend.src.api.routes import _is_local_remote_addr
from backend.src.retrieval import RetrievalCopywriter
from backend.src.retrieval.retrieval import CanonicalRetrievalResponse
from core.config import Settings
from core.db import ImageIndexRepository
from core.image_analysis_contract import IMAGE_ANALYSIS_STAGES
from core.media_db import MediaRepository
from core.sqlite_runtime import UnsafeSQLiteRuntimeError
from core.schemas import (
    GeneratedCopy,
    RetrievedImageSummary,
    StoredImageRecord,
)


def _record(*, image_id: str, sha256: str, relative_path: str) -> StoredImageRecord:
    now = "2026-01-01T00:00:00+00:00"
    return StoredImageRecord(
        id=image_id,
        sha256=sha256,
        filename=Path(relative_path).name,
        relative_path=relative_path,
        mime_type="image/jpeg",
        file_size=1,
        width=1,
        height=1,
        taken_at=None,
        lat=None,
        lon=None,
        altitude=None,
        place_name=None,
        country=None,
        description=image_id,
        tags=[image_id],
        combined_text=image_id,
        text_embedding_model=None,
        combined_text_embedding_blob=None,
        embedding_backend="semantic_hash",
        embedding_blob=np.array([1.0, 0.0], dtype=np.float32).tobytes(),
        created_at=now,
        updated_at=now,
    )


class SettingsPrivacyDefaultsRegressionTests(unittest.TestCase):
    def test_copywriter_does_not_upload_photos_by_default(self) -> None:
        image_limit = inspect.signature(RetrievalCopywriter.generate).parameters[
            "image_limit"
        ]
        self.assertEqual(image_limit.default, 0)

    def test_reverse_geocoding_is_off_when_config_section_is_absent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-config-test-") as temporary:
            root = Path(temporary)
            library = root / "photos"
            library.mkdir()
            config_path = root / "minimal.yaml"
            source_config = yaml.safe_load(
                (Path(__file__).resolve().parents[1] / "config.yaml").read_text(
                    encoding="utf-8"
                )
            )
            source_config.pop("geocode", None)
            config_path.write_text(
                yaml.safe_dump(source_config, sort_keys=False), encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {
                    "APP_CONFIG_PATH": str(config_path),
                    "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                    "IMAGE_LIBRARY_DIR": str(library),
                    "SQLITE_DB_PATH": str(root / "state" / "index.db"),
                },
                clear=False,
            ):
                os.environ.pop("ENABLE_REVERSE_GEOCODE", None)
                settings = Settings.from_env()

        self.assertFalse(settings.geocode_enabled)


class ImageIndexRepositoryRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-db-test-")
        self.db_path = Path(self.temporary_directory.name) / "photo-index.db"
        self.repository = ImageIndexRepository(self.db_path)
        self.repository.ensure_schema()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _rows(self) -> list[sqlite3.Row]:
        return self.repository.fetch_candidates()

    def test_refresh_removes_stale_row_when_content_replaces_an_occupied_path(self) -> None:
        self.repository.upsert(_record(image_id="image-a", sha256="a" * 64, relative_path="A.jpg"))
        self.repository.upsert(_record(image_id="image-b", sha256="b" * 64, relative_path="B.jpg"))

        # B.jpg was overwritten with bytes already indexed as A.jpg. The
        # existing-SHA fast path must move A's record and remove stale B.
        self.repository.refresh_existing_file_metadata(
            sha256="a" * 64,
            filename="B.jpg",
            relative_path="B.jpg",
            mime_type="image/jpeg",
            file_size=2,
            width=2,
            height=2,
            taken_at=None,
            lat=None,
            lon=None,
            altitude=None,
            updated_at="2026-01-02T00:00:00+00:00",
        )

        rows = self._rows()
        self.assertEqual([(row["id"], row["relative_path"]) for row in rows], [("image-a", "B.jpg")])

    def test_refresh_missing_source_sha_preserves_destination_row(self) -> None:
        self.repository.upsert(_record(image_id="image-b", sha256="b" * 64, relative_path="B.jpg"))

        with self.assertRaises(LookupError):
            self.repository.refresh_existing_file_metadata(
                sha256="missing",
                filename="B.jpg",
                relative_path="B.jpg",
                mime_type="image/jpeg",
                file_size=2,
                width=2,
                height=2,
                taken_at=None,
                lat=None,
                lon=None,
                altitude=None,
                updated_at="2026-01-02T00:00:00+00:00",
            )

        self.assertEqual([(row["id"], row["relative_path"]) for row in self._rows()], [("image-b", "B.jpg")])

    def test_upsert_rolls_back_path_cleanup_when_insert_fails(self) -> None:
        self.repository.upsert(_record(image_id="image-a", sha256="a" * 64, relative_path="A.jpg"))
        self.repository.upsert(_record(image_id="image-b", sha256="b" * 64, relative_path="B.jpg"))

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.upsert(
                _record(image_id="image-c", sha256="a" * 64, relative_path="B.jpg")
            )

        self.assertEqual(
            sorted((row["id"], row["relative_path"]) for row in self._rows()),
            [("image-a", "A.jpg"), ("image-b", "B.jpg")],
        )

    def test_schema_upgrade_deduplicates_paths_and_enforces_uniqueness(self) -> None:
        first = _record(image_id="old", sha256="a" * 64, relative_path="same.jpg")
        latest = _record(image_id="latest", sha256="b" * 64, relative_path="same.jpg")
        self.repository.upsert(first)

        # Recreate the historical non-unique schema and inject a duplicate.
        with self.repository._connect() as connection:
            connection.execute("DROP INDEX idx_image_index_relative_path")
            connection.execute(
                "CREATE INDEX idx_image_index_relative_path ON image_index(relative_path)"
            )
            values = (
                latest.id,
                latest.sha256,
                latest.filename,
                latest.relative_path,
                latest.mime_type,
                latest.file_size,
                latest.width,
                latest.height,
                latest.taken_at,
                latest.lat,
                latest.lon,
                latest.altitude,
                latest.place_name,
                latest.country,
                latest.description,
                '["latest"]',
                latest.combined_text,
                latest.text_embedding_model,
                latest.combined_text_embedding_blob,
                latest.embedding_backend,
                latest.embedding_blob,
                latest.aesthetic_score,
                latest.aesthetic_model,
                latest.technical_quality_score,
                latest.aesthetic_updated_at,
                latest.created_at,
                latest.updated_at,
            )
            connection.execute(
                """
                INSERT INTO image_index (
                    id, sha256, filename, relative_path, mime_type, file_size,
                    width, height, taken_at, lat, lon, altitude, place_name,
                    country, description, tags_json, combined_text,
                    text_embedding_model, combined_text_embedding,
                    embedding_backend, embedding, aesthetic_score,
                    aesthetic_model, technical_quality_score,
                    aesthetic_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

        self.repository.ensure_schema()

        self.assertEqual(
            [(row["id"], row["relative_path"]) for row in self._rows()],
            [("latest", "same.jpg")],
        )
        self.repository.upsert(
            _record(image_id="other", sha256="c" * 64, relative_path="other.jpg")
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repository._connect() as connection:
                connection.execute(
                    "UPDATE image_index SET relative_path = ? WHERE id = ?",
                    ("same.jpg", "other"),
                )


class BackendSQLiteAdmissionRegressionTests(unittest.TestCase):
    def test_create_app_fails_before_flask_settings_directories_or_database(self) -> None:
        unsafe = UnsafeSQLiteRuntimeError(
            {
                "sqlite_version": "3.47.1",
                "wal_reset_safe": False,
            }
        )
        with (
            patch(
                "backend.src.require_safe_sqlite_runtime",
                side_effect=unsafe,
            ) as admission,
            patch("backend.src.Flask") as flask_factory,
            patch("backend.src.Settings.from_env") as settings_factory,
            patch("backend.src.configure_runtime") as configure,
        ):
            with self.assertRaises(UnsafeSQLiteRuntimeError) as captured:
                create_app()

        self.assertIs(captured.exception, unsafe)
        self.assertEqual(captured.exception.code, "sqlite_wal_reset_unsafe")
        admission.assert_called_once_with()
        flask_factory.assert_not_called()
        settings_factory.assert_not_called()
        configure.assert_not_called()

    def test_runtime_factory_fails_before_directories_or_database(self) -> None:
        unsafe = UnsafeSQLiteRuntimeError(
            {
                "sqlite_version": "3.47.1",
                "wal_reset_safe": False,
            }
        )
        settings = unittest.mock.Mock()
        settings.ensure_directories.side_effect = AssertionError(
            "runtime factory touched directories before admission"
        )

        with patch(
            "backend.src.require_safe_sqlite_runtime",
            side_effect=unsafe,
        ) as admission:
            with self.assertRaises(UnsafeSQLiteRuntimeError) as captured:
                build_runtime_extensions(settings)

        self.assertIs(captured.exception, unsafe)
        admission.assert_called_once_with()
        settings.ensure_directories.assert_not_called()

    def test_repository_is_not_closed_when_a_runner_cannot_quiesce(self) -> None:
        media_runner = unittest.mock.Mock()
        media_runner.shutdown.side_effect = RuntimeError("cancel write failed")
        render_runner = unittest.mock.Mock()
        repository = unittest.mock.Mock()

        shutdown_runtime_extensions(
            {
                "media_job_runner": media_runner,
                "render_job_runner": render_runner,
                "media_repository": repository,
            }
        )

        media_runner.shutdown.assert_called_once_with()
        render_runner.shutdown.assert_called_once_with()
        repository.close.assert_not_called()


class BackendBoundaryRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-api-test-")
        root = Path(self.temporary_directory.name)
        self.photos_dir = root / "photos"
        self.photos_dir.mkdir()
        self.desktop_token = "test-desktop-session-token"
        self.main_token = "test-main-authority-token"
        self.desktop_headers = {DESKTOP_TOKEN_HEADER: self.desktop_token}
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(Path(__file__).resolve().parents[1] / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.photos_dir),
                "SQLITE_DB_PATH": str(root / "state" / "photo-index.db"),
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": self.main_token,
            },
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def _runtime_payload():
        runtime = unittest.mock.Mock()
        runtime.to_dict.return_value = {}
        return runtime

    def test_health_has_stable_identity_and_security_headers(self) -> None:
        challenge = "ab" * 32
        response = self.client.get(
            "/healthz",
            query_string={"challenge": challenge},
            headers={"Origin": "http://127.0.0.1:5173"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json),
            {
                "status",
                "object",
                "service",
                "api_version",
                "challenge_proof",
                "sqlite_runtime",
            },
        )
        self.assertEqual(response.json["service"], MEMOLENS_SERVICE_ID)
        self.assertEqual(response.json["api_version"], MEMOLENS_API_VERSION)
        self.assertEqual(
            response.json["challenge_proof"],
            hmac.new(
                self.desktop_token.encode(),
                "\n".join(
                    [
                        "memolens.health.v1",
                        f"challenge={challenge}",
                        "object=health.check",
                        "status=ok",
                        f"service={MEMOLENS_SERVICE_ID}",
                        f"api_version={MEMOLENS_API_VERSION}",
                        f"policy_id={response.json['sqlite_runtime']['policy_id']}",
                        f"sqlite_version={sqlite3.sqlite_version}",
                        "wal_reset_safe=true",
                        "journal_policy=wal",
                    ]
                ).encode(),
                "sha256",
            ).hexdigest(),
        )
        self.assertEqual(
            response.json["sqlite_runtime"],
            {
                "policy_id": self.app.config["SQLITE_RUNTIME_CAPABILITY"]["policy_id"],
                "sqlite_version": sqlite3.sqlite_version,
                "wal_reset_safe": True,
                "journal_policy": "wal",
            },
        )
        self.assertNotIn("python_version", response.json["sqlite_runtime"])
        self.assertFalse(
            any(
                "path" in key or "executable" in key
                for key in response.json["sqlite_runtime"]
            )
        )
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5173")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_production_runtime_lease_exposes_coverage_materialization(self) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        repository = bundle.extension("media_repository")
        blueprint_service = bundle.extension("blueprint_service")
        self.assertIsNotNone(bundle.extension("coverage_service"))
        project = repository.create_project(
            "Production Coverage",
            {"goal": "legacy production coverage", "candidate_refs": []},
            {"created_by": "production-runtime-test"},
        )
        project_id = str(project["id"])
        brief = repository.get_brief(project_id, 1)
        assert brief is not None
        semantic = {
            "intent": {
                "goal": "turn local memories into a short film",
                "stance": "ordinary moments deserve attention",
                "audience": "individual creators",
                "platform": "short-video",
            },
            "script": {
                "blocks": [
                    {"block_id": "opening", "text": "These moments were waiting."},
                    {"block_id": "ending", "text": "Now they have a place."},
                ]
            },
            "direction": {
                "theme": "rediscovery",
                "narrative_arc": "forgotten to found",
                "emotion": "warm",
                "tone": "restrained",
                "pace": "measured",
            },
            "output": {"duration_target_ms": 8_000, "aspect_ratio": "9:16"},
            "constraints": {"must_include": [], "must_exclude": []},
            "material_hints": [],
            "reference_refs": [],
            "technique_refs": [],
            "bindings": {"creator_context": None, "wiki_generation": None},
            "assumptions": [],
            "missing_evidence": [],
            "open_decisions": [],
        }
        blueprint_service.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": semantic,
            },
            idempotency_key="production-blueprint",
        )
        blueprint = repository.get_blueprint_head(project_id)
        assert blueprint is not None
        response = self.client.post(
            f"/v1/creative/projects/{project_id}/coverage/materialize",
            query_string={
                "db_path": str(repository.db_path),
                "expected_database_uuid": repository.database_uuid,
            },
            json={
                "expected_blueprint": {
                    "revision": blueprint["revision"],
                    "content_sha256": blueprint["content_sha256"],
                    "semantic_sha256": blueprint["semantic_sha256"],
                },
                "expected_plan_head": None,
            },
            headers={
                **self.desktop_headers,
                "Idempotency-Key": "production-coverage",
            },
        )
        self.assertEqual(response.status_code, 201)
        read = self.client.get(
            f"/v1/creative/projects/{project_id}/coverage",
            query_string={"db_path": str(repository.db_path)},
        )
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json["coverage_plan"]["revision"], 1)
        self.assertEqual(read.json["freshness"]["state"], "current")

    def test_cors_requires_token_for_opaque_renderer_origins(self) -> None:
        with patch(
            "backend.src.api.routes.detect_local_model_runtime",
            return_value=self._runtime_payload(),
        ):
            preflight_response = self.client.options(
                "/v1/settings",
                headers={
                    "Origin": "null",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": (
                        f"{DESKTOP_TOKEN_HEADER}, Idempotency-Key"
                    ),
                },
            )
            missing_token_response = self.client.get(
                "/v1/settings", headers={"Origin": "null"}
            )
            file_response = self.client.get(
                "/v1/settings",
                headers={"Origin": "null", **self.desktop_headers},
            )
            file_scheme_response = self.client.get(
                "/v1/settings",
                headers={"Origin": "file://", **self.desktop_headers},
            )
            untrusted_response = self.client.get(
                "/v1/settings",
                headers={
                    "Origin": "http://127.0.0.1:9999",
                    **self.desktop_headers,
                },
            )

        self.assertEqual(preflight_response.status_code, 200)
        self.assertIn(DESKTOP_TOKEN_HEADER, preflight_response.headers["Access-Control-Allow-Headers"])
        self.assertIn("Idempotency-Key", preflight_response.headers["Access-Control-Allow-Headers"])
        self.assertEqual(missing_token_response.status_code, 403)
        self.assertEqual(file_response.headers["Access-Control-Allow-Origin"], "null")
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_scheme_response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", untrusted_response.headers)
        self.assertNotIn("Access-Control-Allow-Methods", untrusted_response.headers)

    def test_originless_loopback_v1_requests_remain_compatible(self) -> None:
        with patch(
            "backend.src.api.routes.detect_local_model_runtime",
            return_value=self._runtime_payload(),
        ):
            without_token = self.client.get("/v1/settings")
            accepted = self.client.get(
                "/v1/settings", headers=self.desktop_headers
            )

        self.assertEqual(without_token.status_code, 200)
        self.assertEqual(accepted.status_code, 200)

    def test_empty_remote_address_is_not_treated_as_local(self) -> None:
        self.assertFalse(_is_local_remote_addr(None))
        self.assertFalse(_is_local_remote_addr(""))
        self.assertFalse(_is_local_remote_addr("not-an-ip"))
        self.assertTrue(_is_local_remote_addr("127.0.0.1"))
        self.assertTrue(_is_local_remote_addr("::ffff:127.0.0.1"))

        response = self.client.put(
            "/v1/settings",
            json={},
            headers=self.desktop_headers,
            environ_overrides={"REMOTE_ADDR": ""},
        )
        self.assertEqual(response.status_code, 403)

    def test_settings_reject_invalid_sqlite_without_mutating_live_runtime(self) -> None:
        invalid_db_path = Path(self.temporary_directory.name) / "not-sqlite.db"
        invalid_db_path.write_text("not sqlite", encoding="utf-8")
        settings_path = self.app.config["SETTINGS"].persisted_settings_path
        persisted_before = settings_path.read_bytes() if settings_path.exists() else None
        settings_before = self.app.config["SETTINGS"]
        repository_before = self.app.extensions["media_repository"]
        runner_before = self.app.extensions["media_job_runner"]

        with patch.object(runner_before, "shutdown", wraps=runner_before.shutdown) as shutdown:
            response = self.client.put(
                "/v1/settings",
                json={"db_path": str(invalid_db_path)},
                headers=self.desktop_headers,
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("valid MemoLens SQLite", response.json["message"])
        self.assertIs(self.app.config["SETTINGS"], settings_before)
        self.assertIs(self.app.extensions["media_repository"], repository_before)
        self.assertIs(self.app.extensions["media_job_runner"], runner_before)
        shutdown.assert_not_called()
        persisted_after = settings_path.read_bytes() if settings_path.exists() else None
        self.assertEqual(persisted_after, persisted_before)

    def test_settings_reject_candidate_store_and_generation_scope_without_runtime_swap(
        self,
    ) -> None:
        invalid_db_path = Path(self.temporary_directory.name) / "scope-not-sqlite.db"
        invalid_db_path.write_text("not sqlite", encoding="utf-8")
        missing_library = Path(self.temporary_directory.name) / "missing-library"
        settings_before = self.app.config["SETTINGS"]
        settings_path = settings_before.persisted_settings_path
        persisted_before = settings_path.read_bytes() if settings_path.exists() else None
        manager = self.app.extensions["runtime_manager"]
        bundle_before = manager.current_bundle
        repository_before = self.app.extensions["media_repository"]
        runner_before = self.app.extensions["media_job_runner"]

        cases = (
            {"db_path": str(invalid_db_path)},
            {"image_library_dir": str(missing_library)},
            {"settings_path": str(Path(self.temporary_directory.name) / "outside.yaml")},
            {"runtime_generation": "forged-generation"},
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index, fields=sorted(payload)):
                response = self.client.put(
                    "/v1/settings",
                    json=payload,
                    headers=self.desktop_headers,
                )
                self.assertEqual(response.status_code, 400, response.get_json())
                self.assertIs(manager.current_bundle, bundle_before)
                self.assertIs(self.app.config["SETTINGS"], settings_before)
                self.assertIs(self.app.extensions["media_repository"], repository_before)
                self.assertIs(self.app.extensions["media_job_runner"], runner_before)
                persisted_after = (
                    settings_path.read_bytes() if settings_path.exists() else None
                )
                self.assertEqual(persisted_after, persisted_before)

    def test_settings_swap_failure_rolls_back_persistence_and_keeps_old_runner(self) -> None:
        settings_path = self.app.config["SETTINGS"].persisted_settings_path
        persisted_before = settings_path.read_bytes() if settings_path.exists() else None
        settings_before = self.app.config["SETTINGS"]
        runner_before = self.app.extensions["media_job_runner"]

        with (
            patch(
                "backend.src.api.routes.detect_local_model_runtime",
                return_value=self._runtime_payload(),
            ),
            patch("backend.src.api.routes.swap_runtime", side_effect=RuntimeError("swap failed")),
            patch.object(runner_before, "shutdown", wraps=runner_before.shutdown) as shutdown,
        ):
            response = self.client.put(
                "/v1/settings",
                json={"process_image_width": settings_before.process_image_width + 1},
                headers=self.desktop_headers,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["code"], "settings_reload_failed")
        self.assertIs(self.app.config["SETTINGS"], settings_before)
        self.assertIs(self.app.extensions["media_job_runner"], runner_before)
        shutdown.assert_not_called()
        persisted_after = settings_path.read_bytes() if settings_path.exists() else None
        self.assertEqual(persisted_after, persisted_before)

    def test_settings_persist_failure_keeps_live_runtime_and_old_runner(self) -> None:
        settings_path = self.app.config["SETTINGS"].persisted_settings_path
        persisted_before = settings_path.read_bytes() if settings_path.exists() else None
        settings_before = self.app.config["SETTINGS"]
        runner_before = self.app.extensions["media_job_runner"]

        with (
            patch(
                "backend.src.api.routes.detect_local_model_runtime",
                return_value=self._runtime_payload(),
            ),
            patch(
                "backend.src.api.routes._save_settings_atomically",
                side_effect=OSError("disk full"),
            ),
            patch.object(runner_before, "shutdown", wraps=runner_before.shutdown) as shutdown,
        ):
            response = self.client.put(
                "/v1/settings",
                json={"process_image_width": settings_before.process_image_width + 1},
                headers=self.desktop_headers,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["code"], "settings_persist_failed")
        self.assertIs(self.app.config["SETTINGS"], settings_before)
        self.assertIs(self.app.extensions["media_job_runner"], runner_before)
        shutdown.assert_not_called()
        persisted_after = settings_path.read_bytes() if settings_path.exists() else None
        self.assertEqual(persisted_after, persisted_before)

    def test_settings_persist_failure_does_not_interrupt_jobs_in_the_unadopted_database(self) -> None:
        candidate_db = Path(self.temporary_directory.name) / "candidate.db"
        ImageIndexRepository(candidate_db).ensure_schema()
        candidate_repository = MediaRepository(candidate_db)
        candidate_repository.ensure_schema(self.photos_dir)
        root = candidate_repository.register_library_root(self.photos_dir)
        source_path = self.photos_dir / "queued-candidate.mp4"
        content = b"candidate job must remain queued"
        source_path.write_bytes(content)
        metadata = source_path.stat()
        asset = candidate_repository.upsert_asset_source(
            root_id=str(root["id"]),
            relative_path=source_path.name,
            filename=source_path.name,
            kind="video",
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type="video/mp4",
            file_size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            source_file_id=str(metadata.st_ino),
        )
        job = candidate_repository.create_analysis_job(asset_id=str(asset["id"]))
        self.assertEqual(job["status"], "queued")

        with (
            patch(
                "backend.src.api.routes.detect_local_model_runtime",
                return_value=self._runtime_payload(),
            ),
            patch(
                "backend.src.api.routes._save_settings_atomically",
                side_effect=OSError("disk full"),
            ),
        ):
            response = self.client.put(
                "/v1/settings",
                json={"db_path": str(candidate_db)},
                headers=self.desktop_headers,
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["code"], "settings_persist_failed")
        unchanged = MediaRepository(candidate_db).get_media_job(str(job["id"]))
        self.assertIsNotNone(unchanged)
        self.assertEqual(unchanged["status"], "queued")

    def test_runtime_swap_pins_request_database_and_defers_old_runner_shutdown(self) -> None:
        manager = self.app.extensions["runtime_manager"]
        old_bundle = manager.current_bundle
        old_repository = old_bundle.extension("media_repository")
        old_runner = old_bundle.extension("media_job_runner")
        old_render_runner = old_bundle.extension("render_job_runner")
        old_db_path = old_repository.db_path
        teardown_order = unittest.mock.Mock()

        candidate_root = Path(self.temporary_directory.name) / "runtime-b"
        candidate_library = candidate_root / "photos"
        candidate_library.mkdir(parents=True)
        candidate_settings = replace(
            old_bundle.settings,
            image_library_dir=candidate_library,
            db_path=candidate_root / "runtime-b.db",
        )
        candidate_extensions = build_runtime_extensions(candidate_settings)
        new_repository = candidate_extensions["media_repository"]

        validated = threading.Event()
        continue_request = threading.Event()
        original_update = old_bundle.extension("media_inbox_service").update_review

        def blocked_update(*args, **kwargs):
            validated.set()
            if not continue_request.wait(timeout=10):
                raise RuntimeError("runtime barrier timed out")
            return original_update(*args, **kwargs)

        asset_path = self.photos_dir / "runtime-a.jpg"
        asset_path.write_bytes(b"runtime-a")
        metadata = asset_path.stat()
        root_id = str(old_repository.library_roots()[0]["id"])
        asset = old_repository.upsert_asset_source(
            root_id=root_id,
            relative_path=asset_path.name,
            filename=asset_path.name,
            kind="image",
            sha256=hashlib.sha256(b"runtime-a").hexdigest(),
            mime_type="image/jpeg",
            file_size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            source_file_id=str(metadata.st_ino),
        )
        old_repository.update_image_probe(str(asset["id"]), width=10, height=10)
        payload = {
            "db_path": str(old_db_path),
            "base_revision": 0,
            "favorite": True,
        }
        response_holder: list[object] = []

        def send_old_request() -> None:
            with self.app.test_client() as client:
                response_holder.append(
                    client.put(
                        f"/v1/inbox/assets/{asset['id']}",
                        json=payload,
                        headers={
                            **self.desktop_headers,
                            "Idempotency-Key": "runtime-pinned-a",
                        },
                    )
                )

        with (
            patch.object(
                old_bundle.extension("media_inbox_service"),
                "update_review",
                side_effect=blocked_update,
            ),
            patch.object(old_runner, "shutdown", wraps=old_runner.shutdown) as shutdown,
            patch.object(
                old_render_runner,
                "shutdown",
                wraps=old_render_runner.shutdown,
            ) as render_shutdown,
            patch.object(
                old_repository,
                "close",
                wraps=old_repository.close,
            ) as repository_close,
        ):
            teardown_order.attach_mock(shutdown, "media_runner")
            teardown_order.attach_mock(render_shutdown, "render_runner")
            teardown_order.attach_mock(repository_close, "media_repository")
            request_thread = threading.Thread(target=send_old_request)
            request_thread.start()
            self.assertTrue(validated.wait(timeout=10))
            swap_runtime(self.app, candidate_settings, candidate_extensions)
            shutdown.assert_not_called()
            render_shutdown.assert_not_called()
            repository_close.assert_not_called()
            continue_request.set()
            request_thread.join(timeout=10)
            self.assertFalse(request_thread.is_alive())
            self.assertEqual(response_holder[0].status_code, 200)
            shutdown.assert_called_once_with()
            render_shutdown.assert_called_once_with()
            repository_close.assert_called_once_with()
            self.assertEqual(
                teardown_order.mock_calls,
                [
                    unittest.mock.call.media_runner(),
                    unittest.mock.call.render_runner(),
                    unittest.mock.call.media_repository(),
                ],
            )

        old_review = old_repository.get_asset(str(asset["id"]))["review"]
        self.assertTrue(old_review["favorite"])
        self.assertIsNone(new_repository.get_asset(str(asset["id"])))

        mismatch = self.client.put(
            f"/v1/inbox/assets/{asset['id']}",
            json=payload,
            headers={
                **self.desktop_headers,
                "Idempotency-Key": "runtime-mismatch-b",
            },
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json["code"], "database_binding_mismatch")

        candidate_asset_path = candidate_library / "runtime-b.jpg"
        candidate_asset_path.write_bytes(b"runtime-b")
        candidate_metadata = candidate_asset_path.stat()
        candidate_root_id = str(new_repository.library_roots()[0]["id"])
        candidate_asset = new_repository.upsert_asset_source(
            root_id=candidate_root_id,
            relative_path=candidate_asset_path.name,
            filename=candidate_asset_path.name,
            kind="image",
            sha256=hashlib.sha256(b"runtime-b").hexdigest(),
            mime_type="image/jpeg",
            file_size=candidate_metadata.st_size,
            mtime_ns=candidate_metadata.st_mtime_ns,
            source_file_id=str(candidate_metadata.st_ino),
        )
        new_repository.update_image_probe(str(candidate_asset["id"]), width=10, height=10)
        current = self.client.put(
            f"/v1/inbox/assets/{candidate_asset['id']}",
            json={
                "db_path": str(new_repository.db_path),
                "base_revision": 0,
                "favorite": True,
            },
            headers={
                **self.desktop_headers,
                "Idempotency-Key": "runtime-current-b",
            },
        )
        self.assertEqual(current.status_code, 200)
        self.assertTrue(new_repository.get_asset(str(candidate_asset["id"]))["review"]["favorite"])

    def test_settings_activation_and_export_admission_are_serialized_before_lease(self) -> None:
        candidate_entered = threading.Event()
        release_candidate = threading.Event()
        settings_responses: list[object] = []
        export_responses: list[object] = []
        export_finished = threading.Event()

        def blocked_candidate(_settings):
            candidate_entered.set()
            if not release_candidate.wait(timeout=10):
                raise RuntimeError("candidate barrier timed out")
            raise ValueError("injected candidate rejection")

        def update_settings_request() -> None:
            with self.app.test_client() as client:
                settings_responses.append(
                    client.put(
                        "/v1/settings",
                        json={"process_image_width": 1234},
                        headers=self.desktop_headers,
                    )
                )

        def export_request() -> None:
            try:
                with self.app.test_client() as client:
                    export_responses.append(
                        client.post(
                            "/v1/main/creative/projects/project-1/exports",
                            json={
                                "presentation": {},
                                "presentation_sha256": "a" * 64,
                                "native_gesture_nonce": "",
                                "destination": {
                                    "canonical_path": str(
                                        Path(self.temporary_directory.name) / "exports"
                                    ),
                                    "package_basename": "project-1",
                                    "selection_identity": {
                                        "device": "1",
                                        "inode": "2",
                                    },
                                },
                            },
                            headers={
                                MAIN_AUTHORITY_HEADER: self.main_token,
                                "Idempotency-Key": "serialized-export-admission",
                            },
                        )
                    )
            finally:
                export_finished.set()

        with patch(
            "backend.src.api.routes.build_runtime_extensions",
            side_effect=blocked_candidate,
        ):
            settings_thread = threading.Thread(target=update_settings_request)
            settings_thread.start()
            self.assertTrue(candidate_entered.wait(timeout=10))
            export_thread = threading.Thread(target=export_request)
            export_thread.start()
            self.assertFalse(export_finished.wait(timeout=0.1))
            release_candidate.set()
            settings_thread.join(timeout=10)
            export_thread.join(timeout=10)

        self.assertFalse(settings_thread.is_alive())
        self.assertFalse(export_thread.is_alive())
        self.assertEqual(settings_responses[0].status_code, 400)
        self.assertEqual(export_responses[0].status_code, 403)
        self.assertEqual(
            export_responses[0].json["code"],
            "native_confirmation_required",
        )

    def test_canonical_recovery_failure_aborts_runtime_activation(self) -> None:
        manager = self.app.extensions["runtime_manager"]
        old_bundle = manager.current_bundle
        candidate_root = Path(self.temporary_directory.name) / "recovery-gate"
        candidate_library = candidate_root / "photos"
        candidate_library.mkdir(parents=True)
        candidate_settings = replace(
            old_bundle.settings,
            image_library_dir=candidate_library,
            db_path=candidate_root / "recovery-gate.db",
        )
        candidate_extensions = build_runtime_extensions(candidate_settings)
        candidate_repository = candidate_extensions["media_repository"]
        try:
            with patch.object(
                candidate_repository,
                "mark_canonical_export_jobs_interrupted",
                side_effect=RuntimeError("injected recovery failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected recovery failure"):
                    swap_runtime(self.app, candidate_settings, candidate_extensions)
            self.assertIs(manager.current_bundle, old_bundle)
        finally:
            shutdown_runtime_extensions(candidate_extensions)

    def test_provider_egress_recovery_failure_aborts_runtime_activation(self) -> None:
        manager = self.app.extensions["runtime_manager"]
        old_bundle = manager.current_bundle
        candidate_root = Path(self.temporary_directory.name) / "provider-recovery-gate"
        candidate_library = candidate_root / "photos"
        candidate_library.mkdir(parents=True)
        candidate_settings = replace(
            old_bundle.settings,
            image_library_dir=candidate_library,
            db_path=candidate_root / "provider-recovery-gate.db",
        )
        candidate_extensions = build_runtime_extensions(candidate_settings)
        candidate_repository = candidate_extensions["media_repository"]
        try:
            with patch.object(
                candidate_repository,
                "recover_provider_egress_state",
                side_effect=RuntimeError("injected provider recovery failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected provider recovery failure",
                ):
                    swap_runtime(self.app, candidate_settings, candidate_extensions)
            self.assertIs(manager.current_bundle, old_bundle)
        finally:
            shutdown_runtime_extensions(candidate_extensions)

    def test_runtime_swap_keeps_creator_write_on_the_validated_bundle(self) -> None:
        manager = self.app.extensions["runtime_manager"]
        old_bundle = manager.current_bundle
        old_repository = old_bundle.extension("media_repository")
        old_runner = old_bundle.extension("media_job_runner")

        candidate_root = Path(self.temporary_directory.name) / "creator-runtime-b"
        candidate_library = candidate_root / "photos"
        candidate_library.mkdir(parents=True)
        candidate_settings = replace(
            old_bundle.settings,
            image_library_dir=candidate_library,
            db_path=candidate_root / "runtime-b.db",
        )
        candidate_extensions = build_runtime_extensions(candidate_settings)
        new_repository = candidate_extensions["media_repository"]

        validated = threading.Event()
        continue_request = threading.Event()
        creator_service = old_bundle.extension("creator_memory_service")
        original_update = creator_service.update_profile

        def blocked_update(*args, **kwargs):
            validated.set()
            if not continue_request.wait(timeout=10):
                raise RuntimeError("runtime barrier timed out")
            return original_update(*args, **kwargs)

        payload = {
            "db_path": str(old_repository.db_path),
            "base_revision": 0,
            "profile": {"tone": "warm"},
            "source": "user_edit",
        }
        response_holder: list[object] = []

        def send_old_request() -> None:
            with self.app.test_client() as client:
                response_holder.append(
                    client.put(
                        "/v1/creator/profile",
                        json=payload,
                        headers={
                            **self.desktop_headers,
                            "Idempotency-Key": "creator-runtime-pinned-a",
                        },
                    )
                )

        with (
            patch.object(creator_service, "update_profile", side_effect=blocked_update),
            patch.object(old_runner, "shutdown", wraps=old_runner.shutdown) as shutdown,
        ):
            request_thread = threading.Thread(target=send_old_request)
            request_thread.start()
            self.assertTrue(validated.wait(timeout=10))
            swap_runtime(self.app, candidate_settings, candidate_extensions)
            shutdown.assert_not_called()
            continue_request.set()
            request_thread.join(timeout=10)
            self.assertFalse(request_thread.is_alive())
            self.assertEqual(response_holder[0].status_code, 200)
            shutdown.assert_called_once_with()

        old_profile = old_repository.get_creator_profile(profile_id="default")
        self.assertEqual(old_profile["profile"], {"tone": "warm"})
        self.assertIsNone(new_repository.get_creator_profile(profile_id="default"))

        mismatch = self.client.put(
            "/v1/creator/profile",
            json=payload,
            headers={
                **self.desktop_headers,
                "Idempotency-Key": "creator-runtime-mismatch-b",
            },
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json["code"], "database_binding_mismatch")

        current = self.client.put(
            "/v1/creator/profile",
            json={
                "db_path": str(new_repository.db_path),
                "base_revision": 0,
                "profile": {"tone": "cool"},
                "source": "user_edit",
            },
            headers={
                **self.desktop_headers,
                "Idempotency-Key": "creator-runtime-current-b",
            },
        )
        self.assertEqual(current.status_code, 200)
        self.assertEqual(
            new_repository.get_creator_profile(profile_id="default")["profile"],
            {"tone": "cool"},
        )

    def test_indexing_distinguishes_empty_and_all_failed_jobs(self) -> None:
        root_metadata = self.photos_dir.stat()
        empty_response = self.client.post(
            "/v1/indexing/jobs",
            json={
                "image_dir": str(self.photos_dir),
                "library_root_identity": {
                    "device": str(root_metadata.st_dev),
                    "inode": str(root_metadata.st_ino),
                },
                "persist_to_server": True,
            },
            headers=self.desktop_headers,
        )
        failed_response = self.client.post(
            "/v1/indexing/jobs",
            json={
                "input": {
                    "image": {
                        "filename": "broken.jpg",
                        "b64": "not-valid-base64",
                    }
                }
            },
            headers=self.desktop_headers,
        )

        self.assertEqual(empty_response.status_code, 200)
        self.assertEqual(empty_response.json["status"], "empty")
        self.assertEqual(failed_response.status_code, 409)
        self.assertEqual(
            failed_response.json["code"],
            "legacy_inline_image_unmanaged",
        )

    def test_indexing_rejects_non_object_json(self) -> None:
        response = self.client.post(
            "/v1/indexing/jobs",
            json=["not", "an", "object"],
            headers=self.desktop_headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_index_status_is_scoped_to_canonical_existing_db(self) -> None:
        alternate_db_path = Path(self.temporary_directory.name) / "alternate.db"
        alternate_repository = ImageIndexRepository(alternate_db_path)
        alternate_repository.ensure_schema()
        alternate_repository.upsert(
            _record(
                image_id="alternate",
                sha256="d" * 64,
                relative_path="alternate.jpg",
            )
        )

        response = self.client.get(
            "/v1/index/status",
            query_string={"db_path": str(alternate_db_path.parent / "." / alternate_db_path.name)},
            headers=self.desktop_headers,
        )
        missing_response = self.client.get(
            "/v1/index/status",
            query_string={"db_path": str(alternate_db_path.with_name("missing.db"))},
            headers=self.desktop_headers,
        )
        invalid_db_path = alternate_db_path.with_name("not-sqlite.db")
        invalid_db_path.write_text("not sqlite", encoding="utf-8")
        invalid_response = self.client.get(
            "/v1/index/status",
            query_string={"db_path": str(invalid_db_path)},
            headers=self.desktop_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["object"], "image_index.status")
        self.assertEqual(response.json["db_path"], str(alternate_db_path.resolve()))
        self.assertEqual(response.json["index_stats"]["total_records"], 1)
        self.assertEqual(missing_response.status_code, 400)
        self.assertEqual(invalid_response.status_code, 400)

    def test_retrieval_copy_accepts_indexed_metadata_without_vision_text(
        self,
    ) -> None:
        response = self.client.post(
            "/v1/retrieval/copy",
            json={
                "query_text": "quiet beach sunset",
                "image_library_dir": str(self.photos_dir),
                "images": [
                    {
                        "id": "asset_" + "0" * 24,
                        "filename": "quiet-beach.jpg",
                        "relative_path": "quiet-beach.jpg",
                        "description": "",
                        "tags": [],
                        "score": 1.0,
                        "matched_terms": [],
                    }
                ],
            },
            headers=self.desktop_headers,
        )

        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["generated_copy"]["model"], "local_fallback")
        self.assertIsInstance(response.json["caption"], str)

    def test_retrieval_copy_routes_never_send_original_images(self) -> None:
        asset_id = "asset_" + "0" * 24
        analysis_binding = {
            "analysis_run_id": "arun_" + "1" * 32,
            "revision": 1,
            "content_sha256": "2" * 64,
        }
        projection = {
            "status": "current",
            "generation_id": "generation_active",
            "processing_generation_id": "generation_processing",
            "receipt_sha256": "3" * 64,
            "row_sha256": "4" * 64,
            "reason_code": None,
        }
        observation = {
            "object": "memolens.canonical_image_observation",
            "schema_version": "1",
            "status": "current",
            "authority": "canonical_image_analysis",
            "provenance_status": "verified_current",
            "asset_id": asset_id,
            "analysis_binding": analysis_binding,
            "source_binding_sha256": "5" * 64,
            "projection": projection,
            "stages": {
                stage: {
                    "status": "disabled",
                    "provenance": {},
                    "output": None,
                    "reason_code": "test_disabled",
                }
                for stage in IMAGE_ANALYSIS_STAGES
            },
            "reason_code": None,
        }
        retrieved = RetrievedImageSummary(
            id=asset_id,
            filename="one.jpg",
            relative_path="one.jpg",
            taken_at=None,
            place_name=None,
            country=None,
            description="A quiet mountain lake.",
            tags=["mountain", "lake"],
            score=0.9,
            matched_terms=["mountain"],
        )
        generated = GeneratedCopy(
            model="text-model",
            title="Mountain lake",
            body="A quiet mountain lake.",
            highlights=["mountain"],
            image_count=0,
        )
        retrieval_response = CanonicalRetrievalResponse(
            id="query-1",
            query_text="mountain lake",
            current_datetime="2026-01-01T00:00:00+00:00",
            parsed_query=None,
            data=[retrieved],
            status="completed",
            canonical_projection_bindings={
                asset_id: {
                    "generation_id": projection["generation_id"],
                    "analysis_binding": analysis_binding,
                    "row_sha256": projection["row_sha256"],
                }
            },
        )
        bundle = self.app.extensions["runtime_manager"].current_bundle
        retrieval_service = bundle.extension("retrieval_service")
        copywriter = bundle.extension("retrieval_copywriter")
        media_repository = bundle.extension("media_repository")
        with (
            patch.object(retrieval_service, "run", return_value=retrieval_response),
            patch.object(
                media_repository,
                "canonical_image_consumer_states",
                return_value={
                    asset_id: {
                        "analysis_status": "current",
                        "canonical_image_observation": observation,
                    }
                },
                create=True,
            ),
            patch.object(copywriter, "generate", return_value=generated) as generate,
            patch(
                "backend.src.api.routes._legacy_provider_egress_authorized",
                return_value=True,
            ),
        ):
            query_response = self.client.post(
                "/v1/retrieval/query",
                json={"text": "mountain lake", "include_copy": True},
                headers=self.desktop_headers,
            )
            copy_response = self.client.post(
                "/v1/retrieval/copy",
                json={
                    "query_text": "mountain lake",
                    "images": [retrieved.to_dict()],
                },
                headers=self.desktop_headers,
            )

        self.assertEqual(query_response.status_code, 200)
        self.assertEqual(copy_response.status_code, 200)
        self.assertEqual(generate.call_count, 2)
        self.assertTrue(all(call.kwargs["image_limit"] == 0 for call in generate.call_args_list))


if __name__ == "__main__":
    unittest.main()
