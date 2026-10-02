from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from core.db import ImageIndexRepository
from core.media_db import BlueprintIntegrityError, MediaRepository
from core.photo_atlas import (
    ATLAS_PROJECTION_ERROR_CODE,
    AtlasProjectionNotReadyError,
    PhotoAtlasService,
)
from tests import test_image_projection_materialization as materialization_fixture


def _database_digest(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as connection:
        dump = "\n".join(connection.iterdump()).encode("utf-8")
    return hashlib.sha256(dump).hexdigest()


def _blueprint_semantic(label: str = "one") -> dict[str, object]:
    return {
        "intent": {
            "goal": "turn local memories into a short film",
            "stance": "ordinary moments deserve attention",
            "audience": "individual creators",
            "platform": "short-video",
        },
        "script": {
            "blocks": [{"block_id": "opening", "text": f"A memory, revision {label}."}]
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


class PhotoAtlasReadPurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.materialization = materialization_fixture.ImageProjectionMaterializationTests(
            methodName="runTest"
        )
        self.materialization.setUp()
        self.fixture = self.materialization.fixture
        self.materialization._project()
        self.temporary = self.fixture.temporary
        self.db_path = self.fixture.db_path
        self.repository = ImageIndexRepository(self.db_path)
        self.service = PhotoAtlasService(
            self.repository,
            self.fixture.repository,
        )

    def tearDown(self) -> None:
        self.materialization.tearDown()

    def _assert_read_is_pure(self, operation) -> object:
        before_file = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        before_digest = _database_digest(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as observer:
            before_schema = observer.execute("PRAGMA schema_version").fetchone()[0]
            before_data = observer.execute("PRAGMA data_version").fetchone()[0]
            result = operation()
            after_schema = observer.execute("PRAGMA schema_version").fetchone()[0]
            after_data = observer.execute("PRAGMA data_version").fetchone()[0]
        self.assertEqual(after_schema, before_schema)
        self.assertEqual(after_data, before_data)
        self.assertEqual(_database_digest(self.db_path), before_digest)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before_file)
        return result

    def test_status_and_failed_reads_do_not_create_atlas_schema(self) -> None:
        schema_before = _database_digest(self.db_path)
        status = self._assert_read_is_pure(self.service.status)
        self.assertEqual(status["projection_status"], "not_ready")
        self.assertEqual(status["error_code"], ATLAS_PROJECTION_ERROR_CODE)
        with self.assertRaises(AtlasProjectionNotReadyError) as failure:
            self._assert_read_is_pure(self.service.workbench)
        self.assertEqual(failure.exception.code, ATLAS_PROJECTION_ERROR_CODE)
        self.assertEqual(_database_digest(self.db_path), schema_before)

    def test_every_atlas_read_uses_read_only_connections_and_preserves_database(self) -> None:
        rebuilt = self.service.rebuild()
        self.assertEqual(rebuilt["status"], "completed")
        workbench = self.service.workbench()
        memory_id = str(workbench["memories"][0]["id"])
        asset_id = str(workbench["overview"]["assets"][0]["id"])
        asset_observation = workbench["overview"]["assets"][0][
            "canonical_image_observation"
        ]

        operations = {
            "status": self.service.status,
            "overview": self.service.overview,
            "workbench": self.service.workbench,
            "memory": lambda: self.service.memory(memory_id),
            "cleanup": self.service.cleanup,
            "load_basket": self.service.load_basket,
            "assets_by_ids": lambda: self.service.assets_by_ids([asset_id]),
            "query_preview": lambda: self.service.query_preview(text="quiet mountain"),
            "generate": lambda: self.service.generate(
                text="quiet mountain",
                top_k=1,
                asset_ids=[asset_id],
                asset_observations=[asset_observation],
            ),
        }
        original_connect = self.service._connect
        for name, operation in operations.items():
            modes: list[bool] = []

            def audited_connect(*, read_only: bool = False):
                modes.append(read_only)
                return original_connect(read_only=read_only)

            with self.subTest(operation=name), patch.object(
                self.service,
                "_connect",
                side_effect=audited_connect,
            ):
                self._assert_read_is_pure(operation)
                self.assertTrue(modes)
                self.assertTrue(all(modes), modes)

        with closing(self.service._connect(read_only=True)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM atlas_assets")

    def test_missing_derived_layer_is_not_rebuilt_by_a_read(self) -> None:
        self.service.rebuild()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DELETE FROM atlas_memories")

        status = self.service.status()
        self.assertEqual(status["projection_status"], "not_ready")
        self.assertEqual(status["projection_reason"], "projection_incomplete")
        digest_before = _database_digest(self.db_path)
        with self.assertRaises(AtlasProjectionNotReadyError):
            self._assert_read_is_pure(self.service.workbench)
        self.assertEqual(_database_digest(self.db_path), digest_before)

        rematerialized = self.service.rebuild()
        self.assertEqual(rematerialized["status"], "completed")
        self.assertEqual(self.service.status()["projection_status"], "ready")
        self.assertGreater(len(self.service.workbench()["memories"]), 0)

    def test_source_change_marks_projection_stale_without_rebuilding(self) -> None:
        self.service.rebuild()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE image_index SET description = ? WHERE id = ?",
                ("tampered outside the canonical projector", self.fixture.asset_id),
            )

        status = self._assert_read_is_pure(self.service.status)
        self.assertEqual(status["projection_status"], "not_ready")
        self.assertEqual(status["projection_reason"], "projection_identity_mismatch")
        with self.assertRaises(AtlasProjectionNotReadyError) as failure:
            self._assert_read_is_pure(self.service.cleanup)
        self.assertEqual(failure.exception.health["projection_status"], "not_ready")

    def test_routes_return_stable_not_ready_and_plugin_reads_remain_pure(self) -> None:
        app = Flask(__name__)
        app.extensions["photo_atlas_service"] = self.service
        app.extensions["image_index_repository"] = self.repository
        app.register_blueprint(api_blueprint)

        with app.test_client() as client:
            status = client.get("/v1/atlas/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["projection_status"], "not_ready")
            for path, method, payload in (
                ("/v1/atlas/workbench", "get", None),
                ("/v1/atlas/cleanup", "get", None),
                ("/v1/atlas/query-preview", "post", {"text": "mountain"}),
                ("/v1/atlas/generate", "post", {"text": "mountain"}),
            ):
                response = getattr(client, method)(path, json=payload)
                self.assertEqual(response.status_code, 409, (path, response.json))
                self.assertEqual(response.json["code"], ATLAS_PROJECTION_ERROR_CODE)
                self.assertEqual(response.json["status"], "not_ready")

        self.service.rebuild()
        before = _database_digest(self.db_path)
        with app.test_client() as client:
            workbench = client.get("/v1/atlas/workbench")
            cleanup = client.get("/v1/atlas/cleanup")
        self.assertEqual(workbench.status_code, 200, workbench.json)
        self.assertEqual(workbench.json["object"], "atlas.workbench")
        self.assertEqual(cleanup.status_code, 200, cleanup.json)
        self.assertEqual(cleanup.json["object"], "atlas.cleanup")
        self.assertEqual(_database_digest(self.db_path), before)

    def test_explicit_atlas_rebuild_preserves_blueprint_write_and_read(self) -> None:
        library = self.fixture.library
        media_repository = MediaRepository(self.db_path)
        self.addCleanup(media_repository.close)
        media_repository.ensure_schema(library)

        project = media_repository.create_project(
            "Atlas then Blueprint",
            {"goal": "prove schema composition", "candidate_refs": []},
            {"created_by": "atlas-read-purity-test"},
        )
        project_id = str(project["id"])
        brief = media_repository.get_brief(project_id, 1)
        assert brief is not None
        blueprint_service = BlueprintService(media_repository)
        initial = blueprint_service.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": _blueprint_semantic("one"),
            },
            idempotency_key="blueprint-before-atlas",
        )
        self.assertEqual(initial.response_status, 201)

        self.service.rebuild()
        head = media_repository.get_blueprint_head(project_id)
        self.assertIsNotNone(head)
        assert head is not None
        after_atlas = blueprint_service.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": _blueprint_semantic("two"),
            },
            idempotency_key="blueprint-after-atlas",
        )
        self.assertEqual(after_atlas.response_status, 201)
        current = media_repository.get_blueprint_head(project_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["revision"], 2)
        self.assertEqual(self.service.workbench()["status"], "completed")

    def test_atlas_allowlist_rejects_unknown_physical_schema_objects(self) -> None:
        tamper_cases = {
            "table": "CREATE TABLE atlas_attacker(value TEXT)",
            "column": "ALTER TABLE atlas_assets ADD COLUMN attacker TEXT",
            "index": "CREATE INDEX idx_atlas_attacker ON atlas_assets(filename)",
            "trigger": (
                "CREATE TRIGGER trg_atlas_attacker AFTER INSERT ON atlas_assets "
                "BEGIN SELECT 1; END"
            ),
        }
        for name, statement in tamper_cases.items():
            with self.subTest(tamper=name):
                child = Path(self.temporary.name) / name
                child.mkdir()
                library = child / "library"
                library.mkdir()
                db_path = child / "index.db"
                image_repository = ImageIndexRepository(db_path)
                image_repository.ensure_schema()
                media_repository = MediaRepository(db_path)
                media_repository.ensure_schema(library)
                PhotoAtlasService(image_repository, media_repository).rebuild()
                with closing(sqlite3.connect(db_path)) as connection, connection:
                    connection.execute(statement)
                with closing(media_repository._connect()) as connection:
                    with self.assertRaises(BlueprintIntegrityError):
                        media_repository._verify_blueprint_full_physical_schema(connection)
                media_repository.close()


if __name__ == "__main__":
    unittest.main()
