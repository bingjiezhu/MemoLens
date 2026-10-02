from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from core.db import ImageIndexRepository
from core.media_db import (
    SCHEMA_VERSION,
    V20_CHECKSUM,
    MediaMigrationError,
    MediaRepository,
)


def _initialize_current(root: Path, *, adopt_root: bool) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library if adopt_root else None)
    return repository, library, db_path


def _downgrade_current_v20_to_exact_v19(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        for object_type, name in reversed(MediaRepository._V20_SCHEMA_OBJECTS):
            connection.execute(f"DROP {object_type.upper()} {name}")
        connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
        connection.execute("DELETE FROM schema_migrations WHERE version=20")
        connection.execute(
            "UPDATE database_meta SET schema_version=19 WHERE singleton=1"
        )
        connection.execute(
            MediaRepository._expected_v4_physical_schema()[
                ("trigger", "trg_database_meta_no_schema_downgrade")
            ]
        )


class V20LibraryBootstrapMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v20-bootstrap-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fresh_migration_only_schema_creates_no_ghost_domain_rows(self) -> None:
        repository, _library, db_path = _initialize_current(
            self.root,
            adopt_root=False,
        )
        repository.close()

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(SCHEMA_VERSION, 20)
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v20_physical_schema(connection)
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=20"
                    ).fetchone()
                ),
                ("plugin_first_library_bootstrap", V20_CHECKSUM),
            )
            for table in (
                "library_roots",
                "creative_projects",
                "creative_briefs",
                "media_jobs",
                "library_bootstrap_receipts",
            ):
                with self.subTest(table=table):
                    self.assertEqual(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )

    def test_explicit_legacy_adoption_remains_compatible(self) -> None:
        repository, library, db_path = _initialize_current(
            self.root,
            adopt_root=True,
        )
        repository.close()
        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT canonical_path FROM library_roots"
                ).fetchall(),
                [(str(library),)],
            )

    def test_v19_to_v20_backs_up_and_preserves_existing_rows_without_adoption(self) -> None:
        repository, library, db_path = _initialize_current(
            self.root,
            adopt_root=True,
        )
        repository.close()
        _downgrade_current_v20_to_exact_v19(db_path)

        migrated = MediaRepository(db_path)
        migrated.ensure_schema(None)
        migrated.close()

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v20_physical_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT canonical_path FROM library_roots"
                ).fetchall()[0][0],
                str(library),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM library_bootstrap_receipts"
                ).fetchone()[0],
                0,
            )

        backup_dir = db_path.parent / "migration-backups"
        self.assertEqual(
            len(list(backup_dir.glob("media-v19-to-v20-*.manifest.json"))),
            1,
        )

    def test_v20_name_collision_fails_before_backup(self) -> None:
        repository, _library, db_path = _initialize_current(
            self.root,
            adopt_root=False,
        )
        repository.close()
        _downgrade_current_v20_to_exact_v19(db_path)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE library_bootstrap_receipts(fake TEXT)"
            )

        migrated = MediaRepository(db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v20_schema_name_collision:library_bootstrap_receipts:table",
        ):
            migrated.ensure_schema(None)
        migrated.close()
        self.assertFalse((db_path.parent / "migration-backups").exists())

    def test_future_schema_and_v20_physical_tamper_fail_closed(self) -> None:
        repository, _library, db_path = _initialize_current(
            self.root,
            adopt_root=False,
        )
        repository.close()
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_library_bootstrap_receipts_no_update"
            )
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v20_physical_schema_mismatch:trg_library_bootstrap_receipts_no_update",
        ):
            MediaRepository(db_path).ensure_schema(None)

        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(
                MediaRepository._expected_v20_physical_schema()[
                    ("trigger", "trg_library_bootstrap_receipts_no_update")
                ]
            )
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=21 WHERE singleton=1"
            )
        with self.assertRaisesRegex(MediaMigrationError, "future_schema_version:21"):
            MediaRepository(db_path).ensure_schema(None)


if __name__ == "__main__":
    unittest.main()
