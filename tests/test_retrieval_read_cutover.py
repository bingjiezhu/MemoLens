from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src.api import api_blueprint
from backend.src.retrieval.retrieval import CanonicalRetrievalResponse
from core.db import ImageIndexRepository
from core.image_analysis_contract import IMAGE_ANALYSIS_STAGES
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import IMAGE_INDEX_SQL_COLUMNS
from core.schemas import RetrievedImageSummary
from tests import test_image_projection_materialization as materialization_fixture


class ManagedRetrievalCandidateCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.materialization = (
            materialization_fixture.ImageProjectionMaterializationTests(
                methodName="runTest"
            )
        )
        self.materialization.setUp()
        self.fixture = self.materialization.fixture
        self.materialization._project()

    def tearDown(self) -> None:
        self.materialization.tearDown()

    def _insert_unmanaged_high_score_row(self) -> str:
        orphan_id = "unmanaged-high-score-image"
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            source = connection.execute(
                f"SELECT {columns} FROM image_index WHERE id=?",
                (self.fixture.asset_id,),
            ).fetchone()
            assert source is not None
            row = dict(source)
            row.update(
                {
                    "id": orphan_id,
                    "sha256": "f" * 64,
                    "filename": "unmanaged.jpg",
                    "relative_path": "unmanaged.jpg",
                    "description": "target " * 100,
                    "combined_text": "target " * 100,
                }
            )
            connection.execute(
                f"INSERT INTO image_index({columns}) "
                f"VALUES({','.join('?' for _ in IMAGE_INDEX_SQL_COLUMNS)})",
                tuple(row[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )
        return orphan_id

    def test_managed_candidates_are_prefiltered_by_active_clean_generation(self) -> None:
        orphan_id = self._insert_unmanaged_high_score_row()
        repository = ImageIndexRepository(self.fixture.db_path)

        candidates = repository.fetch_candidates(location_text="target")

        self.assertEqual([row["id"] for row in candidates], [self.fixture.asset_id])
        self.assertNotIn(orphan_id, [row["id"] for row in candidates])
        row = candidates[0]
        self.assertEqual(
            row["canonical_analysis_run_id"],
            self.fixture.analysis_binding["analysis_run_id"],
        )
        self.assertEqual(
            row["canonical_analysis_revision"],
            self.fixture.analysis_binding["revision"],
        )
        self.assertEqual(
            row["canonical_content_sha256"],
            self.fixture.analysis_binding["content_sha256"],
        )
        self.assertEqual(row["canonical_row_sha256"], self.fixture.row_sha256)

    def test_managed_candidates_fail_closed_without_an_active_generation(self) -> None:
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_projection_generations SET is_active=0 WHERE is_active=1"
            )

        self.assertEqual(
            ImageIndexRepository(self.fixture.db_path).fetch_candidates(),
            [],
        )

    def test_managed_ranking_fields_come_from_immutable_generation_rows(self) -> None:
        repository = ImageIndexRepository(self.fixture.db_path)
        expected = repository.fetch_candidates()
        self.assertEqual(len(expected), 1)
        expected_row = expected[0]

        with self.fixture.repository.transaction(immediate=True) as connection:
            changed = connection.execute(
                """UPDATE image_index
                      SET filename='physical-only-poison.jpg',
                          description='physical only poison',
                          tags_json='[\"physical-only-poison\"]',
                          combined_text='physical only poison',
                          aesthetic_score=-1000
                    WHERE id=?""",
                (self.fixture.asset_id,),
            )
        self.assertEqual(changed.rowcount, 1)

        observed = repository.fetch_candidates(
            location_text="physical-only-poison"
        )

        self.assertEqual(len(observed), 1)
        for field in (
            "filename",
            "description",
            "tags_json",
            "combined_text",
            "aesthetic_score",
            "embedding",
            "combined_text_embedding",
        ):
            self.assertEqual(observed[0][field], expected_row[field])
        self.assertNotEqual(observed[0]["filename"], "physical-only-poison.jpg")

    def test_batched_consumer_reader_is_canonical_id_only_and_duplicate_free(
        self,
    ) -> None:
        states = self.fixture.repository.canonical_image_consumer_states(
            [self.fixture.asset_id]
        )

        self.assertEqual(tuple(states), (self.fixture.asset_id,))
        self.assertEqual(states[self.fixture.asset_id]["analysis_status"], "current")
        self.assertEqual(
            states[self.fixture.asset_id]["canonical_image_observation"][
                "analysis_binding"
            ],
            self.fixture.analysis_binding,
        )
        for asset_ids in (
            [self.fixture.asset_id, self.fixture.asset_id],
            ["legacy-image-id"],
            [],
        ):
            with self.subTest(asset_ids=asset_ids), self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "canonical_image_consumer_asset_set_invalid",
            ):
                self.fixture.repository.canonical_image_consumer_states(asset_ids)


class RetrievalQueryCanonicalObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="memolens-retrieval-cutover-"
        )
        self.library = Path(self.temporary_directory.name) / "library"
        self.library.mkdir()
        self.asset_id = "asset_" + "1" * 24
        self.binding = {
            "analysis_run_id": "arun_" + "2" * 32,
            "revision": 3,
            "content_sha256": "3" * 64,
        }
        self.projection = {
            "status": "current",
            "generation_id": "generation_active",
            "processing_generation_id": "generation_processing",
            "receipt_sha256": "4" * 64,
            "row_sha256": "5" * 64,
            "reason_code": None,
        }
        self.observation = {
            "object": "memolens.canonical_image_observation",
            "schema_version": "1",
            "status": "current",
            "authority": "canonical_image_analysis",
            "provenance_status": "verified_current",
            "asset_id": self.asset_id,
            "analysis_binding": self.binding,
            "source_binding_sha256": "6" * 64,
            "projection": self.projection,
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
        self.result = CanonicalRetrievalResponse(
            id="retrieval-cutover",
            query_text="target",
            current_datetime="2026-08-29T00:00:00+00:00",
            parsed_query=None,
            data=[
                RetrievedImageSummary(
                    id=self.asset_id,
                    filename="target.jpg",
                    relative_path="target.jpg",
                    taken_at=None,
                    place_name=None,
                    country=None,
                    description="target",
                    tags=["target"],
                    score=1.0,
                    matched_terms=["target"],
                )
            ],
            status="completed",
            canonical_projection_bindings={
                self.asset_id: {
                    "generation_id": self.projection["generation_id"],
                    "analysis_binding": self.binding,
                    "row_sha256": self.projection["row_sha256"],
                }
            },
        )
        self.app = Flask(__name__)
        self.app.register_blueprint(api_blueprint)
        self.settings = SimpleNamespace(image_library_dir=self.library)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _request(
        self,
        *,
        observation: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
    ):
        selected_observation = observation or self.observation
        retrieval_service = SimpleNamespace(run=lambda _request: self.result)
        repository = SimpleNamespace(
            canonical_image_consumer_states=lambda _asset_ids: {
                self.asset_id: {
                    "analysis_status": "current",
                    "canonical_image_observation": selected_observation,
                }
            }
        )

        def extension(name: str):
            return {
                "retrieval_service": retrieval_service,
                "retrieval_copywriter": object(),
            }[name]

        with (
            patch(
                "backend.src.api.routes._runtime_settings",
                return_value=self.settings,
            ),
            patch(
                "backend.src.api.routes._runtime_extension",
                side_effect=extension,
            ),
            patch(
                "backend.src.api.routes._media_repository",
                return_value=repository,
            ),
            patch(
                "backend.src.api.routes._legacy_provider_egress_authorized",
                return_value=True,
            ),
            self.app.test_client() as client,
        ):
            return client.post(
                "/v1/retrieval/query",
                json=payload or {"text": "target", "include_copy": False},
            )

    def test_query_returns_required_exact_canonical_fields(self) -> None:
        response = self._request()

        self.assertEqual(response.status_code, 200, response.json)
        image = response.json["data"][0]
        self.assertEqual(image["asset_id"], self.asset_id)
        self.assertEqual(image["analysis_status"], "current")
        self.assertEqual(image["analysis_binding"], self.binding)
        self.assertEqual(image["projection"], self.projection)
        self.assertEqual(image["canonical_image_observation"], self.observation)

    def test_query_fails_closed_when_generation_changes_after_ranking(self) -> None:
        stale = deepcopy(self.observation)
        stale["projection"] = {
            **self.projection,
            "generation_id": "generation_successor",
        }

        response = self._request(observation=stale)

        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(response.json["code"], "canonical_image_projection_changed")
        self.assertNotIn("data", response.json)

    def test_query_rejects_more_than_one_closed_observation_batch(self) -> None:
        response = self._request(
            payload={"text": "target", "top_k": 513, "include_copy": False}
        )

        self.assertEqual(response.status_code, 400, response.json)
        self.assertEqual(response.json["message"], "`top_k` must be at most 512.")


if __name__ == "__main__":
    unittest.main()
