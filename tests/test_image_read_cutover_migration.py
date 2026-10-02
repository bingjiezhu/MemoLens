from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.db import ImageIndexRepository
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_read_cutover_schema import (
    V17_CHECKSUM,
    V17_SCHEMA_OBJECTS,
    V17_SCHEMA_STATEMENTS,
)
from core.media_db import MediaMigrationError, MediaRepository, SCHEMA_VERSION
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17
from tests import test_image_projection_materialization as materialization_fixture
from tests import test_image_projection_schema_guards as schema_fixture


class ImageReadCutoverV17MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-image-read-cutover-v17-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "media.db"
        ImageIndexRepository(self.db_path).ensure_schema()
        repository = MediaRepository(self.db_path)
        repository.ensure_schema(self.library)
        repository.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _drop_v17_to_exact_v16(db_path: Path) -> None:
        downgrade_current_v18_to_exact_v17(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for object_type, name in reversed(V17_SCHEMA_OBJECTS):
                connection.execute(f"DROP {object_type.upper()} {name}")
            old_activation = MediaRepository._expected_v14_physical_schema()[
                ("trigger", "trg_image_generations_activate_clean_only")
            ]
            connection.execute(old_activation)
            connection.execute("DELETE FROM schema_migrations WHERE version=17")
            guard = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert guard is not None and guard[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=16 WHERE singleton=1"
            )
            connection.execute(str(guard[0]))
            connection.commit()
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            assert MediaRepository._schema_preflight(connection) == 16
            MediaRepository._verify_v14_physical_schema(connection)
            MediaRepository._verify_v16_physical_schema(connection)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    def _projection_harness(
        self,
    ) -> materialization_fixture.ImageProjectionMaterializationTests:
        harness = materialization_fixture.ImageProjectionMaterializationTests(
            methodName="runTest"
        )
        harness.setUp()
        self.addCleanup(harness.tearDown)
        return harness

    @staticmethod
    def _schema_rows(db_path: Path) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(db_path)) as connection:
            return connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
            ).fetchall()

    def test_fresh_v17_has_exact_migration_lineage_and_physical_schema(
        self,
    ) -> None:
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V17_CHECKSUM,
            hashlib.sha256(
                "\n".join(
                    " ".join(statement.split())
                    for statement in V17_SCHEMA_STATEMENTS
                ).encode()
            ).hexdigest(),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v14_physical_schema(
                connection,
                allow_v17_activation_trigger=True,
            )
            MediaRepository._verify_v17_physical_schema(connection)
            MediaRepository._verify_v18_physical_schema(connection)
            migration = connection.execute(
                "SELECT name,checksum FROM schema_migrations WHERE version=17"
            ).fetchone()
            self.assertEqual(
                tuple(migration),
                ("canonical_image_read_cutover", V17_CHECKSUM),
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(image_projection_read_manifests)"
                ).fetchall()
            }
            self.assertIn("previous_generation_id", columns)
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )

    def test_exact_v16_clean_active_upgrade_backfills_and_backs_up(self) -> None:
        harness = self._projection_harness()
        fixture = harness.fixture
        projected = harness._project()
        generation_id = str(projected["generation_id"])
        self._drop_v17_to_exact_v16(fixture.db_path)

        fixture.repository.ensure_schema(fixture.library)

        with fixture.repository.transaction() as connection:
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v17_physical_schema(connection)
            MediaRepository._verify_v18_physical_schema(connection)
            state = connection.execute(
                """SELECT generation.is_active,read_manifest.*,
                          canonical.manifest_json AS canonical_manifest_json,
                          canonical.manifest_sha256 AS canonical_manifest_sha256
                     FROM image_projection_generations generation
                     JOIN image_projection_read_manifests read_manifest
                       ON read_manifest.generation_id=generation.id
                     JOIN image_projection_manifests canonical
                       ON canonical.generation_id=generation.id
                    WHERE generation.id=?""",
                (generation_id,),
            ).fetchone()
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(int(state["is_active"]), 1)
        self.assertIsNone(state["previous_generation_id"])
        self.assertEqual(int(state["unexpected_count"]), 0)
        self.assertEqual(str(state["manifest_json"]), state["canonical_manifest_json"])
        self.assertEqual(
            str(state["manifest_sha256"]),
            state["canonical_manifest_sha256"],
        )

        backup_dir = fixture.db_path.parent / "migration-backups"
        backups = sorted(backup_dir.glob("media-v16-to-v17-*.sqlite3"))
        manifests = sorted(backup_dir.glob("media-v16-to-v17-*.manifest.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["from_schema_version"], 16)
        self.assertEqual(manifest["to_schema_version"], 17)
        self.assertEqual(
            hashlib.sha256(backups[0].read_bytes()).hexdigest(),
            manifest["backup_sha256"],
        )
        with closing(sqlite3.connect(backups[0])) as backup:
            backup.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(backup), 16)
            MediaRepository._verify_v14_physical_schema(backup)
            MediaRepository._verify_v16_physical_schema(backup)
            self.assertIsNone(
                backup.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE name='image_projection_read_manifests'"
                ).fetchone()
            )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "image_projection_read_manifests are immutable",
        ), fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE image_projection_read_manifests
                      SET previous_generation_id=? WHERE generation_id=?""",
                (generation_id, generation_id),
            )

    def test_exact_v16_dirty_active_is_manifested_then_deactivated(self) -> None:
        harness = self._projection_harness()
        fixture = harness.fixture
        projected = harness._project()
        generation_id = str(projected["generation_id"])
        self._drop_v17_to_exact_v16(fixture.db_path)
        physical = harness._physical_rows()
        self.assertEqual(len(physical), 1)
        unmanaged = deepcopy(physical[0])
        unmanaged.update(
            {
                "id": "unmanaged_raw_legacy_row",
                "sha256": "f" * 64,
                "filename": "unmanaged.png",
                "relative_path": "unmanaged.png",
            }
        )
        harness._insert_physical(unmanaged)

        fixture.repository.ensure_schema(fixture.library)

        with fixture.repository.transaction() as connection:
            generation = connection.execute(
                "SELECT is_active FROM image_projection_generations WHERE id=?",
                (generation_id,),
            ).fetchone()
            read_manifest = connection.execute(
                """SELECT previous_generation_id,unexpected_count,manifest_json
                     FROM image_projection_read_manifests WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
            active_count = connection.execute(
                "SELECT COUNT(*) FROM image_projection_generations WHERE is_active=1"
            ).fetchone()[0]
            unmanaged_row = connection.execute(
                "SELECT id FROM image_index WHERE id='unmanaged_raw_legacy_row'"
            ).fetchone()
        self.assertIsNotNone(generation)
        self.assertIsNotNone(read_manifest)
        assert generation is not None and read_manifest is not None
        self.assertEqual(int(generation["is_active"]), 0)
        self.assertEqual(active_count, 0)
        self.assertIsNone(read_manifest["previous_generation_id"])
        self.assertEqual(int(read_manifest["unexpected_count"]), 1)
        self.assertEqual(
            json.loads(str(read_manifest["manifest_json"]))["counts"]["unexpected"],
            1,
        )
        self.assertIsNotNone(unmanaged_row)

        with fixture.repository.transaction() as connection:
            high_water = int(
                connection.execute(
                    "SELECT MAX(position) FROM image_projection_changes"
                ).fetchone()[0]
            )
            _alias_count, alias_set_sha256 = (
                fixture.repository._image_alias_set_in_transaction(
                    connection,
                    fixture.repository.database_uuid,
                )
            )
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_projection_rebuild_read_cutover_blocked",
        ):
            fixture.repository.rebuild_image_projection_generation(
                fixed_high_water=high_water,
                expected_alias_set_sha256=alias_set_sha256,
                rebuild_nonce="v16-dirty-cutover-still-blocked",
                runtime_generation=schema_fixture.RUNTIME_GENERATION,
            )
        with fixture.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_projection_generations"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_projection_read_manifests"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_projection_generations "
                    "WHERE is_active=1"
                ).fetchone()[0],
                0,
            )

        with fixture.repository.transaction(immediate=True) as connection:
            removed = connection.execute(
                "DELETE FROM image_index WHERE id='unmanaged_raw_legacy_row'"
            )
        self.assertEqual(removed.rowcount, 1)

        recovered = fixture.repository.rebuild_image_projection_generation(
            fixed_high_water=high_water,
            expected_alias_set_sha256=alias_set_sha256,
            rebuild_nonce="v16-dirty-cutover-recovery",
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
        )

        self.assertIs(recovered["replayed"], False)
        self.assertIs(recovered["is_active"], True)
        self.assertIsNone(recovered["previous_generation_id"])
        with fixture.repository.transaction() as connection:
            active = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 ORDER BY id"""
            ).fetchall()
            recovered_read = connection.execute(
                """SELECT previous_generation_id,missing_count,unexpected_count,
                          mismatched_count,blocked_count
                     FROM image_projection_read_manifests WHERE generation_id=?""",
                (recovered["generation_id"],),
            ).fetchone()
        self.assertEqual([str(row["id"]) for row in active], [recovered["generation_id"]])
        self.assertIsNotNone(recovered_read)
        assert recovered_read is not None
        self.assertIsNone(recovered_read["previous_generation_id"])
        self.assertEqual(
            tuple(
                int(recovered_read[field])
                for field in (
                    "missing_count",
                    "unexpected_count",
                    "mismatched_count",
                    "blocked_count",
                )
            ),
            (0, 0, 0, 0),
        )

    def test_v17_collision_and_physical_tamper_fail_before_write(self) -> None:
        self._drop_v17_to_exact_v16(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "CREATE VIEW image_projection_read_manifests AS SELECT 1 AS marker"
            )
        before = self._schema_rows(self.db_path)
        repository = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v17_schema_name_collision:image_projection_read_manifests:view",
        ):
            repository.ensure_schema(self.library)
        repository.close()
        self.assertEqual(self._schema_rows(self.db_path), before)
        self.assertFalse((self.db_path.parent / "migration-backups").exists())

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP VIEW image_projection_read_manifests")
        repository = MediaRepository(self.db_path)
        repository.ensure_schema(self.library)
        repository.close()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_image_projection_read_manifests_no_delete"
            )
        damaged = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v17_physical_schema_mismatch:trg_image_projection_read_manifests_no_delete",
        ):
            damaged.ensure_schema(self.library)
        damaged.close()

    def test_v16_history_rejects_v17_activation_trigger_before_backup(self) -> None:
        self._drop_v17_to_exact_v16(self.db_path)
        activation = next(
            statement
            for statement in V17_SCHEMA_STATEMENTS
            if statement.lstrip().startswith(
                "CREATE TRIGGER trg_image_generations_activate_clean_only"
            )
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_image_generations_activate_clean_only")
            connection.execute(activation)
        before = self._schema_rows(self.db_path)

        repository = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v14_physical_schema_mismatch:trg_image_generations_activate_clean_only",
        ):
            repository.ensure_schema(self.library)
        repository.close()
        self.assertEqual(self._schema_rows(self.db_path), before)
        self.assertFalse((self.db_path.parent / "migration-backups").exists())

    def test_future_v21_fails_closed_without_schema_write(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES(21,'future','future','2026-08-29T00:00:00+00:00')"
            )
            connection.execute(
                "UPDATE database_meta SET schema_version=21 WHERE singleton=1"
            )
        before = self._schema_rows(self.db_path)

        repository = MediaRepository(self.db_path)
        with self.assertRaisesRegex(MediaMigrationError, "future_schema_version:21"):
            repository.ensure_schema(self.library)
        repository.close()
        self.assertEqual(self._schema_rows(self.db_path), before)
        self.assertFalse((self.db_path.parent / "migration-backups").exists())


if __name__ == "__main__":
    unittest.main()
