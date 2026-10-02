from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src.api import api_blueprint
from core.db import ImageIndexRepository
from core.image_projection_renderer import IMAGE_INDEX_SQL_COLUMNS
from core.media_db import MediaRepository
from core.photo_atlas import (
    ATLAS_A0_RESIDUALS,
    ATLAS_ASSET_OBSERVATIONS_MAX_CANONICAL_BYTES,
    ATLAS_PROJECTION_ERROR_CODE,
    AtlasAssetObservationConflictError,
    AtlasProjectionNotReadyError,
    PhotoAtlasService,
)
from core.schemas import StoredImageRecord
from tests import test_image_projection_materialization as materialization_fixture


class AtlasVerifiedImageConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.materialization = materialization_fixture.ImageProjectionMaterializationTests(
            methodName="runTest"
        )
        self.materialization.setUp()
        self.fixture = self.materialization.fixture
        self.materialization._project()
        self.service = PhotoAtlasService(
            ImageIndexRepository(self.fixture.db_path),
            self.fixture.repository,
        )

    def tearDown(self) -> None:
        self.materialization.tearDown()

    def _insert_orphan_physical_row(self) -> str:
        orphan_id = "orphan-image-index-row"
        columns_sql = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            source = connection.execute(
                f"SELECT {columns_sql} FROM image_index WHERE id=?",
                (self.fixture.asset_id,),
            ).fetchone()
            assert source is not None
            document = dict(source)
            document.update(
                {
                    "id": orphan_id,
                    "sha256": "b" * 64,
                    "filename": "orphan.png",
                    "relative_path": "orphan.png",
                }
            )
            connection.execute(
                f"INSERT INTO image_index({columns_sql}) VALUES({','.join('?' for _ in IMAGE_INDEX_SQL_COLUMNS)})",
                tuple(document[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )
        return orphan_id

    def _database_digest(self) -> str:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            document = "\n".join(connection.iterdump()).encode("utf-8")
        return hashlib.sha256(document).hexdigest()

    def _current_observation(self) -> dict[str, object]:
        self.service.rebuild()
        return self.service.overview()["assets"][0]["canonical_image_observation"]

    def test_rebuild_and_query_emit_only_exact_canonical_current_evidence(self) -> None:
        rebuilt = self.service.rebuild()
        self.assertEqual(rebuilt["status"], "completed")
        self.assertEqual(rebuilt["verified_image_count"], 1)
        self.assertEqual(rebuilt["parity_gap_count"], 0)
        self.assertEqual(tuple(rebuilt["residuals"]), ATLAS_A0_RESIDUALS)

        overview = self.service.overview()
        self.assertEqual(overview["asset_count"], 1)
        asset = overview["assets"][0]
        self.assertEqual(asset["id"], self.fixture.asset_id)
        self.assertEqual(asset["analysis_binding"], self.fixture.analysis_binding)
        self.assertEqual(asset["analysis_status"], "current")
        self.assertEqual(asset["projection"]["status"], "current")
        self.assertEqual(asset["projection"]["row_sha256"], self.fixture.row_sha256)
        expected_observation = asset["canonical_image_observation"]
        self.assertEqual(
            set(expected_observation),
            {
                "object",
                "schema_version",
                "status",
                "authority",
                "provenance_status",
                "asset_id",
                "analysis_binding",
                "source_binding_sha256",
                "projection",
                "stages",
                "reason_code",
            },
        )
        self.assertEqual(
            expected_observation["object"],
            "memolens.canonical_image_observation",
        )
        self.assertEqual(expected_observation["status"], "current")
        self.assertEqual(
            expected_observation["provenance_status"],
            "verified_current",
        )
        self.assertEqual(
            expected_observation["analysis_binding"],
            self.fixture.analysis_binding,
        )
        self.assertEqual(
            expected_observation["source_binding_sha256"],
            self.fixture.source_binding_sha256,
        )
        self.assertIsNone(expected_observation["projection"]["reason_code"])
        self.assertEqual(
            set(expected_observation["stages"]),
            {"metadata", "geocode", "vision", "embedding", "quality"},
        )
        self.assertEqual(
            asset["canonical_image_observation"],
            expected_observation,
        )

        workbench = self.service.workbench()
        representative = workbench["memories"][0]["representative_assets"][0]
        self.assertEqual(
            representative["canonical_image_observation"],
            expected_observation,
        )
        saved = self.service.save_basket(asset_ids=[self.fixture.asset_id])
        self.assertEqual(
            saved["basket"]["assets"][0]["canonical_image_observation"],
            expected_observation,
        )
        generated = self.service.generate(
            text="guard",
            asset_ids=[self.fixture.asset_id],
            asset_observations=[expected_observation],
        )
        self.assertEqual(
            generated["data"][0]["canonical_image_observation"],
            expected_observation,
        )

    def test_explicit_basket_generate_requires_exact_observation_and_one_snapshot(
        self,
    ) -> None:
        observation = self._current_observation()
        before = self._database_digest()
        original_connect = self.service._connect
        modes: list[bool] = []

        def audited_connect(*, read_only: bool = False):
            modes.append(read_only)
            return original_connect(read_only=read_only)

        with (
            patch.object(
                self.service,
                "overview",
                side_effect=AssertionError("public overview opened a second snapshot"),
            ),
            patch.object(self.service, "_connect", side_effect=audited_connect),
        ):
            generated = self.service.generate(
                text="guard",
                asset_ids=[self.fixture.asset_id],
                asset_observations=[observation],
            )

        self.assertEqual(modes, [True])
        self.assertEqual(
            generated["data"][0]["canonical_image_observation"],
            observation,
        )
        self.assertEqual(self._database_digest(), before)

    def test_selection_revision_change_is_stable_conflict_and_zero_write(self) -> None:
        observation = self._current_observation()
        self.materialization._publish_revision_two()
        before = self._database_digest()

        with self.assertRaises(AtlasAssetObservationConflictError) as failure:
            self.service.generate(
                text="guard",
                asset_ids=[self.fixture.asset_id],
                asset_observations=[observation],
            )

        self.assertEqual(failure.exception.code, "atlas_asset_observation_conflict")
        self.assertEqual(self._database_digest(), before)

        app = Flask(__name__)
        app.register_blueprint(api_blueprint)
        with patch(
            "backend.src.api.routes._atlas_service_for_db_path",
            return_value=self.service,
        ), app.test_client() as client:
            response = client.post(
                "/v1/atlas/generate",
                json={
                    "text": "guard",
                    "asset_ids": [self.fixture.asset_id],
                    "asset_observations": [observation],
                },
            )
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(
            response.json["code"],
            "atlas_asset_observation_conflict",
        )
        self.assertEqual(self._database_digest(), before)

    def test_explicit_basket_observation_shape_order_and_bounds_fail_closed(self) -> None:
        observation = self._current_observation()
        projection_conflict = json.loads(json.dumps(observation))
        projection_conflict["projection"]["row_sha256"] = "a" * 64
        stages_conflict = json.loads(json.dumps(observation))
        stages_conflict["stages"]["vision"]["reason_code"] = "changed"
        malformed = {
            "missing": None,
            "extra": [observation, observation],
            "unknown_key": [{**observation, "unexpected": True}],
            "pending": [{**observation, "status": "pending"}],
            "unavailable": [
                {
                    **observation,
                    "status": "unavailable",
                    "provenance_status": "unavailable",
                }
            ],
            "order_mismatch": [{**observation, "asset_id": "asset_other"}],
            "source_binding_mismatch": [
                {**observation, "source_binding_sha256": "b" * 64}
            ],
            "projection_mismatch": [projection_conflict],
            "stages_mismatch": [stages_conflict],
        }
        before = self._database_digest()
        for label, asset_observations in malformed.items():
            with self.subTest(label=label), self.assertRaises(
                AtlasAssetObservationConflictError
            ):
                self.service.generate(
                    text="guard",
                    asset_ids=[self.fixture.asset_id],
                    asset_observations=asset_observations,
                )

        with self.assertRaises(AtlasAssetObservationConflictError):
            self.service.generate(
                text="guard",
                asset_ids=[self.fixture.asset_id, self.fixture.asset_id],
                asset_observations=[observation, observation],
            )
        with self.assertRaises(AtlasAssetObservationConflictError):
            self.service.generate(
                text="guard",
                asset_ids=[f"asset_{index}" for index in range(25)],
                asset_observations=[observation for _index in range(25)],
            )
        with self.assertRaises(AtlasAssetObservationConflictError):
            self.service.generate(
                text="guard",
                asset_observations=[observation],
            )
        self.assertEqual(self._database_digest(), before)

    def test_atlas_generate_route_forwards_exact_observations_and_rejects_bad_envelopes(
        self,
    ) -> None:
        observation = self._current_observation()
        app = Flask(__name__)
        app.register_blueprint(api_blueprint)
        payload = {
            "text": "guard",
            "asset_ids": [self.fixture.asset_id],
            "asset_observations": [observation],
        }

        with patch(
            "backend.src.api.routes._atlas_service_for_db_path",
            return_value=self.service,
        ), app.test_client() as client:
            success = client.post("/v1/atlas/generate", json=payload)
            self.assertEqual(success.status_code, 200, success.json)
            self.assertEqual(
                success.json["data"][0]["canonical_image_observation"],
                observation,
            )

            for label, update in {
                "missing": {"asset_observations": None},
                "invalid": {"asset_observations": {"asset_id": self.fixture.asset_id}},
                "extra": {"asset_observations": [observation, observation]},
                "duplicate": {
                    "asset_ids": [self.fixture.asset_id, self.fixture.asset_id],
                    "asset_observations": [observation, observation],
                },
                "order": {
                    "asset_observations": [
                        {**observation, "asset_id": "asset_other"}
                    ]
                },
                "unknown_key": {
                    "asset_observations": [{**observation, "unexpected": True}]
                },
            }.items():
                malformed = {**payload, **update}
                if update.get("asset_observations") is None:
                    malformed.pop("asset_observations")
                with self.subTest(label=label):
                    response = client.post("/v1/atlas/generate", json=malformed)
                    self.assertEqual(response.status_code, 409, response.json)
                    self.assertEqual(
                        response.json["code"],
                        "atlas_asset_observation_conflict",
                    )

            oversized = {
                **payload,
                "asset_observations": [
                    {
                        **observation,
                        "unexpected": "x"
                        * (ATLAS_ASSET_OBSERVATIONS_MAX_CANONICAL_BYTES + 1),
                    }
                ],
            }
            response = client.post("/v1/atlas/generate", json=oversized)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(
                response.json["code"],
                "atlas_asset_observation_conflict",
            )
            response = client.post(
                "/v1/atlas/generate",
                json={"text": "guard", "asset_observations": [observation]},
            )
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(
                response.json["code"],
                "atlas_asset_observation_conflict",
            )

    def test_selected_memories_are_resolved_inside_generate_snapshot(self) -> None:
        self.service.rebuild()
        memory_id = str(self.service.workbench()["memories"][0]["id"])
        original_connect = self.service._connect
        modes: list[bool] = []

        def audited_connect(*, read_only: bool = False):
            modes.append(read_only)
            return original_connect(read_only=read_only)

        with (
            patch.object(
                self.service,
                "asset_ids_for_memories",
                side_effect=AssertionError("public memory resolver opened another snapshot"),
            ),
            patch.object(
                self.service,
                "overview",
                side_effect=AssertionError("public overview opened another snapshot"),
            ),
            patch.object(self.service, "_connect", side_effect=audited_connect),
        ):
            generated = self.service.generate(
                text="guard",
                selected_memory_ids=[memory_id],
            )

        self.assertEqual(modes, [True])
        self.assertEqual(generated["data"][0]["id"], self.fixture.asset_id)

    def test_retrieval_adapter_rejects_missing_or_contradictory_current_proof(
        self,
    ) -> None:
        self.service.rebuild()
        asset = self.service.overview()["assets"][0]
        missing = dict(asset)
        missing.pop("canonical_image_observation")
        with self.assertRaisesRegex(
            RuntimeError,
            "atlas_current_image_observation_missing",
        ):
            self.service._asset_to_retrieval_image(missing)

        forged = dict(asset)
        forged["canonical_image_observation"] = {
            **asset["canonical_image_observation"],
            "asset_id": "asset_forged",
        }
        with self.assertRaisesRegex(
            RuntimeError,
            "atlas_current_image_observation_conflict",
        ):
            self.service._asset_to_retrieval_image(forged)

    def test_orphan_row_is_excluded_and_reported_as_path_free_parity_gap(self) -> None:
        orphan_id = self._insert_orphan_physical_row()

        rebuilt = self.service.rebuild()

        self.assertEqual(rebuilt["verified_image_count"], 1)
        self.assertEqual(rebuilt["parity_gap_count"], 1)
        self.assertEqual(
            rebuilt["parity_gaps"],
            [
                {
                    "asset_id": orphan_id,
                    "identity_kind": "unverified_image_index_row",
                    "reason_code": "orphan_or_unmanaged_image_index_row",
                }
            ],
        )
        self.assertNotIn(str(self.fixture.library), str(rebuilt["parity_gaps"]))
        self.assertEqual(
            [asset["id"] for asset in self.service.overview()["assets"]],
            [self.fixture.asset_id],
        )

    def test_active_generation_scope_excludes_high_score_unmanaged_row_before_ordering(
        self,
    ) -> None:
        orphan_id = self._insert_orphan_physical_row()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET aesthetic_score=? WHERE id=?",
                (1000.0, orphan_id),
            )

        with closing(self.service._connect(read_only=True)) as connection:
            candidate_rows = self.service._active_generation_image_rows(connection)

        self.assertEqual(
            [str(row["id"]) for row in candidate_rows],
            [self.fixture.asset_id],
        )
        rebuilt = self.service.rebuild()
        self.assertEqual(rebuilt["verified_image_count"], 1)
        self.assertEqual(rebuilt["parity_gap_count"], 1)
        self.assertEqual(rebuilt["parity_gaps"][0]["asset_id"], orphan_id)
        self.assertEqual(
            [asset["id"] for asset in self.service.overview()["assets"]],
            [self.fixture.asset_id],
        )

    def test_stale_head_is_a_gap_and_never_reuses_the_old_atlas_row(self) -> None:
        self.materialization._publish_revision_two()

        rebuilt = self.service.rebuild()

        self.assertEqual(rebuilt["verified_image_count"], 0)
        self.assertEqual(rebuilt["parity_gap_count"], 1)
        self.assertEqual(rebuilt["parity_gaps"][0]["asset_id"], self.fixture.asset_id)
        self.assertEqual(
            rebuilt["parity_gaps"][0]["reason_code"],
            "image_projection_pending",
        )
        self.assertEqual(self.service.overview()["assets"], [])

    def test_bad_physical_digest_invalidates_query_until_a_clean_rebuild(self) -> None:
        self.service.rebuild()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description='tampered' WHERE id=?",
                (self.fixture.asset_id,),
            )

        with self.assertRaises(AtlasProjectionNotReadyError) as failure:
            self.service.overview()
        self.assertEqual(failure.exception.code, ATLAS_PROJECTION_ERROR_CODE)
        self.assertEqual(
            failure.exception.health["projection_reason"],
            "projection_identity_mismatch",
        )

        rebuilt = self.service.rebuild()
        self.assertEqual(rebuilt["verified_image_count"], 0)
        self.assertEqual(
            rebuilt["parity_gaps"][0]["reason_code"],
            "image_analysis_projection_corrupt",
        )
        self.assertEqual(self.service.overview()["assets"], [])

    def test_wrong_database_repository_is_typed_unavailable(self) -> None:
        wrong_root = Path(self.fixture.temporary.name) / "wrong-library"
        wrong_root.mkdir()
        wrong_db = Path(self.fixture.temporary.name) / "wrong.db"
        ImageIndexRepository(wrong_db).ensure_schema()
        wrong_repository = MediaRepository(wrong_db)
        self.addCleanup(wrong_repository.close)
        wrong_repository.ensure_schema(wrong_root)
        service = PhotoAtlasService(
            ImageIndexRepository(self.fixture.db_path),
            wrong_repository,
        )

        with self.assertRaises(AtlasProjectionNotReadyError) as failure:
            service.rebuild()

        self.assertEqual(
            failure.exception.health["projection_reason"],
            "verified_image_source_unavailable",
        )
        self.assertEqual(
            failure.exception.health["parity_gaps"][0]["reason_code"],
            "image_database_scope_changed",
        )
        self.assertNotIn(str(wrong_root), str(failure.exception.health["parity_gaps"]))

    def test_unmanaged_legacy_row_is_never_admitted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-atlas-legacy-") as directory:
            db_path = Path(directory) / "legacy.db"
            repository = ImageIndexRepository(db_path)
            repository.ensure_schema()
            repository.upsert(
                StoredImageRecord(
                    id="legacy-only",
                    sha256="c" * 64,
                    filename="legacy.jpg",
                    relative_path="legacy.jpg",
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
                    description="legacy",
                    tags=[],
                    combined_text="legacy",
                    text_embedding_model=None,
                    combined_text_embedding_blob=None,
                    embedding_backend="semantic_hash",
                    embedding_blob=b"\x00\x00\x80?",
                    created_at="2026-08-29T00:00:00+00:00",
                    updated_at="2026-08-29T00:00:00+00:00",
                )
            )
            service = PhotoAtlasService(repository)

            with self.assertRaises(AtlasProjectionNotReadyError) as failure:
                service.rebuild()

            self.assertEqual(
                failure.exception.health["parity_gaps"],
                [
                    {
                        "asset_id": "legacy-only",
                        "identity_kind": "unverified_image_index_row",
                        "reason_code": "canonical_image_source_adapter_unavailable",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
