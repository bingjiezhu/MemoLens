from __future__ import annotations

from copy import deepcopy
import hashlib
import unittest

from PIL import Image

from core.image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    materialize_projected_image_index_values,
    projected_image_index_row_sha256,
)
from tests import test_image_projection_schema_guards as schema_guards


class ImageProjectionVerifiedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _activate_clean_generation(self, generation_id: str) -> None:
        with self.fixture.repository.transaction(immediate=True) as connection:
            active = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 LIMIT 2"""
            ).fetchall()
            self.assertLessEqual(len(active), 1)
            previous_generation_id = (
                str(active[0]["id"]) if active else None
            )
            connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=?
                    WHERE id=?""",
                (schema_guards.NOW, generation_id),
            )
            connection.execute(
                """INSERT INTO image_projection_read_manifests(
                       generation_id,previous_generation_id,database_uuid,
                       canonical_high_water_position,compiler_id,
                       projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,
                       mismatched_count,blocked_count,alias_set_sha256,
                       row_set_sha256,manifest_json,manifest_sha256,created_at)
                   SELECT generation_id,?,database_uuid,
                          canonical_high_water_position,compiler_id,
                          projector_version,eligible_count,projected_count,
                          alias_count,missing_count,unexpected_count,
                          mismatched_count,blocked_count,alias_set_sha256,
                          row_set_sha256,manifest_json,manifest_sha256,created_at
                     FROM image_projection_manifests WHERE generation_id=?""",
                (previous_generation_id, generation_id),
            )
            connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete' AND is_active=0""",
                (generation_id,),
            )

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
        placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                values,
            )
        self._activate_clean_generation(generation_id)
        return generation_id

    def _publish_second_asset(self) -> tuple[str, str]:
        image_path = self.fixture.library / "guard-b.png"
        Image.new("RGB", (24, 12), "green").save(image_path)
        payload = image_path.read_bytes()
        observed = image_path.stat()
        asset = self.fixture.repository.upsert_asset_source(
            root_id=self.fixture.root_id,
            relative_path=image_path.name,
            filename=image_path.name,
            kind="image",
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type="image/png",
            file_size=len(payload),
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        source_id = str(asset["asset_source_id"])
        self.fixture.repository.update_image_probe(asset_id, width=24, height=12)
        database_stat = self.fixture.db_path.stat(follow_symlinks=False)
        job = self.fixture.repository.enqueue_image_analysis(
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=asset_id,
            source_id=source_id,
            analysis_profile=schema_guards.PROFILE,
            expected_head=None,
            enqueue_scope="verified-reader/two-assets",
            idempotency_key="verified-reader-second-asset",
        )
        job_id = str(job["id"])
        binding = self.fixture.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        original = (
            self.fixture.asset_id,
            self.fixture.asset_sha256,
            self.fixture.source_id,
            self.fixture.binding,
        )
        try:
            self.fixture.asset_id = asset_id
            self.fixture.asset_sha256 = hashlib.sha256(payload).hexdigest()
            self.fixture.source_id = source_id
            self.fixture.binding = binding
            result = self.fixture._sealed_result()
        finally:
            (
                self.fixture.asset_id,
                self.fixture.asset_sha256,
                self.fixture.source_id,
                self.fixture.binding,
            ) = original
        self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        return asset_id, job_id

    def _publish_second_revision(self) -> tuple[str, dict[str, object]]:
        database_stat = self.fixture.db_path.stat(follow_symlinks=False)
        job = self.fixture.repository.enqueue_image_analysis(
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=self.fixture.asset_id,
            source_id=self.fixture.source_id,
            analysis_profile=schema_guards.PROFILE,
            expected_head=self.fixture.analysis_binding,
            enqueue_scope="verified-reader/same-asset-revision",
            idempotency_key="verified-reader-same-asset-revision-two",
        )
        job_id = str(job["id"])
        binding = self.fixture.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        original_binding = self.fixture.binding
        try:
            self.fixture.binding = binding
            result = self.fixture._sealed_result()
        finally:
            self.fixture.binding = original_binding
        self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        return job_id, {
            "analysis_run_id": str(binding["analysis_run_id"]),
            "revision": int(binding["intended_revision"]),
            "content_sha256": str(result["content_sha256"]),
        }

    def _tamper_projection_row_sha256(
        self,
        *,
        generation_id: str,
        asset_id: str,
    ) -> None:
        with self.fixture.repository.transaction(immediate=True) as connection:
            trigger_rows = connection.execute(
                """SELECT name,sql FROM sqlite_master
                    WHERE type='trigger' AND name IN (?,?) ORDER BY name""",
                (
                    "trg_image_projection_rows_building_update",
                    "trg_image_projection_rows_document_validate_update",
                ),
            ).fetchall()
            self.assertEqual(len(trigger_rows), 2)
            for trigger in trigger_rows:
                connection.execute(f"DROP TRIGGER {trigger['name']}")
            connection.execute(
                """UPDATE image_projection_rows SET row_sha256=?
                    WHERE generation_id=? AND asset_id=?""",
                ("f" * 64, generation_id, asset_id),
            )
            for trigger in trigger_rows:
                connection.execute(str(trigger["sql"]))

    def test_building_generation_with_applied_receipt_remains_pending(self) -> None:
        generation_id = self.fixture._create_generation(with_row=True)
        receipt = self.fixture._projection_receipt(generation_id)
        self.fixture._insert_projection_receipt(generation_id, receipt)

        current = self.fixture.repository.get_current_image_analysis(self.fixture.asset_id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["projection"]["status"], "pending")
        self.assertNotEqual(current["projection"]["status"], "current")
        self.assertEqual(
            current["projection"]["reason_code"],
            "projection_generation_not_active",
        )
        self.assertIsNone(current["projection"]["generation_id"])
        self.assertEqual(
            current["projection"]["processing_generation_id"], generation_id
        )

    def test_complete_active_clean_exact_projection_is_current(self) -> None:
        generation_id = self._install_complete_projection()

        current = self.fixture.repository.get_current_image_analysis(self.fixture.asset_id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(
            current["analysis_binding"],
            self.fixture.analysis_binding,
        )
        self.assertEqual(current["projection"]["status"], "current")
        self.assertEqual(current["projection"]["outcome"], "applied")
        self.assertIsNone(current["projection"]["reason_code"])
        self.assertEqual(current["projection"]["generation_id"], generation_id)
        self.assertEqual(current["projection"]["row_sha256"], self.fixture.row_sha256)

    def test_historical_processing_receipt_does_not_replace_active_generation(self) -> None:
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

        first_current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        second_current = self.fixture.repository.get_current_image_analysis(
            second_asset_id
        )

        assert first_current is not None and second_current is not None
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        for current in (first_current, second_current):
            self.assertEqual(current["projection"]["status"], "current")
            self.assertEqual(
                current["projection"]["generation_id"],
                second["generation_id"],
            )
        self.assertEqual(
            first_current["projection"]["processing_generation_id"],
            first["generation_id"],
        )
        self.assertEqual(
            second_current["projection"]["processing_generation_id"],
            second["generation_id"],
        )

    def test_unrelated_future_change_does_not_stale_current_asset(self) -> None:
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

        assert first_current is not None and second_current is not None
        self.assertEqual(first_current["projection"]["status"], "current")
        self.assertEqual(
            first_current["projection"]["generation_id"], first["generation_id"]
        )
        self.assertEqual(
            first_current["projection"]["processing_generation_id"],
            first["generation_id"],
        )
        self.assertEqual(second_current["projection"]["status"], "pending")
        self.assertIsNone(second_current["projection"]["generation_id"])
        self.assertIsNone(second_current["projection"]["processing_generation_id"])

    def test_unprojected_same_asset_revision_only_stales_that_asset(self) -> None:
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

        assert first_current is not None and second_current is not None
        self.assertEqual(first_current["analysis_binding"], revision_binding)
        self.assertEqual(first_current["projection"]["status"], "pending")
        self.assertIsNone(first_current["projection"]["receipt_sha256"])
        self.assertEqual(second_current["projection"]["status"], "current")
        self.assertEqual(
            second_current["projection"]["generation_id"],
            second["generation_id"],
        )

    def test_tampered_historical_processing_row_fails_closed_after_new_activation(
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
        self._tamper_projection_row_sha256(
            generation_id=str(first["generation_id"]),
            asset_id=self.fixture.asset_id,
        )

        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )

        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )
        self.assertIsNone(current["projection"].get("row_sha256"))

    def test_tampered_active_row_fails_closed_with_historical_receipt(self) -> None:
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

        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )

    def test_self_consistent_but_not_result_derived_row_is_unavailable(self) -> None:
        forged_document = deepcopy(self.fixture.projected_row_document)
        forged_row = forged_document["row"]
        assert isinstance(forged_row, dict)
        forged_row["filename"] = "forged.png"
        forged_row["relative_path"] = "forged.png"
        forged_document["row_sha256"] = projected_image_index_row_sha256(forged_row)

        # Rebind every downstream proof to the self-consistent forged row.  This
        # must pass storage guards and fail only at exact canonical re-render.
        self.fixture.projected_row_document = forged_document
        self.fixture.row_sha256 = str(forged_document["row_sha256"])
        self._install_complete_projection()

        current = self.fixture.repository.get_current_image_analysis(self.fixture.asset_id)

        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )
        self.assertNotIn("row_sha256", current["projection"])


if __name__ == "__main__":
    unittest.main()
