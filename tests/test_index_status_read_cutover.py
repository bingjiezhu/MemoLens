from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src.api import api_blueprint
from core.db import FALLBACK_DESCRIPTION_PREFIX, ImageIndexRepository
from core.media_db import MediaRepository
from core.schemas import StoredImageRecord
from tests import test_image_projection_rebuild as rebuild_fixture


NOW = "2026-08-29T12:00:00+00:00"


def _legacy_record(*, image_id: str, digest: str) -> StoredImageRecord:
    return StoredImageRecord(
        id=image_id,
        sha256=digest,
        filename=f"{image_id}.jpg",
        relative_path=f"{image_id}.jpg",
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
        embedding_blob=b"legacy-embedding",
        created_at=NOW,
        updated_at=NOW,
    )


class _ReplaceAfterFirstImageRead:
    """Inject one committed writer after the first image-index SELECT result."""

    def __init__(self, connection: sqlite3.Connection, db_path: Path) -> None:
        self._connection = connection
        self._db_path = db_path
        self.triggered = False

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        cursor = self._connection.execute(statement, parameters)
        normalized = " ".join(statement.upper().split())
        if (
            not self.triggered
            and normalized.startswith("SELECT")
            and "FROM IMAGE_INDEX" in normalized
        ):
            self.triggered = True
            with closing(sqlite3.connect(self._db_path, timeout=5)) as writer, writer:
                writer.execute(
                    "UPDATE image_index SET description=?",
                    (f"{FALLBACK_DESCRIPTION_PREFIX}concurrent.jpg",),
                )
        return cursor

    def __enter__(self) -> _ReplaceAfterFirstImageRead:
        self._connection.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._connection.__exit__(*args)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class ManagedIndexStatusReadCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = rebuild_fixture.SameHighWaterProjectionRebuildTests(
            methodName="runTest"
        )
        self.harness.setUp()
        self.repository = ImageIndexRepository(self.harness.fixture.db_path)

    def tearDown(self) -> None:
        self.harness.tearDown()

    def _stats(self) -> dict[str, int | float | bool | str | None]:
        return self.repository.summarize_index_health()

    def _assert_projection_unavailable(
        self,
        stats: dict[str, int | float | bool | str | None],
    ) -> None:
        for field in (
            "projection_ready",
            "projection_status",
            "projection_reason_code",
            "projection_generation_id",
        ):
            self.assertIn(field, stats)
        self.assertIs(stats["projection_ready"], False)
        self.assertEqual(stats["projection_status"], "unavailable")
        self.assertIsInstance(stats["projection_reason_code"], str)
        self.assertTrue(stats["projection_reason_code"])
        self.assertEqual(stats["total_records"], 0)
        self.assertEqual(stats["fallback_records"], 0)
        self.assertEqual(stats["fallback_ratio"], 0.0)
        self.assertEqual(stats["aesthetic_records"], 0)
        self.assertEqual(stats["aesthetic_missing"], 0)
        self.assertIs(stats["needs_reindex"], False)

    def test_clean_active_v17_counts_only_verified_canonical_rows(self) -> None:
        stats = self._stats()

        for field in (
            "projection_ready",
            "projection_status",
            "projection_reason_code",
            "projection_generation_id",
        ):
            self.assertIn(field, stats)
        self.assertIs(stats["projection_ready"], True)
        self.assertEqual(stats["projection_status"], "ready")
        self.assertIsNone(stats["projection_reason_code"])
        self.assertEqual(
            stats["projection_generation_id"],
            self.harness.old_generation_id,
        )
        self.assertEqual(stats["total_records"], 2)
        self.assertLessEqual(stats["fallback_records"], stats["total_records"])
        self.assertLessEqual(stats["aesthetic_records"], stats["total_records"])

    def test_unmanaged_physical_row_makes_managed_status_unavailable(self) -> None:
        self.harness._insert_unmanaged_physical_row()

        self._assert_projection_unavailable(self._stats())

    def test_physical_canonical_row_tamper_makes_status_unavailable(self) -> None:
        with self.harness.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description=? WHERE id=?",
                ("tampered physical ranking text", self.harness.fixture.asset_id),
            )

        self._assert_projection_unavailable(self._stats())

    def test_missing_active_generation_makes_status_unavailable(self) -> None:
        with self.harness.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_projection_generations SET is_active=0 WHERE is_active=1"
            )

        self._assert_projection_unavailable(self._stats())

    def test_dirty_active_read_manifest_makes_status_unavailable(self) -> None:
        self.harness._insert_unmanaged_physical_row()
        dirty = self.harness._rebuild("index-status-dirty-read-manifest")
        dirty_generation_id = str(dirty["generation_id"])
        self.assertGreater(dirty["manifest"]["counts"]["unexpected"], 0)

        # Simulate persisted corruption: the production activation trigger would
        # reject this state, but every current reader still has to fail closed if
        # an out-of-band SQLite writer bypasses that admission boundary.
        with self.harness.fixture.repository.transaction(immediate=True) as connection:
            connection.execute("DROP TRIGGER trg_image_generations_activate_clean_only")
            connection.execute(
                "UPDATE image_projection_generations SET is_active=0 WHERE is_active=1"
            )
            connection.execute(
                "UPDATE image_projection_generations SET is_active=1 WHERE id=?",
                (dirty_generation_id,),
            )
            connection.execute(
                MediaRepository._expected_v17_physical_schema()[
                    ("trigger", "trg_image_generations_activate_clean_only")
                ]
            )

        self._assert_projection_unavailable(self._stats())

    def test_all_index_stats_are_from_one_sqlite_snapshot(self) -> None:
        before = self._stats()
        connection = self.repository._connect()
        raced = _ReplaceAfterFirstImageRead(
            connection,
            self.harness.fixture.db_path,
        )

        with patch.object(self.repository, "_connect", return_value=raced):
            observed = self._stats()

        self.assertTrue(raced.triggered)
        self.assertEqual(observed, before)

    def _settings_app(self) -> tuple[Flask, SimpleNamespace]:
        app = Flask(__name__)
        app.register_blueprint(api_blueprint)
        root = self.harness.fixture.root
        settings = SimpleNamespace(
            image_library_dir=self.harness.fixture.library,
            db_path=self.harness.fixture.db_path,
            app_state_dir=root / "app-state",
            persisted_settings_path=root / "app-state" / "settings.json",
            process_image_width=1024,
            vision_profile_name="local",
            query_profile_name="local",
            embedding_backend="semantic_hash",
            available_vlm_profiles=(),
            vlm_profile_catalog=(),
        )
        return app, settings

    def test_settings_never_reports_dirty_managed_index_as_ready(self) -> None:
        self.harness._insert_unmanaged_physical_row()
        app, settings = self._settings_app()

        def extension(name: str) -> object:
            return {
                "image_index_repository": self.repository,
                "media_repository": self.harness.fixture.repository,
            }[name]

        with (
            patch("backend.src.api.routes._runtime_settings", return_value=settings),
            patch("backend.src.api.routes._runtime_extension", side_effect=extension),
            patch(
                "backend.src.api.routes.load_persisted_app_settings",
                return_value=SimpleNamespace(to_dict=lambda: {}),
            ),
            patch(
                "backend.src.api.routes.detect_local_model_runtime",
                return_value=SimpleNamespace(to_dict=lambda: {}),
            ),
            app.test_client() as client,
        ):
            response = client.get("/v1/settings")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self._assert_projection_unavailable(response.json["index_stats"])

    def test_index_status_never_reports_unmanaged_managed_row_as_ready(self) -> None:
        self.harness._insert_unmanaged_physical_row()
        app = Flask(__name__)
        app.register_blueprint(api_blueprint)

        with app.test_client() as client:
            response = client.get(
                "/v1/index/status",
                query_string={"db_path": str(self.harness.fixture.db_path)},
            )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self._assert_projection_unavailable(response.json["index_stats"])


class IndexStatusDatabaseIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-index-status-identity-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / "target.db"
        self.replacement = self.root / "replacement.db"
        for path, image_id, digest in (
            (self.target, "target", "a" * 64),
            (self.replacement, "replacement", "b" * 64),
        ):
            repository = ImageIndexRepository(path)
            repository.ensure_schema()
            repository.upsert(_legacy_record(image_id=image_id, digest=digest))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_arbitrary_status_path_replacement_fails_closed(self) -> None:
        app = Flask(__name__)
        app.register_blueprint(api_blueprint)
        original = ImageIndexRepository.summarize_index_health
        replaced = False

        def replace_before_open(
            repository: ImageIndexRepository,
            *args: object,
            **kwargs: object,
        ) -> dict[str, int | float | bool | str | None]:
            nonlocal replaced
            if not replaced and repository.db_path == self.target:
                replaced = True
                os.replace(self.replacement, self.target)
            return original(repository, *args, **kwargs)

        with (
            patch.object(
                ImageIndexRepository,
                "summarize_index_health",
                autospec=True,
                side_effect=replace_before_open,
            ),
            app.test_client() as client,
        ):
            response = client.get(
                "/v1/index/status",
                query_string={"db_path": str(self.target)},
            )

        self.assertTrue(replaced)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(
            response.json["code"],
            "image_index_database_scope_changed",
        )


if __name__ == "__main__":
    unittest.main()
