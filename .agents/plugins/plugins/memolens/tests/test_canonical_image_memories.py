from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_atlas_presenter import present_memories  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_image_authority import CanonicalImageObservationReader  # noqa: E402

from core.db import ImageIndexRepository  # noqa: E402
from core.photo_atlas import PhotoAtlasService  # noqa: E402
from tests import test_image_projection_materialization as materialization_fixture  # noqa: E402


def _observation(asset_id: str = "asset_test") -> dict[str, object]:
    provenance = {
        "producer_id": "memolens.image-worker",
        "producer_version": "1",
        "model_id": None,
        "model_version": None,
        "rule_id": "test-rule",
        "rule_version": "1",
    }
    stages = {
        name: {
            "status": "disabled",
            "provenance": dict(provenance),
            "output": None,
            "reason_code": "test_disabled",
        }
        for name in ("metadata", "geocode", "vision", "embedding", "quality")
    }
    return {
        "object": "memolens.canonical_image_observation",
        "schema_version": "1",
        "status": "current",
        "authority": "canonical_image_analysis",
        "provenance_status": "verified_current",
        "asset_id": asset_id,
        "analysis_binding": {
            "analysis_run_id": f"arun_{'1' * 32}",
            "revision": 7,
            "content_sha256": "2" * 64,
        },
        "source_binding_sha256": "5" * 64,
        "projection": {
            "status": "current",
            "generation_id": "image_projection_generation_active",
            "processing_generation_id": "image_projection_generation_processing",
            "receipt_sha256": "3" * 64,
            "row_sha256": "4" * 64,
            "reason_code": None,
        },
        "stages": stages,
        "reason_code": None,
    }


def _asset(asset_id: str = "asset_test") -> dict[str, object]:
    observation = _observation(asset_id)
    return {
        "object": "atlas.asset",
        "id": asset_id,
        "asset_id": asset_id,
        "analysis_status": "current",
        "analysis_binding": observation["analysis_binding"],
        "projection": observation["projection"],
        "canonical_image_observation": observation,
        "filename": "guard.jpg",
        "relative_path": "guard.jpg",
        "description": "guard",
        "tags": ["guard"],
    }


def _payload(asset: dict[str, object]) -> dict[str, object]:
    return {
        "object": "atlas.workbench",
        "memories": [
            {
                "id": "memory_guard",
                "kind": "event",
                "label": "Guard",
                "asset_count": 1,
                "representative_assets": [asset],
            }
        ],
        "suggested_queries": [],
        "storylines": [],
    }


class CanonicalImageMemoriesPresenterTests(unittest.TestCase):
    def _present(self, asset: dict[str, object]) -> dict[str, object]:
        return present_memories(
            _payload(asset),
            library_dir=None,
            query=None,
            limit=8,
            local_api={"enabled": True},
        )

    def test_presenter_preserves_core_observation_without_claiming_new_authority(
        self,
    ) -> None:
        asset = _asset()

        presented = self._present(asset)
        representative = presented["memories"][0]["representative_assets"][0]

        self.assertEqual(
            representative["canonical_image_observation"],
            asset["canonical_image_observation"],
        )
        self.assertEqual(
            representative["canonical_image_observation"]["provenance_status"],
            "verified_current",
        )
        self.assertNotIn("verified_by_plugin", representative)

    def test_missing_forged_pending_and_unavailable_envelopes_fail_closed(
        self,
    ) -> None:
        cases: list[dict[str, object]] = []

        missing = _asset("asset_missing")
        missing.pop("canonical_image_observation")
        cases.append(missing)

        forged = _asset("asset_forged")
        forged_observation = deepcopy(forged["canonical_image_observation"])
        forged_observation["analysis_binding"]["revision"] = 8
        forged["canonical_image_observation"] = forged_observation
        cases.append(forged)

        pending = _asset("asset_pending")
        pending_observation = deepcopy(pending["canonical_image_observation"])
        pending_observation["status"] = "pending"
        pending_observation["provenance_status"] = "verified_pending"
        pending["canonical_image_observation"] = pending_observation
        cases.append(pending)

        unavailable = _asset("asset_unavailable")
        unavailable_observation = deepcopy(
            unavailable["canonical_image_observation"]
        )
        unavailable_observation["status"] = "unavailable"
        unavailable_observation["provenance_status"] = "unavailable"
        unavailable["canonical_image_observation"] = unavailable_observation
        cases.append(unavailable)

        for asset in cases:
            with self.subTest(asset_id=asset["id"]):
                with self.assertRaises(MemoLensError) as failure:
                    self._present(asset)
                self.assertEqual(
                    failure.exception.code,
                    "canonical_image_observation_invalid",
                )

    def test_atlas_and_plugin_reader_emit_the_same_existing_v1_dialect(self) -> None:
        materialization = materialization_fixture.ImageProjectionMaterializationTests(
            methodName="runTest"
        )
        materialization.setUp()
        self.addCleanup(materialization.tearDown)
        materialization._project()
        fixture = materialization.fixture
        service = PhotoAtlasService(
            ImageIndexRepository(fixture.db_path),
            fixture.repository,
        )
        service.rebuild()

        atlas_observation = service.overview()["assets"][0][
            "canonical_image_observation"
        ]
        connection = sqlite3.connect(
            f"file:{fixture.db_path}?mode=ro",
            uri=True,
        )
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        plugin_observation = CanonicalImageObservationReader().read(
            connection,
            raw={"kind": "image", "id": fixture.asset_id},
        )

        self.assertEqual(atlas_observation, plugin_observation)


if __name__ == "__main__":
    unittest.main()
