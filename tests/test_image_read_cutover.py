from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import unittest

from core.db import IMAGE_INDEX_PROJECTOR_AUTHORITY_ERROR, ImageIndexRepository
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    materialize_projected_image_index_values,
)
from core.media_db import MediaRepository
from core.photo_atlas import PhotoAtlasService
from tests import test_image_projection_rebuild as projection_rebuild
from tests import test_image_projection_schema_guards as schema_guards


class ImageProjectionReadCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = projection_rebuild.SameHighWaterProjectionRebuildTests(
            methodName="runTest"
        )
        self.harness.setUp()

    def tearDown(self) -> None:
        self.harness.tearDown()

    @property
    def repository(self):  # type: ignore[no-untyped-def]
        return self.harness.fixture.repository

    def _active_generation_id(self) -> str:
        with self.repository.transaction() as connection:
            rows = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 ORDER BY id"""
            ).fetchall()
        self.assertEqual(len(rows), 1)
        return str(rows[0]["id"])

    def _manifest_counts(self, generation_id: str) -> dict[str, int]:
        with self.repository.transaction() as connection:
            row = connection.execute(
                """SELECT missing_count,unexpected_count,mismatched_count,
                          blocked_count
                     FROM image_projection_read_manifests WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        return {
            "missing": int(row["missing_count"]),
            "unexpected": int(row["unexpected_count"]),
            "mismatched": int(row["mismatched_count"]),
            "blocked": int(row["blocked_count"]),
        }

    def _expected_generation_physical_rows(
        self,
        generation_id: str,
    ) -> list[tuple[object, ...]]:
        with self.repository.transaction() as connection:
            generation = connection.execute(
                """SELECT created_at FROM image_projection_generations
                    WHERE id=?""",
                (generation_id,),
            ).fetchone()
            projected = connection.execute(
                """SELECT row_json FROM image_projection_rows
                    WHERE generation_id=? ORDER BY asset_id""",
                (generation_id,),
            ).fetchall()
        self.assertIsNotNone(generation)
        assert generation is not None
        materialized_at = str(generation["created_at"])
        return sorted(
            (
                materialize_projected_image_index_values(
                    json.loads(str(row["row_json"])),
                    created_at=materialized_at,
                    updated_at=materialized_at,
                )
                for row in projected
            ),
            key=lambda row: str(row[0]),
        )

    def _physical_rows(self) -> list[tuple[object, ...]]:
        with self.repository.transaction() as connection:
            rows = connection.execute(
                f"SELECT {','.join(IMAGE_INDEX_SQL_COLUMNS)} "
                "FROM image_index ORDER BY id"
            ).fetchall()
        return [tuple(row) for row in rows]

    def _rollback(self, *, expected: str, target: str) -> dict[str, object]:
        return self.repository.rollback_image_projection_read_generation(
            expected_active_generation_id=expected,
            target_generation_id=target,
        )

    def _legacy_consumer_candidate_ids(self) -> dict[str, list[str]]:
        core_rows = ImageIndexRepository(
            self.harness.fixture.db_path
        ).fetch_candidates()
        with self.repository.transaction() as connection:
            atlas_rows = PhotoAtlasService._active_generation_image_rows(connection)

        plugin_scripts = (
            Path(__file__).resolve().parents[1]
            / ".agents"
            / "plugins"
            / "plugins"
            / "memolens"
            / "scripts"
        )
        if str(plugin_scripts) not in sys.path:
            sys.path.insert(0, str(plugin_scripts))
        photo_store = importlib.import_module("memolens_photo_store")
        sqlite_module = importlib.import_module("memolens_sqlite")
        plugin_reader = photo_store.PhotoIndexReader(
            sqlite_module.ReadOnlyDatabase(self.harness.fixture.db_path),
            self.harness.fixture.library,
        )
        with self.repository.transaction() as connection:
            columns = plugin_reader.database.columns(connection, "image_index")
            select_parts, from_sql, where_sql = (
                plugin_reader._canonical_search_projection(connection, columns)
            )
            plugin_rows = connection.execute(
                f"SELECT {', '.join(select_parts)} {from_sql} {where_sql}"
            ).fetchall()
        return {
            "core": sorted(str(row["id"]) for row in core_rows),
            "atlas": sorted(str(row["id"]) for row in atlas_rows),
            "plugin": sorted(str(row["id"]) for row in plugin_rows),
        }

    def test_clean_parity_successor_is_the_only_active_read_generation(self) -> None:
        previous = self.harness.old_generation_id

        successor = self.harness._rebuild("t066-clean-read-cutover")

        successor_id = str(successor["generation_id"])
        self.assertNotEqual(successor_id, previous)
        self.assertEqual(successor["previous_generation_id"], previous)
        self.assertIs(successor["is_active"], True)
        self.assertEqual(successor["manifest"], successor["canonical_manifest"])
        self.assertEqual(
            self._manifest_counts(successor_id),
            {"missing": 0, "unexpected": 0, "mismatched": 0, "blocked": 0},
        )
        self.assertEqual(self._active_generation_id(), successor_id)
        for asset_id in (
            self.harness.fixture.asset_id,
            self.harness.second_asset_id,
        ):
            current = self.repository.get_current_image_analysis(asset_id)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["projection"]["generation_id"], successor_id)

    def test_unmanaged_physical_row_blocks_cutover_until_explicit_cleanup(self) -> None:
        previous = self.harness.old_generation_id
        self.harness._insert_unmanaged_physical_row()

        dirty = self.harness._rebuild("t066-unexpected-physical-row")

        dirty_id = str(dirty["generation_id"])
        dirty_counts = dirty["manifest"]["counts"]
        self.assertEqual(dirty_counts["unexpected"], 1)
        self.assertEqual(dirty["canonical_manifest"]["counts"]["unexpected"], 0)
        self.assertEqual(
            self._manifest_counts(dirty_id),
            {"missing": 0, "unexpected": 1, "mismatched": 0, "blocked": 0},
        )
        self.assertIs(dirty["is_active"], False)
        self.assertEqual(self._active_generation_id(), previous)
        with self.repository.transaction() as connection:
            orphan = connection.execute(
                "SELECT id FROM image_index WHERE id='orphan_legacy_row'"
            ).fetchone()
        self.assertIsNotNone(orphan)

        for asset_id in (
            self.harness.fixture.asset_id,
            self.harness.second_asset_id,
        ):
            try:
                current = self.repository.get_current_image_analysis(asset_id)
            except ImageAnalysisPersistenceError:
                # An unmanaged physical conflict may make the whole shadow read
                # unavailable, but it must never select the dirty successor.
                continue
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["projection"]["generation_id"], previous)

        with self.repository.transaction(immediate=True) as connection:
            removed = connection.execute(
                "DELETE FROM image_index WHERE id='orphan_legacy_row'"
            )
        self.assertEqual(removed.rowcount, 1)

        clean = self.harness._rebuild("t066-clean-after-explicit-backfill")

        clean_id = str(clean["generation_id"])
        self.assertIs(clean["is_active"], True)
        self.assertEqual(
            self._manifest_counts(clean_id),
            {"missing": 0, "unexpected": 0, "mismatched": 0, "blocked": 0},
        )
        self.assertEqual(self._active_generation_id(), clean_id)
        self.assertNotEqual(clean_id, dirty_id)
        self.assertEqual(clean["previous_generation_id"], previous)

        rolled_back = self._rollback(expected=clean_id, target=previous)
        self.assertEqual(rolled_back["generation_id"], previous)
        self.assertEqual(self._active_generation_id(), previous)

    def test_initial_unmanaged_row_blocks_before_receipt_and_can_retry_clean(self) -> None:
        fixture = schema_guards.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        fixture.setUp()
        try:
            materialized = materialize_projected_image_index_values(
                fixture.projected_row_document,
                created_at=schema_guards.NOW,
                updated_at=schema_guards.NOW,
            )
            orphan = dict(zip(IMAGE_INDEX_SQL_COLUMNS, materialized, strict=True))
            orphan.update(
                {
                    "id": "orphan_initial_cutover",
                    "sha256": "e" * 64,
                    "filename": "orphan-initial.jpg",
                    "relative_path": "orphan/orphan-initial.jpg",
                }
            )
            with fixture.repository.transaction(immediate=True) as connection:
                connection.execute(
                    f"INSERT INTO image_index({','.join(IMAGE_INDEX_SQL_COLUMNS)}) "
                    f"VALUES({','.join('?' for _ in IMAGE_INDEX_SQL_COLUMNS)})",
                    tuple(orphan[column] for column in IMAGE_INDEX_SQL_COLUMNS),
                )

            with self.assertRaises(ImageAnalysisPersistenceError) as failure:
                fixture.repository.project_image_analysis_change(
                    job_id=fixture.job_id,
                    runtime_generation=schema_guards.RUNTIME_GENERATION,
                    expected_attempt=1,
                )
            self.assertEqual(
                failure.exception.code,
                "image_analysis_legacy_backfill_required",
            )
            with fixture.repository.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_projection_receipts"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_projection_generations"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_projection_read_manifests"
                    ).fetchone()[0],
                    0,
                )
                job = connection.execute(
                    "SELECT status,stage FROM media_jobs WHERE id=?",
                    (fixture.job_id,),
                ).fetchone()
                self.assertEqual(tuple(job), ("running", "shadow_projection"))
                physical_ids = [
                    str(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM image_index ORDER BY id"
                    ).fetchall()
                ]
            self.assertEqual(physical_ids, ["orphan_initial_cutover"])
            self.assertEqual(
                ImageIndexRepository(fixture.db_path).fetch_candidates(),
                [],
            )

            with fixture.repository.transaction(immediate=True) as connection:
                removed = connection.execute(
                    "DELETE FROM image_index WHERE id='orphan_initial_cutover'"
                )
            self.assertEqual(removed.rowcount, 1)

            clean = fixture.repository.project_image_analysis_change(
                job_id=fixture.job_id,
                runtime_generation=schema_guards.RUNTIME_GENERATION,
                expected_attempt=1,
            )
            self.assertIs(clean["is_active"], True)
            self.assertIsNone(clean["previous_generation_id"])
            self.assertIsNotNone(clean["projection_receipt"])
            self.assertEqual(clean["manifest"], clean["canonical_manifest"])
            with fixture.repository.transaction() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_projection_receipts"
                    ).fetchone()[0],
                    1,
                )
                active = connection.execute(
                    "SELECT id FROM image_projection_generations WHERE is_active=1"
                ).fetchone()
            self.assertEqual(str(active["id"]), clean["generation_id"])
        finally:
            fixture.tearDown()

    def test_legacy_consumers_require_exact_full_read_manifest_equality(self) -> None:
        active = self.harness._rebuild("t066-consumer-read-manifest-gate")
        active_id = str(active["generation_id"])
        expected_ids = sorted(
            [self.harness.fixture.asset_id, self.harness.second_asset_id]
        )
        self.assertEqual(
            self._legacy_consumer_candidate_ids(),
            {name: expected_ids for name in ("core", "atlas", "plugin")},
        )
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "DROP TRIGGER trg_image_projection_read_manifests_no_update"
            )
            changed = connection.execute(
                """UPDATE image_projection_read_manifests
                      SET unexpected_count=1 WHERE generation_id=?""",
                (active_id,),
            )
        self.assertEqual(changed.rowcount, 1)
        self.assertEqual(
            self._legacy_consumer_candidate_ids(),
            {name: [] for name in ("core", "atlas", "plugin")},
        )

        with self.repository.transaction(immediate=True) as connection:
            canonical = connection.execute(
                """SELECT manifest_json,manifest_sha256
                     FROM image_projection_manifests WHERE generation_id=?""",
                (active_id,),
            ).fetchone()
            assert canonical is not None
            connection.execute(
                """UPDATE image_projection_read_manifests
                      SET unexpected_count=0,manifest_sha256=?
                    WHERE generation_id=?""",
                ("f" * 64, active_id),
            )
        self.assertEqual(
            self._legacy_consumer_candidate_ids(),
            {name: [] for name in ("core", "atlas", "plugin")},
        )

        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE image_projection_read_manifests
                      SET manifest_sha256=?,manifest_json=?
                    WHERE generation_id=?""",
                (
                    str(canonical["manifest_sha256"]),
                    str(canonical["manifest_json"]) + " ",
                    active_id,
                ),
            )
        self.assertEqual(
            self._legacy_consumer_candidate_ids(),
            {name: [] for name in ("core", "atlas", "plugin")},
        )

    def test_rollback_only_to_immediate_clean_predecessor_rematerializes_and_persists(
        self,
    ) -> None:
        first = self.harness._rebuild("t066-rollback-first")
        first_id = str(first["generation_id"])
        second = self.harness._rebuild("t066-rollback-second")
        second_id = str(second["generation_id"])
        self.assertEqual(second["previous_generation_id"], first_id)
        expected_physical = self._expected_generation_physical_rows(first_id)

        with self.repository.transaction(immediate=True) as connection:
            changed = connection.execute(
                "UPDATE image_index SET description='tampered physical row' WHERE id=?",
                (self.harness.fixture.asset_id,),
            )
        self.assertEqual(changed.rowcount, 1)
        self.assertNotEqual(self._physical_rows(), expected_physical)

        rolled_back = self._rollback(expected=second_id, target=first_id)

        self.assertEqual(rolled_back["generation_id"], first_id)
        self.assertEqual(
            rolled_back["previous_generation_id"],
            self.harness.old_generation_id,
        )
        self.assertEqual(
            rolled_back["rolled_back_from_generation_id"],
            second_id,
        )
        self.assertIs(rolled_back["is_active"], True)
        self.assertEqual(self._active_generation_id(), first_id)
        self.assertEqual(self._physical_rows(), expected_physical)
        for asset_id in (
            self.harness.fixture.asset_id,
            self.harness.second_asset_id,
        ):
            current = self.repository.get_current_image_analysis(asset_id)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["projection"]["generation_id"], first_id)

        physical_before_legacy_write = self._physical_rows()
        with self.assertRaisesRegex(
            RuntimeError,
            IMAGE_INDEX_PROJECTOR_AUTHORITY_ERROR,
        ):
            ImageIndexRepository(self.harness.fixture.db_path).update_image_quality(
                image_id=self.harness.fixture.asset_id,
                aesthetic_score=0.99,
                aesthetic_model="forbidden-legacy-writer",
                technical_quality_score=0.99,
                aesthetic_updated_at="2026-08-29T00:00:00+00:00",
            )
        self.assertEqual(self._physical_rows(), physical_before_legacy_write)

        self.repository.close()
        reopened = MediaRepository(self.harness.fixture.db_path)
        try:
            reopened.ensure_schema(self.harness.fixture.library)
            with reopened.transaction() as connection:
                active = connection.execute(
                    """SELECT id FROM image_projection_generations
                        WHERE is_active=1 ORDER BY id"""
                ).fetchall()
            self.assertEqual([str(row["id"]) for row in active], [first_id])
            current = reopened.get_current_image_analysis(
                self.harness.fixture.asset_id
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current["projection"]["generation_id"], first_id)
        finally:
            reopened.close()

    def test_wrong_expected_and_non_predecessor_rollback_are_zero_write(self) -> None:
        first = self.harness._rebuild("t066-reject-first")
        first_id = str(first["generation_id"])
        second = self.harness._rebuild("t066-reject-second")
        second_id = str(second["generation_id"])

        cases = (
            (first_id, first_id),
            (second_id, self.harness.old_generation_id),
        )
        for expected, target in cases:
            with self.subTest(expected=expected, target=target):
                before = self.harness._projection_snapshot()
                with self.assertRaises(ImageAnalysisPersistenceError):
                    self._rollback(expected=expected, target=target)
                self.assertEqual(self.harness._projection_snapshot(), before)
                self.assertEqual(self._active_generation_id(), second_id)

    def test_dirty_immediate_target_is_rejected_without_state_change(self) -> None:
        first = self.harness._rebuild("t066-dirty-target-first")
        first_id = str(first["generation_id"])
        second = self.harness._rebuild("t066-dirty-target-second")
        second_id = str(second["generation_id"])
        self.assertEqual(second["previous_generation_id"], first_id)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "DROP TRIGGER trg_image_projection_read_manifests_no_update"
            )
            changed = connection.execute(
                """UPDATE image_projection_read_manifests
                      SET unexpected_count=1 WHERE generation_id=?""",
                (first_id,),
            )
        self.assertEqual(changed.rowcount, 1)
        before = self.harness._projection_snapshot()

        with self.assertRaises(ImageAnalysisPersistenceError):
            self._rollback(expected=second_id, target=first_id)

        self.assertEqual(self.harness._projection_snapshot(), before)
        self.assertEqual(self._active_generation_id(), second_id)

    def test_cross_database_target_is_rejected_without_state_change(self) -> None:
        active = self.harness._rebuild("t066-cross-db-active")
        active_id = str(active["generation_id"])
        other = projection_rebuild.SameHighWaterProjectionRebuildTests(
            methodName="runTest"
        )
        other.setUp()
        try:
            foreign = other._rebuild("t066-cross-db-foreign")
            before = self.harness._projection_snapshot()

            with self.assertRaises(ImageAnalysisPersistenceError):
                self._rollback(
                    expected=active_id,
                    target=str(foreign["generation_id"]),
                )

            self.assertEqual(self.harness._projection_snapshot(), before)
            self.assertEqual(self._active_generation_id(), active_id)
        finally:
            other.tearDown()


if __name__ == "__main__":
    unittest.main()
