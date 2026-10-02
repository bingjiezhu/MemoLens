from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sys
import unittest
from typing import Iterator

from backend.src.media.director import CreativeBriefError, CreativeDirector
from backend.src.media.inbox import MediaInboxService
from backend.src.media.retrieval import MixedRetrievalService
from core.db import ImageIndexRepository
from core.image_projection_renderer import IMAGE_INDEX_SQL_COLUMNS
from core.media_db import canonical_json
from core.photo_atlas import PhotoAtlasService
from tests import test_image_projection_materialization as materialization_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = (
    PROJECT_ROOT / ".agents" / "plugins" / "plugins" / "memolens" / "scripts"
)
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

from memolens_atlas_presenter import present_memories  # noqa: E402
from memolens_creator_store import CreatorMemoryReader  # noqa: E402
from memolens_sqlite import ReadOnlyDatabase  # noqa: E402


class CanonicalImageConsumerMatrixTests(unittest.TestCase):
    @contextmanager
    def _fixture(
        self,
    ) -> Iterator[
        tuple[
            materialization_fixture.ImageProjectionMaterializationTests,
            object,
        ]
    ]:
        materialization = (
            materialization_fixture.ImageProjectionMaterializationTests(
                methodName="runTest"
            )
        )
        materialization.setUp()
        try:
            yield materialization, materialization.fixture
        finally:
            materialization.tearDown()

    @staticmethod
    def _request_sha256(payload: dict[str, object]) -> str:
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _plugin_inbox(fixture: object) -> dict[str, object]:
        return CreatorMemoryReader(
            ReadOnlyDatabase(fixture.db_path)  # type: ignore[attr-defined]
        ).inbox_list(
            state="all",
            kinds=["image"],
            limit=24,
            cursor=None,
        )

    def _atlas_surfaces(
        self,
        fixture: object,
    ) -> tuple[
        PhotoAtlasService,
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        service = PhotoAtlasService(
            ImageIndexRepository(fixture.db_path),  # type: ignore[attr-defined]
            fixture.repository,  # type: ignore[attr-defined]
        )
        rebuilt = service.rebuild()
        workbench = service.workbench()
        plugin_memories = present_memories(
            workbench,
            library_dir=None,
            query=None,
            limit=8,
            local_api={"enabled": True},
        )
        return service, rebuilt, workbench, plugin_memories

    @staticmethod
    def _first_memory_observation(
        payload: dict[str, object],
    ) -> tuple[str, dict[str, object]]:
        memories = payload["memories"]
        assert isinstance(memories, list) and memories
        memory = memories[0]
        assert isinstance(memory, dict)
        representatives = memory["representative_assets"]
        assert isinstance(representatives, list) and representatives
        asset = representatives[0]
        assert isinstance(asset, dict)
        observation = asset["canonical_image_observation"]
        assert isinstance(observation, dict)
        return str(asset["id"]), observation

    @staticmethod
    def _is_current_adoption(item: object, *, asset_id: str) -> bool:
        if not isinstance(item, dict):
            return False
        observation = item.get("canonical_image_observation")
        item_asset_id = item.get("id", item.get("asset_id"))
        return (
            item_asset_id == asset_id
            and isinstance(observation, dict)
            and observation.get("asset_id") == asset_id
            and observation.get("status") == "current"
            and observation.get("provenance_status") == "verified_current"
        )

    def _adoption_matrix(
        self,
        fixture: object,
        *,
        query: str,
        target_asset_id: str,
    ) -> dict[str, int]:
        repository = fixture.repository  # type: ignore[attr-defined]
        search = MixedRetrievalService(repository).search(
            {"query": query, "types": ["image"]}
        )
        backend_inbox = MediaInboxService(repository).list_assets(
            kinds="image"
        ).items
        plugin_inbox = self._plugin_inbox(fixture)["assets"]
        assert isinstance(plugin_inbox, list)
        _service, _rebuilt, workbench, plugin_memories = self._atlas_surfaces(
            fixture
        )
        overview = workbench["overview"]
        assert isinstance(overview, dict)
        atlas_assets = overview["assets"]
        assert isinstance(atlas_assets, list)

        def memory_assets(payload: dict[str, object]) -> list[object]:
            assets: list[object] = []
            memories = payload.get("memories")
            if not isinstance(memories, list):
                return assets
            for memory in memories:
                if not isinstance(memory, dict):
                    continue
                representatives = memory.get("representative_assets")
                if isinstance(representatives, list):
                    assets.extend(representatives)
            return assets

        surfaces = {
            "search": search["results"],
            "backend_inbox": backend_inbox,
            "creator_memories": memory_assets(workbench),
            "codex_plugin_inbox": plugin_inbox,
            "codex_plugin_memory": memory_assets(plugin_memories),
            "photo_atlas": atlas_assets,
        }
        return {
            name: sum(
                self._is_current_adoption(item, asset_id=target_asset_id)
                for item in items
            )
            for name, items in surfaces.items()
        }

    def _assert_zero_create_writes(self, fixture: object) -> None:
        with fixture.repository.transaction() as connection:  # type: ignore[attr-defined]
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM creative_projects"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM idempotency_records"
                ).fetchone()[0],
                0,
            )

    def _assert_create_rejects_observation(
        self,
        fixture: object,
        *,
        observation: dict[str, object],
        key: str,
    ) -> None:
        repository = fixture.repository  # type: ignore[attr-defined]
        payload = {
            "goal": "guard",
            "duration_ms": 3_000,
            "candidate_refs": [fixture.asset_id],  # type: ignore[attr-defined]
            "candidate_observations": [observation],
        }
        with self.assertRaises(CreativeBriefError) as raised:
            CreativeDirector(
                repository,
                MixedRetrievalService(repository),
            ).create_brief_idempotent(
                payload,
                idempotency_scope="test:image-consumer-matrix",
                idempotency_key=key,
                request_sha256=self._request_sha256(payload),
            )
        self.assertEqual(raised.exception.code, "candidate_observation_conflict")
        self.assertEqual(raised.exception.status, 409)
        self._assert_zero_create_writes(fixture)

    def test_one_current_publication_has_one_exact_observation_everywhere(
        self,
    ) -> None:
        with self._fixture() as (materialization, fixture):
            materialization._project()
            repository = fixture.repository
            retrieval = MixedRetrievalService(repository)
            search = retrieval.search({"query": "guard", "types": ["image"]})
            search_asset = search["results"][0]
            expected = search_asset["canonical_image_observation"]
            self.assertIsInstance(expected, dict)

            backend_inbox = MediaInboxService(repository).list_assets(
                kinds="image"
            ).items[0]
            plugin_inbox = self._plugin_inbox(fixture)["assets"][0]
            atlas, rebuilt, workbench, plugin_memories = self._atlas_surfaces(
                fixture
            )
            atlas_asset = atlas.overview()["assets"][0]
            creator_memory_id, creator_memory_observation = (
                self._first_memory_observation(workbench)
            )
            plugin_memory_id, plugin_memory_observation = (
                self._first_memory_observation(plugin_memories)
            )

            payload = {
                "goal": "guard",
                "duration_ms": 3_000,
                "candidate_refs": [search_asset["id"]],
                "candidate_observations": [expected],
            }
            created = CreativeDirector(
                repository,
                retrieval,
            ).create_brief_idempotent(
                payload,
                idempotency_scope="test:image-consumer-matrix",
                idempotency_key="current-exact-observation",
                request_sha256=self._request_sha256(payload),
            )
            brief = created.response["project"]["brief"]
            create_asset = brief["candidate_refs"][0]

            observations = {
                "search": expected,
                "backend_inbox": backend_inbox["canonical_image_observation"],
                "creator_memories": creator_memory_observation,
                "create": brief["candidate_observations"][0],
                "codex_plugin_inbox": plugin_inbox[
                    "canonical_image_observation"
                ],
                "codex_plugin_memory": plugin_memory_observation,
                "photo_atlas": atlas_asset["canonical_image_observation"],
            }
            ids = {
                "search": search_asset["id"],
                "backend_inbox": backend_inbox["id"],
                "creator_memories": creator_memory_id,
                "create": create_asset["id"],
                "codex_plugin_inbox": plugin_inbox["asset_id"],
                "codex_plugin_memory": plugin_memory_id,
                "photo_atlas": atlas_asset["id"],
            }
            self.assertEqual(
                ids,
                {name: fixture.asset_id for name in ids},
            )
            expected_json = canonical_json(expected)
            self.assertEqual(
                {name: canonical_json(value) for name, value in observations.items()},
                {name: expected_json for name in observations},
            )
            self.assertEqual(
                {
                    name: value["analysis_binding"]
                    for name, value in observations.items()
                },
                {name: fixture.analysis_binding for name in observations},
            )
            self.assertEqual(rebuilt["verified_image_count"], 1)
            self.assertEqual(rebuilt["parity_gap_count"], 0)

    def test_revision_change_rejects_create_without_coupling_review_cas(
        self,
    ) -> None:
        with self._fixture() as (materialization, fixture):
            materialization._project()
            old_observation = MixedRetrievalService(fixture.repository).search(
                {"query": "guard", "types": ["image"]}
            )["results"][0]["canonical_image_observation"]
            materialization._publish_revision_two()

            self.assertEqual(
                self._adoption_matrix(
                    fixture,
                    query="guard",
                    target_asset_id=fixture.asset_id,
                ),
                {
                    "search": 0,
                    "backend_inbox": 0,
                    "creator_memories": 0,
                    "codex_plugin_inbox": 0,
                    "codex_plugin_memory": 0,
                    "photo_atlas": 0,
                },
            )
            self._assert_create_rejects_observation(
                fixture,
                observation=old_observation,
                key="stale-n-to-n-plus-one",
            )

            review_payload = {"base_revision": 0, "favorite": True}
            review, replayed = MediaInboxService(
                fixture.repository
            ).update_review(
                fixture.asset_id,
                review_payload,
                idempotency_scope=(
                    f"desktop:PUT:/v1/inbox/assets/{fixture.asset_id}"
                ),
                idempotency_key="review-after-analysis-head-change",
                request_sha256=self._request_sha256(review_payload),
            )
            self.assertFalse(replayed)
            self.assertEqual(review["revision"], 1)
            self.assertTrue(review["favorite"])

    def test_orphan_and_tampered_projections_have_zero_consumer_adoptions(
        self,
    ) -> None:
        with self.subTest(projection_state="orphan"), self._fixture() as (
            materialization,
            fixture,
        ):
            materialization._project()
            columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
            orphan_id = "orphan-image-index-row"
            with fixture.repository.transaction(immediate=True) as connection:
                row = connection.execute(
                    f"SELECT {columns} FROM image_index WHERE id=?",
                    (fixture.asset_id,),
                ).fetchone()
                assert row is not None
                orphan = dict(row)
                orphan.update(
                    {
                        "id": orphan_id,
                        "sha256": "b" * 64,
                        "filename": "orphan.png",
                        "relative_path": "orphan.png",
                    }
                )
                connection.execute(
                    f"INSERT INTO image_index({columns}) "
                    f"VALUES({','.join('?' for _ in IMAGE_INDEX_SQL_COLUMNS)})",
                    tuple(orphan[column] for column in IMAGE_INDEX_SQL_COLUMNS),
                )
            self.assertEqual(
                self._adoption_matrix(
                    fixture,
                    query="orphan",
                    target_asset_id=orphan_id,
                ),
                {
                    "search": 0,
                    "backend_inbox": 0,
                    "creator_memories": 0,
                    "codex_plugin_inbox": 0,
                    "codex_plugin_memory": 0,
                    "photo_atlas": 0,
                },
            )
            orphan_payload = {
                "goal": "orphan",
                "candidate_refs": [orphan_id],
            }
            with self.assertRaises(CreativeBriefError) as raised:
                CreativeDirector(
                    fixture.repository,
                    MixedRetrievalService(fixture.repository),
                ).create_brief_idempotent(
                    orphan_payload,
                    idempotency_scope="test:image-consumer-matrix",
                    idempotency_key="orphan-projection",
                    request_sha256=self._request_sha256(orphan_payload),
                )
            self.assertEqual(raised.exception.code, "candidate_unavailable")
            self._assert_zero_create_writes(fixture)

        with self.subTest(projection_state="tampered"), self._fixture() as (
            materialization,
            fixture,
        ):
            materialization._project()
            old_observation = MixedRetrievalService(fixture.repository).search(
                {"query": "guard", "types": ["image"]}
            )["results"][0]["canonical_image_observation"]
            with fixture.repository.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE image_index SET description=? WHERE id=?",
                    ("tampered consumer matrix", fixture.asset_id),
                )
            self.assertEqual(
                self._adoption_matrix(
                    fixture,
                    query="guard",
                    target_asset_id=fixture.asset_id,
                ),
                {
                    "search": 0,
                    "backend_inbox": 0,
                    "creator_memories": 0,
                    "codex_plugin_inbox": 0,
                    "codex_plugin_memory": 0,
                    "photo_atlas": 0,
                },
            )
            self._assert_create_rejects_observation(
                fixture,
                observation=old_observation,
                key="tampered-projection",
            )


if __name__ == "__main__":
    unittest.main()
