from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.db import ImageIndexRepository
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
from core.media_db import MediaMigrationError, MediaRepository, SCHEMA_VERSION
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17
from core.provider_egress_schema import (
    V16_CHECKSUM,
    V16_SCHEMA_OBJECTS,
    V16_SCHEMA_STATEMENTS,
)


NOW = "2026-08-29T12:00:00Z"
EXPIRES = "2026-08-29T12:05:00Z"


class ProviderEgressV16MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-provider-v16-")
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

    @staticmethod
    def _drop_v16_to_exact_v15(db_path: Path) -> None:
        ProviderEgressV16MigrationTests._drop_v17_to_exact_v16(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for object_type, name in reversed(V16_SCHEMA_OBJECTS):
                connection.execute(f"DROP {object_type.upper()} {name}")
            connection.execute("DELETE FROM schema_migrations WHERE version=16")
            guard = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert guard is not None and guard[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=15 WHERE singleton=1"
            )
            connection.execute(str(guard[0]))
            connection.commit()
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            assert MediaRepository._schema_preflight(connection) == 15
            MediaRepository._verify_v15_physical_schema(connection)

    @staticmethod
    def _insert_v15_legacy_provider_history(db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                """INSERT INTO provider_opt_in_grants(
                     id,provider,capability,payload_classes_json,asset_scope_json,
                     token_sha256,status,single_use,issued_at,expires_at,consumed_at)
                   VALUES(?,?,?,?,?,?,'active',1,?,?,NULL)""",
                (
                    "legacy-grant",
                    "legacy-provider",
                    "video_vlm",
                    '["thumbnail"]',
                    '["asset_legacy"]',
                    hashlib.sha256(b"legacy-token").hexdigest(),
                    NOW,
                    EXPIRES,
                ),
            )
            connection.execute(
                """INSERT INTO provider_payload_manifests(
                     id,job_id,grant_id,provider,model,capability,
                     payload_classes_json,asset_ids_json,time_ranges_json,
                     payload_sha256_json,planned_bytes,bytes_sent,outcome,
                     retention_policy,retention_evidence,user_opt_in_at,
                     created_at,completed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    "legacy-manifest",
                    "legacy-job",
                    "legacy-grant",
                    "legacy-provider",
                    "legacy-model",
                    "video_vlm",
                    '["thumbnail"]',
                    '["asset_legacy"]',
                    "[]",
                    '[]',
                    0,
                    None,
                    "planned",
                    None,
                    None,
                    NOW,
                    NOW,
                ),
            )

    def test_current_schema_preserves_exact_v16_migration_and_physical_schema(
        self,
    ) -> None:
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V16_CHECKSUM,
            hashlib.sha256(
                "\n".join(
                    " ".join(statement.split())
                    for statement in V16_SCHEMA_STATEMENTS
                ).encode()
            ).hexdigest(),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v16_physical_schema(connection)
            MediaRepository._verify_v17_physical_schema(connection)
            MediaRepository._verify_v18_physical_schema(connection)
            migration = connection.execute(
                "SELECT name,checksum FROM schema_migrations WHERE version=16"
            ).fetchone()
            self.assertEqual(
                tuple(migration),
                ("provider_egress_one_shot_authority", V16_CHECKSUM),
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_exact_v15_upgrade_is_additive_and_backup_is_recoverable(self) -> None:
        self._drop_v16_to_exact_v15(self.db_path)
        self._insert_v15_legacy_provider_history(self.db_path)

        repository = MediaRepository(self.db_path)
        repository.ensure_schema(self.library)
        repository.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v16_physical_schema(connection)
            MediaRepository._verify_v17_physical_schema(connection)
            MediaRepository._verify_v18_physical_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_opt_in_grants"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_payload_manifests"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_egress_grants"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_egress_manifests"
                ).fetchone()[0],
                0,
            )

        backup_dir = self.db_path.parent / "migration-backups"
        backups = sorted(backup_dir.glob("media-v15-to-v16-*.sqlite3"))
        manifests = sorted(backup_dir.glob("media-v15-to-v16-*.manifest.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["from_schema_version"], 15)
        self.assertEqual(manifest["to_schema_version"], 16)
        self.assertEqual(
            hashlib.sha256(backups[0].read_bytes()).hexdigest(),
            manifest["backup_sha256"],
        )
        with closing(sqlite3.connect(backups[0])) as backup:
            backup.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(backup), 15)
            self.assertEqual(
                backup.execute("SELECT COUNT(*) FROM provider_opt_in_grants").fetchone()[0],
                1,
            )
            self.assertIsNone(
                backup.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='provider_egress_grants'"
                ).fetchone()
            )

    def test_v16_collision_and_physical_tamper_fail_closed(self) -> None:
        self._drop_v16_to_exact_v15(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("CREATE VIEW provider_egress_grants AS SELECT 1 AS marker")
        repository = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v16_schema_name_collision:provider_egress_grants:view",
        ):
            repository.ensure_schema(self.library)
        repository.close()
        self.assertFalse((self.db_path.parent / "migration-backups").exists())

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP VIEW provider_egress_grants")
        repository = MediaRepository(self.db_path)
        repository.ensure_schema(self.library)
        repository.close()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_provider_egress_manifests_state_transition"
            )
        damaged = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v16_physical_schema_mismatch:trg_provider_egress_manifests_state_transition",
        ):
            damaged.ensure_schema(self.library)
        damaged.close()


if __name__ == "__main__":
    unittest.main()
