from __future__ import annotations

import hmac
import inspect
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import yaml

from backend.src import (
    DESKTOP_TOKEN_HEADER,
    MEMOLENS_API_VERSION,
    MEMOLENS_SERVICE_ID,
    create_app,
)
from backend.src.api.routes import _is_local_remote_addr
from backend.src.retrieval import RetrievalCopywriter
from core.config import Settings
from core.db import ImageIndexRepository
from core.schemas import (
    GeneratedCopy,
    RetrievalResponse,
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


class BackendBoundaryRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="memolens-api-test-")
        root = Path(self.temporary_directory.name)
        self.photos_dir = root / "photos"
        self.photos_dir.mkdir()
        self.desktop_token = "test-desktop-session-token"
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
            },
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()

    def tearDown(self) -> None:
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
            {"status", "object", "service", "api_version", "challenge_proof"},
        )
        self.assertEqual(response.json["service"], MEMOLENS_SERVICE_ID)
        self.assertEqual(response.json["api_version"], MEMOLENS_API_VERSION)
        self.assertEqual(
            response.json["challenge_proof"],
            hmac.new(
                self.desktop_token.encode(), challenge.encode(), "sha256"
            ).hexdigest(),
        )
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://127.0.0.1:5173")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

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
                    "Access-Control-Request-Headers": DESKTOP_TOKEN_HEADER,
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

    def test_indexing_distinguishes_empty_and_all_failed_jobs(self) -> None:
        empty_response = self.client.post(
            "/v1/indexing/jobs",
            json={"image_dir": str(self.photos_dir), "persist_to_server": True},
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
        self.assertEqual(failed_response.status_code, 200)
        self.assertEqual(failed_response.json["status"], "failed")
        self.assertEqual(len(failed_response.json["errors"]), 1)

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

    def test_retrieval_copy_routes_never_send_original_images(self) -> None:
        retrieved = RetrievedImageSummary(
            id="image-1",
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
        retrieval_service = Mock()
        retrieval_service.run.return_value = RetrievalResponse(
            id="query-1",
            query_text="mountain lake",
            current_datetime="2026-01-01T00:00:00+00:00",
            parsed_query=None,
            data=[retrieved],
            status="completed",
        )
        copywriter = Mock()
        copywriter.generate.return_value = generated
        self.app.extensions["retrieval_service"] = retrieval_service
        self.app.extensions["retrieval_copywriter"] = copywriter

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
        self.assertEqual(copywriter.generate.call_count, 2)
        self.assertTrue(
            all(call.kwargs["image_limit"] == 0 for call in copywriter.generate.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
