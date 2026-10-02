from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.db import ImageIndexRepository
from core.media_db import (
    SCHEMA_VERSION,
    V19_CHECKSUM,
    CanonicalExportIntegrityError,
    MediaMigrationError,
    MediaRepository,
)
from tests.v18_migration_test_support import downgrade_current_v19_to_exact_v18


def _initialize_current(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library, db_path


class V19ExportRootAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v19-anchor-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = _initialize_current(self.root)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _insert_roots(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executemany(
                """INSERT INTO output_roots(
                     id,kind,canonical_path,permission_fingerprint,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    (
                        "root_user_export_anchor",
                        "user_export",
                        str(self.root / "exports"),
                        "permission-one",
                        "active",
                        "2035-01-01T00:00:00Z",
                        "2035-01-01T00:00:00Z",
                    ),
                    (
                        "root_app_preview_anchor",
                        "app_preview",
                        str(self.root / "preview"),
                        "permission-two",
                        "active",
                        "2035-01-01T00:00:00Z",
                        "2035-01-01T00:00:00Z",
                    ),
                ),
            )

    def test_identity_is_immutable_while_operational_state_remains_mutable(self) -> None:
        self._insert_roots()
        mutations = (
            "UPDATE output_roots SET id='root_changed' WHERE id='root_user_export_anchor'",
            "UPDATE output_roots SET kind='app_preview' WHERE id='root_user_export_anchor'",
            "UPDATE output_roots SET canonical_path='/tmp/changed' WHERE id='root_user_export_anchor'",
            "UPDATE output_roots SET created_at='2040-01-01T00:00:00Z' WHERE id='root_user_export_anchor'",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            for statement in mutations:
                with self.subTest(statement=statement), self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "output_roots identity is immutable",
                ):
                    connection.execute(statement)
            connection.execute(
                """UPDATE output_roots
                      SET permission_fingerprint='permission-refreshed',
                          status='unavailable',updated_at='2035-01-02T00:00:00Z'
                    WHERE id='root_user_export_anchor'"""
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "user_export output_roots are immutable",
            ):
                connection.execute(
                    "DELETE FROM output_roots WHERE id='root_user_export_anchor'"
                )
            connection.execute(
                "DELETE FROM output_roots WHERE id='root_app_preview_anchor'"
            )
            connection.commit()

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT kind,permission_fingerprint,status,updated_at
                         FROM output_roots WHERE id='root_user_export_anchor'"""
                ).fetchone(),
                (
                    "user_export",
                    "permission-refreshed",
                    "unavailable",
                    "2035-01-02T00:00:00Z",
                ),
            )

    def test_search_rejects_missing_or_replaced_anchor_trigger(self) -> None:
        for replacement in (None, "BEGIN SELECT 1; END"):
            with self.subTest(replacement=replacement):
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute("DROP TRIGGER trg_output_roots_identity_immutable")
                    if replacement is not None:
                        connection.execute(
                            """CREATE TRIGGER trg_output_roots_identity_immutable
                               BEFORE UPDATE ON output_roots
                               """ + replacement
                        )
                with self.assertRaisesRegex(
                    CanonicalExportIntegrityError,
                    "canonical_export_output_root_anchor_invalid",
                ):
                    self.repository.canonical_material_search_facts([])
                if replacement is not None:
                    break
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        MediaRepository._expected_v19_physical_schema()[
                            ("trigger", "trg_output_roots_identity_immutable")
                        ]
                    )

    def test_v18_to_v19_preserves_rows_and_installs_exact_manifest(self) -> None:
        self._insert_roots()
        self.repository.close()
        downgrade_current_v19_to_exact_v18(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            before = tuple(
                connection.execute("SELECT * FROM output_roots ORDER BY id")
            )

        migrated = MediaRepository(self.db_path)
        migrated.ensure_schema(self.library)
        migrated.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(SCHEMA_VERSION, 20)
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v19_physical_schema(connection)
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=19"
                    ).fetchone()
                ),
                ("canonical_export_output_root_anchor", V19_CHECKSUM),
            )
            after = tuple(
                tuple(row)
                for row in connection.execute("SELECT * FROM output_roots ORDER BY id")
            )
            self.assertEqual(after, before)

        backup_dir = self.db_path.parent / "migration-backups"
        self.assertEqual(
            len(list(backup_dir.glob("media-v18-to-v19-*.manifest.json"))),
            1,
        )

    def test_v19_name_collision_fails_before_backup(self) -> None:
        self.repository.close()
        downgrade_current_v19_to_exact_v18(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER trg_output_roots_identity_immutable
                   BEFORE UPDATE ON output_roots BEGIN SELECT 1; END"""
            )

        migrated = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v19_schema_name_collision:trg_output_roots_identity_immutable:trigger",
        ):
            migrated.ensure_schema(self.library)
        migrated.close()
        self.assertFalse((self.db_path.parent / "migration-backups").exists())


if __name__ == "__main__":
    unittest.main()
