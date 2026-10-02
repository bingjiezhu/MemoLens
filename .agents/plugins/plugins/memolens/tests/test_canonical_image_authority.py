from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
for search_path in (REPOSITORY_ROOT, SCRIPTS):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

from core.db import ImageIndexRepository  # noqa: E402
from core.image_analysis_contract import (  # noqa: E402
    ImageAnalysisContractError,
    canonical_json,
    canonical_sha256,
    require_valid_projection_manifest,
)
from core.image_projection_renderer import (  # noqa: E402
    IMAGE_INDEX_SQL_COLUMNS,
    materialize_projected_image_index_values,
)
from core.media_db import MediaRepository  # noqa: E402
from memolens_core import MemoLensGateway, TRUST_LOCAL_API_ENV  # noqa: E402
from memolens_image_authority import (  # noqa: E402
    _AuthorityFailure,
    _artifacts,
    _projection_alias_set,
    _projection_receipt_document,
    _publish_document,
    _render_projected_row,
    _result_document,
)
from tests import test_image_projection_schema_guards as schema_guards  # noqa: E402
from tests.test_image_projection_verified_read import (  # noqa: E402
    ImageProjectionVerifiedReadTests as CoreVerifiedReadTests,
)


class CanonicalImageAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def gateway(
        self,
        path: Path | None = None,
        *,
        trust_local_api: bool = False,
    ) -> MemoLensGateway:
        with mock.patch.dict(
            "os.environ",
            {TRUST_LOCAL_API_ENV: "1" if trust_local_api else "0"},
            clear=False,
        ):
            return MemoLensGateway(
                db_path=path or self.fixture.db_path,
                library_dir=(self.fixture.library if path is None else None),
                base_url="http://should-never-resolve.invalid:1",
                timeout=0.1,
            )

    def _activate_clean_generation(self, generation_id: str) -> None:
        with self.fixture.repository.transaction(immediate=True) as connection:
            active_rows = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 AND id!=? ORDER BY id""",
                (generation_id,),
            ).fetchall()
            self.assertLessEqual(len(active_rows), 1)
            previous_generation_id = (
                str(active_rows[0]["id"]) if active_rows else None
            )
            completed = connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=?
                    WHERE id=? AND status='building' AND is_active=0""",
                (schema_guards.NOW, generation_id),
            )
            self.assertEqual(completed.rowcount, 1)
            read_manifest = connection.execute(
                """INSERT INTO image_projection_read_manifests(
                       generation_id,previous_generation_id,database_uuid,
                       canonical_high_water_position,compiler_id,
                       projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,
                       mismatched_count,blocked_count,alias_set_sha256,
                       row_set_sha256,manifest_json,manifest_sha256,created_at)
                   SELECT manifest.generation_id,?,manifest.database_uuid,
                          manifest.canonical_high_water_position,
                          manifest.compiler_id,manifest.projector_version,
                          manifest.eligible_count,manifest.projected_count,
                          manifest.alias_count,manifest.missing_count,
                          manifest.unexpected_count,manifest.mismatched_count,
                          manifest.blocked_count,manifest.alias_set_sha256,
                          manifest.row_set_sha256,manifest.manifest_json,
                          manifest.manifest_sha256,?
                     FROM image_projection_manifests manifest
                    WHERE manifest.generation_id=?""",
                (previous_generation_id, schema_guards.NOW, generation_id),
            )
            self.assertEqual(read_manifest.rowcount, 1)
            if previous_generation_id is not None:
                deactivated = connection.execute(
                    """UPDATE image_projection_generations SET is_active=0
                        WHERE id=? AND status='complete' AND is_active=1""",
                    (previous_generation_id,),
                )
                self.assertEqual(deactivated.rowcount, 1)
            activated = connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete' AND is_active=0""",
                (generation_id,),
            )
            self.assertEqual(activated.rowcount, 1)

    def _install_complete_projection(self) -> str:
        generation_id = self.fixture._create_generation(with_row=True)
        receipt = self.fixture._projection_receipt(generation_id)
        self.fixture._insert_projection_receipt(generation_id, receipt)
        self.fixture._insert_manifest(generation_id, self.fixture._manifest())
        values = materialize_projected_image_index_values(
            self.fixture.projected_row_document,
            created_at=schema_guards.NOW,
            updated_at=schema_guards.NOW,
        )
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _column in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                values,
            )
        self._activate_clean_generation(generation_id)
        return generation_id

    def _publish_second_asset(self) -> tuple[str, str]:
        return CoreVerifiedReadTests._publish_second_asset(self)

    def _publish_second_revision(self) -> tuple[str, dict[str, object]]:
        return CoreVerifiedReadTests._publish_second_revision(self)

    def _tamper_projection_row_sha256(
        self,
        *,
        generation_id: str,
        asset_id: str,
    ) -> None:
        CoreVerifiedReadTests._tamper_projection_row_sha256(
            self,
            generation_id=generation_id,
            asset_id=asset_id,
        )

    @staticmethod
    def _resign(document: dict[str, object], digest_field: str) -> dict[str, object]:
        unsigned = deepcopy(document)
        unsigned.pop(digest_field, None)
        document[digest_field] = canonical_sha256(unsigned)
        return document

    @staticmethod
    def _temporarily_drop_trigger(
        connection: sqlite3.Connection,
        name: str,
    ) -> str:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (name,),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        sql = str(row[0])
        connection.execute(f"DROP TRIGGER {name}")
        return sql

    def _assert_projection_corrupt(
        self,
        reason_code: str = "canonical_image_projection_corrupt",
    ) -> None:
        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)
        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(
            observation["reason_code"],
            reason_code,
        )
        self.assertIsNone(observation["stages"])
        self.assertEqual(self.gateway().mixed_search("guard")["results"], [])
        self.assertEqual(self.gateway().search("guard")["results"], [])

    @staticmethod
    def _observation(page: dict[str, object]) -> dict[str, object]:
        projected = page["page"]
        assert isinstance(projected, dict)
        content = projected["content"]
        assert isinstance(content, dict)
        observation = content["observation"]
        assert isinstance(observation, dict)
        return observation

    @staticmethod
    def _gap_codes(page: dict[str, object]) -> set[str]:
        gaps = page.get("gaps")
        assert isinstance(gaps, list)
        return {
            str(item.get("code"))
            for item in gaps
            if isinstance(item, dict)
        }

    def test_active_clean_exact_projection_exposes_typed_canonical_stages(self) -> None:
        generation_id = self._install_complete_projection()

        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)

        self.assertEqual(observation["status"], "current")
        self.assertEqual(observation["authority"], "canonical_image_analysis")
        self.assertEqual(observation["provenance_status"], "verified_current")
        self.assertEqual(observation["asset_id"], self.fixture.asset_id)
        self.assertEqual(
            observation["analysis_binding"], self.fixture.analysis_binding
        )
        projection = observation["projection"]
        assert isinstance(projection, dict)
        self.assertEqual(projection["generation_id"], generation_id)
        self.assertEqual(projection["status"], "current")
        stages = observation["stages"]
        assert isinstance(stages, dict)
        self.assertEqual(stages["metadata"]["status"], "succeeded")
        self.assertEqual(stages["embedding"]["status"], "succeeded")
        self.assertEqual(stages["vision"]["status"], "disabled")
        self.assertNotIn("image_observation_unavailable", self._gap_codes(page))
        serialized = json.dumps(page, ensure_ascii=False)
        self.assertNotIn(str(self.fixture.library), serialized)
        self.assertNotIn("relative_path", serialized)
        self.assertNotIn("incomplete_legacy", serialized)

        mixed = self.gateway().mixed_search("guard")
        image_match = next(
            item
            for item in mixed["results"]
            if item["result_type"] == "image"
        )
        search_observation = image_match["canonical_image_observation"]
        self.assertTrue(self.gateway().status()["capabilities"]["search"])
        self.assertEqual(search_observation["status"], "current")
        self.assertEqual(
            search_observation["provenance_status"],
            "verified_current",
        )
        self.assertEqual(
            search_observation["analysis_binding"],
            self.fixture.analysis_binding,
        )
        self.assertNotIn("relative_path", image_match)
        self.assertNotIn("absolute_path", image_match)
        photos_only = self.gateway().search("guard")
        self.assertEqual(photos_only["result_count"], 1)
        local_match = photos_only["results"][0]
        self.assertEqual(local_match["path_status"], "ok")
        self.assertEqual(
            local_match["absolute_path"],
            str(self.fixture.library / "guard.png"),
        )
        self.assertEqual(
            local_match["canonical_image_observation"]["analysis_binding"],
            self.fixture.analysis_binding,
        )
        opt_in_gateway = self.gateway(trust_local_api=True)
        with mock.patch.object(
            opt_in_gateway,
            "_request_json",
            side_effect=AssertionError("photo search reached local API"),
        ):
            opt_in_search = opt_in_gateway.search("guard")
        self.assertFalse(opt_in_search["local_api_used"])
        self.assertEqual(opt_in_search["source"], "sqlite_read_only")
        self.assertEqual(
            opt_in_search["results"][0]["canonical_image_observation"][
                "analysis_binding"
            ],
            self.fixture.analysis_binding,
        )
        searched = self.gateway().wiki_search("guard")
        self.assertEqual(
            [item["asset_id"] for item in searched["results"]],
            [self.fixture.asset_id],
        )
        search_json = json.dumps([mixed, searched], ensure_ascii=False)
        self.assertNotIn(str(self.fixture.library), search_json)
        self.assertNotIn("provider_payload", search_json)

    def test_active_generation_requires_v17_read_manifest(self) -> None:
        generation_id = self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            trigger_sql = self._temporarily_drop_trigger(
                connection,
                "trg_image_projection_read_manifests_no_delete",
            )
            deleted = connection.execute(
                "DELETE FROM image_projection_read_manifests WHERE generation_id=?",
                (generation_id,),
            )
            connection.execute(trigger_sql)
        self.assertEqual(deleted.rowcount, 1)

        self._assert_projection_corrupt()

    def test_active_read_manifest_must_be_clean(self) -> None:
        generation_id = self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            trigger_sql = self._temporarily_drop_trigger(
                connection,
                "trg_image_projection_read_manifests_no_update",
            )
            changed = connection.execute(
                """UPDATE image_projection_read_manifests
                      SET unexpected_count=1 WHERE generation_id=?""",
                (generation_id,),
            )
            connection.execute(trigger_sql)
        self.assertEqual(changed.rowcount, 1)

        self._assert_projection_corrupt()

    def test_active_read_manifest_must_exactly_equal_canonical_manifest(
        self,
    ) -> None:
        generation_id = self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            trigger_sql = self._temporarily_drop_trigger(
                connection,
                "trg_image_projection_read_manifests_no_update",
            )
            changed = connection.execute(
                """UPDATE image_projection_read_manifests
                      SET manifest_json=manifest_json || ' '
                    WHERE generation_id=?""",
                (generation_id,),
            )
            connection.execute(trigger_sql)
        self.assertEqual(changed.rowcount, 1)

        self._assert_projection_corrupt()

    def test_active_read_manifest_previous_generation_cannot_self_link(
        self,
    ) -> None:
        generation_id = self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            trigger_sql = self._temporarily_drop_trigger(
                connection,
                "trg_image_projection_read_manifests_no_update",
            )
            connection.execute("PRAGMA ignore_check_constraints=ON")
            changed = connection.execute(
                """UPDATE image_projection_read_manifests
                      SET previous_generation_id=generation_id
                    WHERE generation_id=?""",
                (generation_id,),
            )
            connection.execute("PRAGMA ignore_check_constraints=OFF")
            connection.execute(trigger_sql)
        self.assertEqual(changed.rowcount, 1)

        self._assert_projection_corrupt()

    def test_historical_receipt_and_active_generation_match_core_semantics(
        self,
    ) -> None:
        first = self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        second_asset_id, second_job_id = self._publish_second_asset()
        second = self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )

        observations = {
            asset_id: self._observation(
                self.gateway().wiki_open(f"memolens://asset/{asset_id}")
            )
            for asset_id in (self.fixture.asset_id, second_asset_id)
        }
        core = {
            asset_id: self.fixture.repository.get_current_image_analysis(asset_id)
            for asset_id in (self.fixture.asset_id, second_asset_id)
        }

        for asset_id, observation in observations.items():
            current = core[asset_id]
            assert current is not None
            self.assertEqual(observation["status"], "current")
            self.assertEqual(observation["projection"]["status"], "current")
            self.assertEqual(
                observation["projection"]["generation_id"],
                second["generation_id"],
            )
            for field in (
                "generation_id",
                "processing_generation_id",
                "receipt_sha256",
                "row_sha256",
            ):
                self.assertEqual(
                    observation["projection"][field],
                    current["projection"][field],
                )
        self.assertEqual(
            observations[self.fixture.asset_id]["projection"][
                "processing_generation_id"
            ],
            first["generation_id"],
        )
        self.assertEqual(
            observations[second_asset_id]["projection"][
                "processing_generation_id"
            ],
            second["generation_id"],
        )

    def test_historical_processing_row_tamper_fails_closed_in_core_and_plugin(
        self,
    ) -> None:
        first = self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        _second_asset_id, second_job_id = self._publish_second_asset()
        self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        with self.fixture.repository.transaction(immediate=True) as connection:
            building_trigger = self._temporarily_drop_trigger(
                connection,
                "trg_image_projection_rows_building_update",
            )
            document_trigger = self._temporarily_drop_trigger(
                connection,
                "trg_image_projection_rows_document_validate_update",
            )
            connection.execute(
                """UPDATE image_projection_rows SET row_sha256=?
                    WHERE generation_id=? AND asset_id=?""",
                ("f" * 64, first["generation_id"], self.fixture.asset_id),
            )
            connection.execute(building_trigger)
            connection.execute(document_trigger)

        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )
        observation = self._observation(
            self.gateway().wiki_open(
                f"memolens://asset/{self.fixture.asset_id}"
            )
        )
        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(
            observation["reason_code"],
            "canonical_image_projection_corrupt",
        )
        self.assertIsNone(observation["stages"])
        for result in (
            self.gateway().mixed_search("guard")["results"],
            self.gateway().search("guard")["results"],
        ):
            self.assertNotIn(
                self.fixture.asset_id,
                {item.get("asset_id") for item in result},
            )

    def test_unrelated_future_change_keeps_first_asset_current_and_second_pending(
        self,
    ) -> None:
        first = self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        second_asset_id, _second_job_id = self._publish_second_asset()

        first_current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        second_current = self.fixture.repository.get_current_image_analysis(
            second_asset_id
        )
        first_observation = self._observation(
            self.gateway().wiki_open(
                f"memolens://asset/{self.fixture.asset_id}"
            )
        )
        second_observation = self._observation(
            self.gateway().wiki_open(f"memolens://asset/{second_asset_id}")
        )

        assert first_current is not None and second_current is not None
        self.assertEqual(first_current["projection"]["status"], "current")
        self.assertEqual(first_observation["status"], "current")
        for field in (
            "status",
            "generation_id",
            "processing_generation_id",
            "receipt_sha256",
            "reason_code",
        ):
            self.assertEqual(
                first_observation["projection"][field],
                first_current["projection"][field],
            )
        self.assertEqual(
            first_observation["projection"]["generation_id"],
            first["generation_id"],
        )
        self.assertEqual(second_current["projection"]["status"], "pending")
        self.assertEqual(second_observation["status"], "pending")
        self.assertIsNone(second_observation["projection"]["generation_id"])
        self.assertIsNone(
            second_observation["projection"]["processing_generation_id"]
        )

    def test_same_asset_unprojected_revision_only_stales_that_asset_in_both_readers(
        self,
    ) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        second_asset_id, second_job_id = self._publish_second_asset()
        second = self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        _revision_job_id, revision_binding = self._publish_second_revision()

        first_current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        second_current = self.fixture.repository.get_current_image_analysis(
            second_asset_id
        )
        first_observation = self._observation(
            self.gateway().wiki_open(
                f"memolens://asset/{self.fixture.asset_id}"
            )
        )
        second_observation = self._observation(
            self.gateway().wiki_open(f"memolens://asset/{second_asset_id}")
        )

        assert first_current is not None and second_current is not None
        self.assertEqual(first_current["analysis_binding"], revision_binding)
        self.assertEqual(first_current["projection"]["status"], "pending")
        self.assertEqual(first_observation["status"], "pending")
        self.assertIsNone(first_observation["stages"])
        self.assertEqual(second_current["projection"]["status"], "current")
        self.assertEqual(second_observation["status"], "current")
        self.assertEqual(
            second_observation["projection"]["generation_id"],
            second["generation_id"],
        )
        for results in (
            self.gateway().mixed_search("guard")["results"],
            self.gateway().search("guard")["results"],
        ):
            admitted = {item.get("asset_id") for item in results}
            self.assertNotIn(self.fixture.asset_id, admitted)
            self.assertIn(second_asset_id, admitted)

    def test_active_row_tamper_fails_closed_with_historical_receipt_in_both_readers(
        self,
    ) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        _second_asset_id, second_job_id = self._publish_second_asset()
        second = self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        self._tamper_projection_row_sha256(
            generation_id=str(second["generation_id"]),
            asset_id=self.fixture.asset_id,
        )

        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        observation = self._observation(
            self.gateway().wiki_open(
                f"memolens://asset/{self.fixture.asset_id}"
            )
        )

        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )
        self.assertEqual(
            observation["reason_code"],
            "canonical_image_projection_corrupt",
        )
        self.assertIsNone(observation["stages"])

    def test_published_head_without_active_projection_is_explicit_pending(self) -> None:
        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)

        self.assertEqual(observation["status"], "pending")
        self.assertEqual(observation["asset_id"], self.fixture.asset_id)
        self.assertEqual(
            observation["analysis_binding"], self.fixture.analysis_binding
        )
        self.assertIsNone(observation["stages"])
        self.assertEqual(observation["projection"]["status"], "pending")
        self.assertIn(
            "canonical_image_projection_pending",
            self._gap_codes(page),
        )
        self.assertNotIn("incomplete_legacy", json.dumps(page))
        photo_search = self.gateway().search("guard")
        mixed_search = self.gateway().mixed_search("guard")
        self.assertEqual(photo_search["results"], [])
        self.assertEqual(mixed_search["results"], [])
        self.assertNotIn(
            str(self.fixture.library),
            json.dumps([photo_search, mixed_search]),
        )

    def test_unmanaged_legacy_image_index_text_never_becomes_observation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-plugin-image-legacy-only-"
        ) as temporary:
            root = Path(temporary).resolve()
            library = root / "library"
            library.mkdir()
            db_path = root / "state" / "media.db"
            db_path.parent.mkdir()
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            repository.ensure_schema(library)
            image_path = library / "legacy-only.jpg"
            image_path.write_bytes(b"legacy-only-image")
            payload_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
            observed = image_path.stat()
            asset = repository.upsert_asset_source(
                root_id=str(repository.library_roots()[0]["id"]),
                relative_path=image_path.name,
                filename=image_path.name,
                kind="image",
                sha256=payload_sha256,
                mime_type="image/jpeg",
                file_size=observed.st_size,
                mtime_ns=observed.st_mtime_ns,
                source_file_id=str(observed.st_ino),
            )
            repository.update_image_probe(
                str(asset["id"]), width=10, height=10
            )
            repository.close()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """INSERT INTO image_index(
                           id,sha256,filename,relative_path,mime_type,file_size,
                           width,height,description,tags_json,combined_text,
                           embedding_backend,embedding,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "legacy_unmanaged",
                        payload_sha256,
                        image_path.name,
                        image_path.name,
                        "image/jpeg",
                        observed.st_size,
                        10,
                        10,
                        "LEGACY_TEXT_MUST_NOT_SURFACE",
                        '["legacy"]',
                        "LEGACY_COMBINED_MUST_NOT_SURFACE",
                        "legacy",
                        b"legacy-vector",
                        schema_guards.NOW,
                        schema_guards.NOW,
                    ),
                )
                connection.commit()

            page = self.gateway(db_path).wiki_open(
                f"memolens://asset/{asset['id']}"
            )
            observation = self._observation(page)
            serialized = json.dumps(page, ensure_ascii=False)
            self.assertEqual(observation["status"], "unavailable")
            self.assertEqual(
                observation["reason_code"],
                "canonical_image_head_unavailable",
            )
            self.assertIn(
                "canonical_image_observation_unavailable",
                self._gap_codes(page),
            )
            self.assertNotIn("LEGACY_TEXT_MUST_NOT_SURFACE", serialized)
            self.assertNotIn("LEGACY_COMBINED_MUST_NOT_SURFACE", serialized)
            self.assertNotIn("incomplete_legacy", serialized)

            mixed = self.gateway(db_path).mixed_search(
                "LEGACY_TEXT_MUST_NOT_SURFACE"
            )
            self.assertEqual(mixed["results"], [])
            self.assertNotIn(
                "LEGACY_TEXT_MUST_NOT_SURFACE",
                json.dumps(mixed["results"], ensure_ascii=False),
            )
            photos_only = self.gateway(db_path).search(
                "LEGACY_TEXT_MUST_NOT_SURFACE"
            )
            self.assertEqual(photos_only["results"], [])
            self.assertNotIn(str(library), json.dumps(photos_only))

    def test_physical_projection_tamper_fails_closed_without_stage_text(self) -> None:
        self._install_complete_projection()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            connection.execute(
                "UPDATE image_index SET description=? WHERE id=?",
                ("FORGED_PHYSICAL_DESCRIPTION", self.fixture.asset_id),
            )
            connection.commit()

        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)

        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(
            observation["reason_code"],
            "canonical_image_projection_corrupt",
        )
        self.assertIsNone(observation["stages"])
        self.assertNotIn("FORGED_PHYSICAL_DESCRIPTION", json.dumps(page))
        self.assertIn(
            "canonical_image_observation_unavailable",
            self._gap_codes(page),
        )
        mixed = self.gateway().mixed_search("FORGED_PHYSICAL_DESCRIPTION")
        self.assertEqual(mixed["results"], [])
        self.assertNotIn(
            "FORGED_PHYSICAL_DESCRIPTION",
            json.dumps(mixed["results"], ensure_ascii=False),
        )
        photos_only = self.gateway().search("FORGED_PHYSICAL_DESCRIPTION")
        self.assertEqual(photos_only["results"], [])
        self.assertNotIn(
            str(self.fixture.library),
            json.dumps(photos_only, ensure_ascii=False),
        )

    def test_unmanaged_physical_row_invalidates_active_full_census(self) -> None:
        self._install_complete_projection()
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _column in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            canonical = connection.execute(
                f"SELECT {columns} FROM image_index WHERE id=?",
                (self.fixture.asset_id,),
            ).fetchone()
            assert canonical is not None
            unmanaged = {
                column: canonical[column] for column in IMAGE_INDEX_SQL_COLUMNS
            }
            unmanaged.update(
                {
                    "id": "unmanaged_after_v17_cutover",
                    "sha256": hashlib.sha256(b"unmanaged-after-v17").hexdigest(),
                    "filename": "unmanaged-after-v17.png",
                    "relative_path": "unmanaged-after-v17.png",
                }
            )
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                tuple(unmanaged[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )

        self._assert_projection_corrupt()

    def test_other_asset_physical_tamper_invalidates_active_full_census(
        self,
    ) -> None:
        self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        second_asset_id, second_job_id = self._publish_second_asset()
        self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        with self.fixture.repository.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE image_index SET description=? WHERE id=?",
                ("TAMPERED_OTHER_PHYSICAL_ROW", second_asset_id),
            )
        self.assertEqual(changed.rowcount, 1)

        self._assert_projection_corrupt()

    def test_resigned_manifest_entry_not_bound_to_generation_fails_closed(self) -> None:
        generation_id = self._install_complete_projection()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            row = connection.execute(
                """SELECT manifest_json FROM image_projection_manifests
                    WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
            assert row is not None
            forged = json.loads(str(row[0]))
            entries = forged["entries"]
            assert isinstance(entries, list)
            assert len(entries) == 1
            entry = entries[0]
            assert isinstance(entry, dict)
            forged_asset_id = f"asset_{'f' * 24}"
            self.assertNotEqual(forged_asset_id, self.fixture.asset_id)
            entry["asset_id"] = forged_asset_id
            unsigned = deepcopy(forged)
            unsigned.pop("manifest_sha256")
            forged["manifest_sha256"] = canonical_sha256(unsigned)

            trigger_row = connection.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='trigger' AND name='trg_image_manifests_no_update'"""
            ).fetchone()
            assert trigger_row is not None
            trigger_sql = trigger_row[0]
            assert isinstance(trigger_sql, str)
            connection.execute("DROP TRIGGER trg_image_manifests_no_update")
            connection.execute(
                """UPDATE image_projection_manifests
                      SET manifest_json=?,manifest_sha256=?
                    WHERE generation_id=?""",
                (
                    canonical_json(forged),
                    forged["manifest_sha256"],
                    generation_id,
                ),
            )
            connection.execute(trigger_sql)
            connection.commit()

        with self.assertRaises(ImageAnalysisContractError) as contract_failure:
            require_valid_projection_manifest(forged)
        self.assertIn(
            "row_set_digest_mismatch",
            {item["code"] for item in contract_failure.exception.errors},
        )

        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)

        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(
            observation["reason_code"],
            "canonical_image_projection_corrupt",
        )
        self.assertIsNone(observation["stages"])
        self.assertIn(
            "canonical_image_observation_unavailable",
            self._gap_codes(page),
        )
        self.assertNotIn(forged_asset_id, json.dumps(page, ensure_ascii=False))
        mixed = self.gateway().mixed_search("guard")
        photos_only = self.gateway().search("guard")
        self.assertEqual(mixed["results"], [])
        self.assertEqual(photos_only["results"], [])
        self.assertNotIn(
            forged_asset_id,
            json.dumps([mixed, photos_only], ensure_ascii=False),
        )

    def test_inactive_stale_generation_is_pending_not_current(self) -> None:
        generation_id = self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_projection_generations SET is_active=0 WHERE id=?",
                (generation_id,),
            )

        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)

        self.assertEqual(observation["status"], "pending")
        self.assertEqual(
            observation["reason_code"],
            "projection_generation_not_active",
        )
        self.assertIsNone(observation["stages"])
        self.assertIn(
            "canonical_image_projection_pending",
            self._gap_codes(page),
        )
        mixed_search = self.gateway().mixed_search("guard")
        photo_search = self.gateway().search("guard")
        self.assertEqual(mixed_search["results"], [])
        self.assertEqual(photo_search["results"], [])
        self.assertNotIn(
            str(self.fixture.library),
            json.dumps([photo_search, mixed_search]),
        )

    def test_wrong_database_uuid_binding_is_unavailable(self) -> None:
        self._install_complete_projection()
        replacement = "11111111-1111-4111-8111-111111111111"
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE database_meta SET database_uuid=? WHERE singleton=1",
                (replacement,),
            )
            connection.commit()

        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)

        self.assertEqual(observation["status"], "unavailable")
        self.assertIsNone(observation["stages"])
        self.assertNotIn("incomplete_legacy", json.dumps(page))
        mixed_search = self.gateway().mixed_search("guard")
        photo_search = self.gateway().search("guard")
        self.assertEqual(mixed_search["results"], [])
        self.assertEqual(photo_search["results"], [])
        self.assertNotIn(
            str(self.fixture.library),
            json.dumps([photo_search, mixed_search]),
        )

    def test_changed_current_source_binding_is_excluded_from_search(self) -> None:
        self._install_complete_projection()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            connection.execute(
                "UPDATE asset_sources SET observed_mtime_ns=observed_mtime_ns+1 "
                "WHERE id=?",
                (self.fixture.source_id,),
            )
            connection.commit()

        mixed = self.gateway().mixed_search("guard")
        photo_search = self.gateway().search("guard")

        self.assertEqual(mixed["results"], [])
        self.assertEqual(photo_search["results"], [])
        self.assertNotIn(
            str(self.fixture.library),
            json.dumps([photo_search, mixed]),
        )
        page = self.gateway().wiki_open(
            f"memolens://asset/{self.fixture.asset_id}"
        )
        observation = self._observation(page)
        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(
            observation["reason_code"],
            "canonical_image_source_binding_invalid",
        )

    def test_canonical_search_authority_work_is_hard_bounded(self) -> None:
        reader = self.gateway()._store.photos
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """CREATE TABLE candidates(
                       id TEXT,sha256 TEXT,filename TEXT,relative_path TEXT,
                       description TEXT,tags_json TEXT,combined_text TEXT,
                       asset_id TEXT,asset_sha256 TEXT)"""
            )
            sha256 = "a" * 64
            connection.executemany(
                "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    (
                        f"asset_{index:024x}",
                        sha256,
                        f"guard-{index}.png",
                        f"guard-{index}.png",
                        "guard candidate",
                        '["guard"]',
                        "guard candidate",
                        f"asset_{index:024x}",
                        sha256,
                    )
                    for index in range(200)
                ),
            )
            with mock.patch.object(
                reader.image_authority,
                "read",
                return_value={
                    "status": "unavailable",
                    "reason_code": "canonical_image_projection_corrupt",
                },
            ) as verifier:
                admitted, scanned, summary = reader._rank_rows(
                    connection,
                    connection.execute("SELECT * FROM candidates"),
                    query="guard",
                    limit=36,
                    require_canonical_authority=True,
                )

        self.assertEqual(admitted, [])
        self.assertEqual(scanned, 200)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["matched_candidate_count"], 200)
        self.assertEqual(summary["evaluated_candidate_count"], 144)
        self.assertEqual(summary["excluded_count"], 144)
        self.assertTrue(summary["candidate_pool_truncated"])
        self.assertEqual(verifier.call_count, 144)

    def test_active_generation_scope_precedes_ranking_candidate_limit(self) -> None:
        reader = self.gateway()._store.photos
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE database_meta(
                    singleton INTEGER PRIMARY KEY,database_uuid TEXT);
                CREATE TABLE image_index(
                    id TEXT PRIMARY KEY,sha256 TEXT,filename TEXT,
                    relative_path TEXT,description TEXT,tags_json TEXT,
                    combined_text TEXT,aesthetic_score REAL,
                    technical_quality_score REAL);
                CREATE TABLE assets(id TEXT PRIMARY KEY,sha256 TEXT,kind TEXT);
                CREATE TABLE image_analysis_heads(
                    asset_id TEXT PRIMARY KEY,analysis_run_id TEXT,
                    revision INTEGER,content_sha256 TEXT);
                CREATE TABLE image_analysis_results(
                    asset_id TEXT,analysis_run_id TEXT,revision INTEGER,
                    content_sha256 TEXT,source_id TEXT,
                    source_binding_sha256 TEXT);
                CREATE TABLE asset_sources(
                    id TEXT PRIMARY KEY,asset_id TEXT,library_root_id TEXT,
                    availability TEXT);
                CREATE TABLE library_roots(id TEXT PRIMARY KEY,status TEXT);
                CREATE TABLE image_projection_generations(
                    id TEXT PRIMARY KEY,database_uuid TEXT,
                    projection_contract TEXT,canonical_high_water_position INTEGER,
                    compiler_id TEXT,projector_version TEXT,status TEXT,
                    is_active INTEGER);
                CREATE TABLE image_projection_manifests(
                    generation_id TEXT PRIMARY KEY,database_uuid TEXT,
                    canonical_high_water_position INTEGER,compiler_id TEXT,
                    projector_version TEXT,eligible_count INTEGER,
                    projected_count INTEGER,missing_count INTEGER,
                    unexpected_count INTEGER,mismatched_count INTEGER,
                    blocked_count INTEGER,manifest_json TEXT,
                    manifest_sha256 TEXT);
                CREATE TABLE image_projection_read_manifests(
                    generation_id TEXT PRIMARY KEY,database_uuid TEXT,
                    canonical_high_water_position INTEGER,compiler_id TEXT,
                    projector_version TEXT,eligible_count INTEGER,
                    projected_count INTEGER,missing_count INTEGER,
                    unexpected_count INTEGER,mismatched_count INTEGER,
                    blocked_count INTEGER,manifest_json TEXT,
                    manifest_sha256 TEXT);
                CREATE TABLE image_projection_rows(
                    generation_id TEXT,asset_id TEXT,analysis_run_id TEXT,
                    revision INTEGER,content_sha256 TEXT,source_id TEXT,
                    source_binding_sha256 TEXT);
                """
            )
            database_uuid = "11111111-1111-4111-8111-111111111111"
            generation_id = "generation_active"
            active_id = f"asset_{'f' * 24}"
            connection.execute(
                "INSERT INTO database_meta VALUES(1,?)",
                (database_uuid,),
            )
            connection.execute(
                "INSERT INTO library_roots VALUES('root_active','active')"
            )
            connection.execute(
                "INSERT INTO image_projection_generations VALUES(?,?,?,?,?,?,?,?)",
                (
                    generation_id,
                    database_uuid,
                    "legacy-image-index-shadow/v1",
                    201,
                    "compiler-v1",
                    "projector-v1",
                    "complete",
                    1,
                ),
            )
            connection.execute(
                "INSERT INTO image_projection_manifests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_id,
                    database_uuid,
                    201,
                    "compiler-v1",
                    "projector-v1",
                    1,
                    1,
                    0,
                    0,
                    0,
                    0,
                    '{"manifest":"exact"}',
                    "a" * 64,
                ),
            )
            connection.execute(
                """INSERT INTO image_projection_read_manifests
                   SELECT * FROM image_projection_manifests"""
            )

            def insert_candidate(asset_id: str, ordinal: int) -> None:
                digest = hashlib.sha256(asset_id.encode()).hexdigest()
                run_id = f"run_{ordinal}"
                content_sha256 = hashlib.sha256(run_id.encode()).hexdigest()
                source_id = f"source_{ordinal}"
                source_binding = hashlib.sha256(source_id.encode()).hexdigest()
                connection.execute(
                    "INSERT INTO image_index VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        asset_id,
                        digest,
                        f"guard-{ordinal}.png",
                        f"guard-{ordinal}.png",
                        "guard candidate",
                        '["guard"]',
                        "guard candidate",
                        1000.0 if asset_id != active_id else 0.0,
                        1.0,
                    ),
                )
                connection.execute(
                    "INSERT INTO assets VALUES(?,?,'image')",
                    (asset_id, digest),
                )
                connection.execute(
                    "INSERT INTO image_analysis_heads VALUES(?,?,1,?)",
                    (asset_id, run_id, content_sha256),
                )
                connection.execute(
                    "INSERT INTO image_analysis_results VALUES(?,?,1,?,?,?)",
                    (
                        asset_id,
                        run_id,
                        content_sha256,
                        source_id,
                        source_binding,
                    ),
                )
                connection.execute(
                    "INSERT INTO asset_sources VALUES(?,?,?, 'available')",
                    (source_id, asset_id, "root_active"),
                )
                if asset_id == active_id:
                    connection.execute(
                        "INSERT INTO image_projection_rows VALUES(?,?,?,1,?,?,?)",
                        (
                            generation_id,
                            asset_id,
                            run_id,
                            content_sha256,
                            source_id,
                            source_binding,
                        ),
                    )

            for index in range(200):
                insert_candidate(f"asset_{index:024x}", index)
            insert_candidate(active_id, 200)

            columns = reader.database.columns(connection, "image_index")
            select_parts, from_sql, where_sql = reader._canonical_search_projection(
                connection,
                columns,
            )
            cursor = connection.execute(
                f"SELECT {', '.join(select_parts)} {from_sql} {where_sql}"
            )
            with mock.patch.object(
                reader,
                "_canonical_observation",
                return_value={"status": "current"},
            ) as verifier:
                admitted, scanned, summary = reader._rank_rows(
                    connection,
                    cursor,
                    query="guard",
                    limit=1,
                    require_canonical_authority=True,
                )
            connection.execute(
                "UPDATE image_projection_manifests SET mismatched_count=1"
            )
            dirty_cursor = connection.execute(
                f"SELECT {', '.join(select_parts)} {from_sql} {where_sql}"
            )
            dirty_admitted, dirty_scanned, _dirty_summary = reader._rank_rows(
                connection,
                dirty_cursor,
                query="guard",
                limit=1,
                require_canonical_authority=True,
            )

        self.assertEqual(scanned, 1)
        self.assertEqual([candidate[-1]["asset_id"] for candidate in admitted], [active_id])
        self.assertIsNotNone(summary)
        self.assertEqual(verifier.call_count, 1)
        self.assertEqual(dirty_scanned, 0)
        self.assertEqual(dirty_admitted, [])

    def test_resigned_result_rejects_non_image_mime_and_non_float_numbers(self) -> None:
        base = self.fixture._sealed_result()
        mutations = (
            (
                "non-image-mime",
                lambda result: result["stages"]["metadata"]["output"].__setitem__(  # type: ignore[index,union-attr]
                    "mime_type", "video/mp4"
                ),
            ),
            (
                "integer-coordinate",
                lambda result: (
                    result["stages"]["metadata"]["output"].__setitem__(  # type: ignore[index,union-attr]
                        "latitude", 1
                    ),
                    result["stages"]["metadata"]["output"].__setitem__(  # type: ignore[index,union-attr]
                        "longitude", 2.0
                    ),
                ),
            ),
            (
                "boolean-altitude",
                lambda result: result["stages"]["metadata"]["output"].__setitem__(  # type: ignore[index,union-attr]
                    "altitude", False
                ),
            ),
            (
                "integer-quality-score",
                lambda result: result["stages"].__setitem__(  # type: ignore[union-attr]
                    "quality",
                    {
                        "status": "succeeded",
                        "provenance": deepcopy(result["stages"]["quality"]["provenance"]),  # type: ignore[index]
                        "output": {
                            "aesthetic_score": 1,
                            "technical_quality_score": None,
                        },
                        "artifact_sha256": None,
                        "reason_code": None,
                    },
                ),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                forged = deepcopy(base)
                mutate(forged)
                self._resign(forged, "content_sha256")
                with self.assertRaisesRegex(
                    _AuthorityFailure,
                    "canonical_image_projection_corrupt",
                ):
                    _result_document(canonical_json(forged))

    def test_renderer_rejects_visual_text_search_and_unsafe_source_projection(self) -> None:
        result = self.fixture._sealed_result()
        forged = deepcopy(result)
        embedding = forged["stages"]["embedding"]["output"]  # type: ignore[index]
        assert isinstance(embedding, dict)
        vectors = embedding["vectors"]
        assert isinstance(vectors, list)
        vectors.append(
            {
                "purpose": "text_search",
                "signal": "visual",
                "model_id": "forged-visual-text-v1",
                "dimensions": 3,
                "artifact_sha256": "f" * 64,
            }
        )
        embedding["combined_text_sha256"] = hashlib.sha256(b"").hexdigest()
        self._resign(forged, "content_sha256")
        parsed = _result_document(canonical_json(forged))
        with self.fixture.repository.transaction() as connection:
            with self.assertRaisesRegex(
                _AuthorityFailure,
                "canonical_image_projection_corrupt",
            ):
                _render_projected_row(
                    connection,
                    result=parsed,
                    relative_path="guard.png",
                    projector_version=schema_guards.PROJECTOR_VERSION,
                )

            valid = _result_document(canonical_json(result))
            for relative_path in (
                "a" * 1_025,
                "folder/../guard.png",
                "folder//guard.png",
                "guard\x00.png",
                "a" * 8_193,
            ):
                with self.subTest(relative_path=repr(relative_path)):
                    with self.assertRaisesRegex(
                        _AuthorityFailure,
                        "canonical_image_projection_corrupt",
                    ):
                        _render_projected_row(
                            connection,
                            result=valid,
                            relative_path=relative_path,
                            projector_version=schema_guards.PROJECTOR_VERSION,
                        )

    def test_resigned_publish_receipt_rejects_nonlinear_prior_revision(self) -> None:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                "SELECT publish_receipt_json FROM image_publish_receipts WHERE job_id=?",
                (self.fixture.job_id,),
            ).fetchone()
        assert row is not None
        forged = json.loads(str(row["publish_receipt_json"]))
        forged["expected_prior_head"] = {
            "analysis_run_id": f"arun_{'f' * 32}",
            "revision": 1,
            "content_sha256": "f" * 64,
        }
        self._resign(forged, "publish_receipt_sha256")
        with self.assertRaisesRegex(
            _AuthorityFailure,
            "canonical_image_projection_corrupt",
        ):
            _publish_document(canonical_json(forged))

    def test_result_parent_columns_cannot_diverge_from_publish_prior(self) -> None:
        self._install_complete_projection()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            trigger_sql = self._temporarily_drop_trigger(
                connection,
                "trg_image_results_no_update",
            )
            connection.execute(
                """UPDATE image_analysis_results
                      SET parent_analysis_run_id=?,parent_revision=?,
                          parent_content_sha256=?
                    WHERE asset_id=?""",
                (
                    f"arun_{'f' * 32}",
                    1,
                    "f" * 64,
                    self.fixture.asset_id,
                ),
            )
            connection.execute(trigger_sql)
            connection.commit()

        self._assert_projection_corrupt()

    def test_resigned_projection_receipt_rejects_open_outcome_relations(self) -> None:
        base = self.fixture._projection_receipt("unused-generation")
        cases = (
            ("success-with-reason", {"reason_code": "forged_reason"}),
            ("success-without-row", {"projected_row_sha256": None}),
            (
                "blocked-with-row",
                {"outcome": "blocked", "reason_code": "forged_reason"},
            ),
            ("superseded-without-reason", {"outcome": "superseded"}),
        )
        for name, replacements in cases:
            with self.subTest(name=name):
                forged = deepcopy(base)
                forged.update(replacements)
                self._resign(forged, "receipt_sha256")
                with self.assertRaisesRegex(
                    _AuthorityFailure,
                    "canonical_image_projection_corrupt",
                ):
                    _projection_receipt_document(canonical_json(forged))

    def test_resigned_request_cannot_rebind_immutable_attempt_authority(self) -> None:
        self._install_complete_projection()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection:
            binding = connection.execute(
                "SELECT request_json FROM image_analysis_job_bindings WHERE job_id=?",
                (self.fixture.job_id,),
            ).fetchone()
            receipt_row = connection.execute(
                "SELECT publish_receipt_json FROM image_publish_receipts WHERE job_id=?",
                (self.fixture.job_id,),
            ).fetchone()
            assert binding is not None and receipt_row is not None
            request = json.loads(str(binding[0]))
            request["idempotency"]["key"] = "resigned-forged-key"
            request_sha256 = canonical_sha256(request)
            publish = json.loads(str(receipt_row[0]))
            publish["request_sha256"] = request_sha256
            self._resign(publish, "publish_receipt_sha256")

            binding_trigger = self._temporarily_drop_trigger(
                connection,
                "trg_image_job_bindings_no_update",
            )
            publish_trigger = self._temporarily_drop_trigger(
                connection,
                "trg_image_publish_receipts_no_update",
            )
            connection.execute(
                """UPDATE image_analysis_job_bindings
                      SET idempotency_key=?,request_json=?,request_sha256=?
                    WHERE job_id=?""",
                (
                    "resigned-forged-key",
                    canonical_json(request),
                    request_sha256,
                    self.fixture.job_id,
                ),
            )
            connection.execute(
                """UPDATE image_publish_receipts
                      SET request_sha256=?,publish_receipt_json=?,
                          publish_receipt_sha256=?
                    WHERE job_id=?""",
                (
                    request_sha256,
                    canonical_json(publish),
                    publish["publish_receipt_sha256"],
                    self.fixture.job_id,
                ),
            )
            connection.execute(binding_trigger)
            connection.execute(publish_trigger)
            connection.commit()

        self._assert_projection_corrupt("canonical_image_source_binding_invalid")

    def test_resigned_alias_set_cannot_hide_forged_identity_digest(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """CREATE TABLE image_projection_aliases(
                       generation_id TEXT,legacy_id TEXT,canonical_asset_id TEXT,
                       library_root_id TEXT,source_id TEXT,alias_scope TEXT,
                       alias_sha256 TEXT)"""
            )
            forged_identity_digest = canonical_sha256(
                {
                    "canonical_asset_id": self.fixture.asset_id,
                    "library_root_id": self.fixture.root_id,
                    "source_id": self.fixture.source_id,
                    "alias_scope": "legacy-image-index/v1",
                }
            )
            connection.execute(
                "INSERT INTO image_projection_aliases VALUES(?,?,?,?,?,?,?)",
                (
                    "forged-generation",
                    "legacy-guard",
                    self.fixture.asset_id,
                    self.fixture.root_id,
                    self.fixture.source_id,
                    "legacy-image-index/v1",
                    forged_identity_digest,
                ),
            )
            resigned_set_digest = canonical_sha256(
                [
                    {
                        "legacy_id": "legacy-guard",
                        "alias_sha256": forged_identity_digest,
                    }
                ]
            )
            self.assertRegex(resigned_set_digest, r"^[0-9a-f]{64}$")
            with self.assertRaisesRegex(
                _AuthorityFailure,
                "canonical_image_projection_corrupt",
            ):
                _projection_alias_set(
                    connection,
                    generation_id="forged-generation",
                    database_uuid=self.fixture.repository.database_uuid,
                )

    def test_artifact_media_type_and_payload_bounds_are_closed(self) -> None:
        base_result = self.fixture._sealed_result()
        base_artifacts = self.fixture._artifacts()
        cases = ("empty-media-type", "empty-payload")
        for name in cases:
            with self.subTest(name=name), closing(sqlite3.connect(":memory:")) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute(
                    """CREATE TABLE image_analysis_artifacts(
                           asset_id TEXT,analysis_run_id TEXT,stage TEXT,name TEXT,
                           media_type TEXT,signal TEXT,model_id TEXT,dimensions INTEGER,
                           artifact_sha256 TEXT,size_bytes INTEGER,artifact_blob BLOB)"""
                )
                result = deepcopy(base_result)
                artifacts = deepcopy(base_artifacts)
                metadata = artifacts[0]
                if name == "empty-media-type":
                    metadata["media_type"] = ""
                else:
                    metadata["bytes"] = b""
                    metadata["artifact_sha256"] = hashlib.sha256(b"").hexdigest()
                    result["stages"]["metadata"]["artifact_sha256"] = metadata[  # type: ignore[index]
                        "artifact_sha256"
                    ]
                    self._resign(result, "content_sha256")
                for artifact in artifacts:
                    payload = artifact["bytes"]
                    assert isinstance(payload, bytes)
                    connection.execute(
                        "INSERT INTO image_analysis_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            self.fixture.asset_id,
                            self.fixture.binding["analysis_run_id"],
                            artifact["stage"],
                            artifact["name"],
                            artifact["media_type"],
                            artifact["signal"],
                            artifact["model_id"],
                            artifact["dimensions"],
                            artifact["artifact_sha256"],
                            len(payload),
                            payload,
                        ),
                    )
                with self.assertRaisesRegex(
                    _AuthorityFailure,
                    "canonical_image_projection_corrupt",
                ):
                    _artifacts(
                        connection,
                        asset_id=self.fixture.asset_id,
                        analysis_run_id=str(self.fixture.binding["analysis_run_id"]),
                        result=result,
                    )

    def test_bad_result_self_digest_is_rejected_before_observation_projection(self) -> None:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                "SELECT result_json FROM image_analysis_results WHERE asset_id=?",
                (self.fixture.asset_id,),
            ).fetchone()
        assert row is not None
        forged = json.loads(str(row["result_json"]))
        forged["content_sha256"] = "0" * 64

        with self.assertRaisesRegex(
            _AuthorityFailure,
            "canonical_image_projection_corrupt",
        ):
            _result_document(json.dumps(forged, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
