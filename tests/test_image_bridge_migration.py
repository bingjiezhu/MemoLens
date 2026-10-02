from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from core.db import ImageIndexRepository
from core.image_analysis_schema import (
    V14_CHECKSUM,
    V14_SCHEMA_OBJECTS,
    V14_SCHEMA_STATEMENTS,
)
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
from core.media_db import (
    MediaMigrationError,
    MediaRepository,
    SCHEMA_VERSION,
    SCHEMA_STATEMENTS,
    V13_SCHEMA_STATEMENTS,
)
from core.provider_egress_schema import V16_SCHEMA_OBJECTS
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17


_CORE_SNAPSHOT_TABLES = (
    "assets",
    "asset_sources",
    "analysis_runs",
    "media_jobs",
)


class V14ImageBridgeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v14-image-bridge-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _initialize(root: Path) -> tuple[MediaRepository, Path, Path]:
        library = root / "library"
        library.mkdir(parents=True)
        db_path = root / "state" / "media.db"
        db_path.parent.mkdir(parents=True)
        ImageIndexRepository(db_path).ensure_schema()
        repository = MediaRepository(db_path)
        repository.ensure_schema(library)
        return repository, library, db_path

    @staticmethod
    def _register_asset(
        repository: MediaRepository,
        library: Path,
        *,
        relative_path: str,
        content: bytes,
        kind: str,
    ) -> dict[str, object]:
        source_path = library / relative_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(content)
        source_identity = source_path.stat()
        root_id = str(repository.library_roots()[0]["id"])
        return repository.upsert_asset_source(
            root_id=root_id,
            relative_path=relative_path,
            filename=source_path.name,
            kind=kind,
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type="image/jpeg" if kind == "image" else "video/mp4",
            file_size=source_identity.st_size,
            mtime_ns=source_identity.st_mtime_ns,
            source_file_id=str(source_identity.st_ino),
        )

    @staticmethod
    def _core_snapshot(db_path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
        with closing(sqlite3.connect(db_path)) as connection:
            return {
                table: tuple(tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
                for table in _CORE_SNAPSHOT_TABLES
            }

    @staticmethod
    def _assert_exact_v13(db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            assert MediaRepository._schema_preflight(connection) == 13
            MediaRepository._verify_v4_physical_schema(connection)
            MediaRepository._verify_v5_post_v12_physical_schema(connection)
            MediaRepository._verify_v6_physical_schema(connection)
            MediaRepository._verify_v8_physical_schema(connection)
            MediaRepository._verify_v9_post_v13_physical_schema(connection)
            MediaRepository._verify_v11_post_v13_physical_schema(connection)
            MediaRepository._verify_v12_post_v13_physical_schema(connection)
            MediaRepository._verify_v13_physical_schema(connection)
            assert connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version IN (14,15,16,17)"
            ).fetchone()[0] == 0
            for _object_type, name in (
                *V14_SCHEMA_OBJECTS,
                *V16_SCHEMA_OBJECTS,
                *V17_SCHEMA_OBJECTS,
            ):
                assert (
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name=?",
                        (name,),
                    ).fetchone()
                    is None
                )

    @classmethod
    def _downgrade_to_exact_v13(cls, db_path: Path) -> None:
        downgrade_current_v18_to_exact_v17(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            for object_type, name in reversed(V17_SCHEMA_OBJECTS):
                connection.execute(
                    f'DROP {object_type.upper()} IF EXISTS "{name}"'
                )
            connection.execute(
                MediaRepository._expected_v14_physical_schema()[
                    ("trigger", "trg_image_generations_activate_clean_only")
                ]
            )
            for object_type, name in reversed(V16_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} "{name}"')
            connection.execute("DROP TRIGGER trg_agent_capabilities_no_update")
            connection.execute("DROP TRIGGER trg_agent_capabilities_no_delete")
            connection.execute("DROP INDEX idx_agent_capabilities_project_expiry")
            connection.execute(
                "ALTER TABLE agent_project_capabilities "
                "RENAME TO agent_project_capabilities_v15_fixture"
            )
            connection.execute(V13_SCHEMA_STATEMENTS[0])
            connection.execute(
                "INSERT INTO agent_project_capabilities "
                "SELECT * FROM agent_project_capabilities_v15_fixture"
            )
            connection.execute("DROP TABLE agent_project_capabilities_v15_fixture")
            for statement in (
                V13_SCHEMA_STATEMENTS[4],
                V13_SCHEMA_STATEMENTS[9],
                V13_SCHEMA_STATEMENTS[10],
            ):
                connection.execute(statement)
            for object_type, name in reversed(V14_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} "{name}"')
            connection.execute(
                "DELETE FROM schema_migrations WHERE version IN (14,15,16,17)"
            )
            guard = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert guard is not None and guard[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute("UPDATE database_meta SET schema_version=13 WHERE singleton=1")
            connection.execute(str(guard[0]))
            connection.commit()
        cls._assert_exact_v13(db_path)

    def test_exact_v13_upgrades_additively_with_recoverable_backup(self) -> None:
        fixture_root = self.root / "upgrade"
        fixture_root.mkdir()
        repository, library, db_path = self._initialize(fixture_root)
        image = self._register_asset(
            repository,
            library,
            relative_path="album/original.jpg",
            content=b"v13 canonical image fixture",
            kind="image",
        )
        video = self._register_asset(
            repository,
            library,
            relative_path="album/original.mp4",
            content=b"v13 durable video fixture",
            kind="video",
        )
        job = repository.create_analysis_job(asset_id=str(video["id"]))
        database_uuid = repository.database_uuid
        expected_core = self._core_snapshot(db_path)
        repository.close()

        self._downgrade_to_exact_v13(db_path)
        exact_v13_core = self._core_snapshot(db_path)
        self.assertEqual(exact_v13_core, expected_core)

        migrated = MediaRepository(db_path)
        try:
            migrated.ensure_schema(library)
            self.assertEqual(migrated.database_uuid, database_uuid)
        finally:
            migrated.close()

        self.assertEqual(self._core_snapshot(db_path), exact_v13_core)
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta WHERE singleton=1").fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertEqual(
                tuple(connection.execute("SELECT name,checksum FROM schema_migrations WHERE version=14").fetchone()),
                ("canonical_image_publication_bridge", V14_CHECKSUM),
            )
            MediaRepository._verify_v14_physical_schema(
                connection,
                allow_v17_activation_trigger=True,
            )
            MediaRepository._verify_v17_physical_schema(connection)
            actual_objects = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    f"SELECT type,name FROM sqlite_schema WHERE name IN ({','.join('?' for _ in V14_SCHEMA_OBJECTS)})",
                    tuple(name for _object_type, name in V14_SCHEMA_OBJECTS),
                )
            }
            self.assertEqual(actual_objects, set(V14_SCHEMA_OBJECTS))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM assets WHERE id=? AND kind='image'",
                    (image["id"],),
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM media_jobs WHERE id=? AND status='queued'",
                    (job["id"],),
                ).fetchone()
            )

        backup_dir = db_path.parent / "migration-backups"
        artifacts = list(backup_dir.glob("media-v13-to-v14-*"))
        self.assertEqual(len(artifacts), 2)
        manifest_path = next(backup_dir.glob("media-v13-to-v14-*.manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = backup_dir / manifest["backup_filename"]
        self.assertEqual(manifest["object"], "memolens.migration_backup_manifest")
        self.assertEqual(manifest["database_uuid"], database_uuid)
        self.assertEqual(manifest["from_schema_version"], 13)
        self.assertEqual(manifest["to_schema_version"], 14)
        self.assertEqual(manifest["backup_size_bytes"], backup_path.stat().st_size)
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        self.assertEqual(
            hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            manifest["backup_sha256"],
        )
        self._assert_exact_v13(backup_path)
        self.assertEqual(self._core_snapshot(backup_path), exact_v13_core)

    def test_v14_names_collide_fail_closed_before_backup_or_migration(self) -> None:
        representatives = {
            object_type: next(name for candidate_type, name in V14_SCHEMA_OBJECTS if candidate_type == object_type)
            for object_type in ("table", "index", "trigger")
        }
        for expected_type, colliding_name in representatives.items():
            with self.subTest(v14_object_type=expected_type, name=colliding_name):
                case_root = self.root / f"collision-{expected_type}"
                case_root.mkdir()
                repository, library, db_path = self._initialize(case_root)
                repository.close()
                self._downgrade_to_exact_v13(db_path)
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(f'CREATE TABLE "{colliding_name}"(attacker TEXT)')
                    connection.commit()

                collided = MediaRepository(db_path)
                try:
                    with self.assertRaisesRegex(
                        MediaMigrationError,
                        f"v14_schema_name_collision:{colliding_name}:table",
                    ):
                        collided.ensure_schema(library)
                finally:
                    collided.close()

                with closing(sqlite3.connect(db_path)) as connection:
                    self.assertEqual(
                        connection.execute("SELECT schema_version FROM database_meta WHERE singleton=1").fetchone(),
                        (13,),
                    )
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=14").fetchone(),
                        (0,),
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT type FROM sqlite_schema WHERE name=?",
                            (colliding_name,),
                        ).fetchone(),
                        ("table",),
                    )
                backup_dir = db_path.parent / "migration-backups"
                self.assertFalse(backup_dir.exists())

    def test_unknown_legacy_migration_history_fails_before_backup_or_ddl(self) -> None:
        fixture_root = self.root / "unknown-legacy-schema"
        fixture_root.mkdir()
        repository, library, db_path = self._initialize(fixture_root)
        repository.close()
        self._downgrade_to_exact_v13(db_path)
        before = self._core_snapshot(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(
                """INSERT INTO schema_migrations(version,name,checksum,applied_at)
                   VALUES(0,'unknown_legacy_image_schema',?,'2026-08-29T00:00:00Z')""",
                (hashlib.sha256(b"unknown-legacy-image-schema").hexdigest(),),
            )
            connection.commit()

        reopened = MediaRepository(db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "^unknown_schema_version:0$",
            ):
                reopened.ensure_schema(library)
        finally:
            reopened.close()

        self.assertEqual(self._core_snapshot(db_path), before)
        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (13,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM schema_migrations WHERE version=0"
                ).fetchone(),
                ("unknown_legacy_image_schema",),
            )
            for _object_type, name in V14_SCHEMA_OBJECTS:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name=?",
                        (name,),
                    ).fetchone()
                )
        self.assertFalse((db_path.parent / "migration-backups").exists())

    def test_duplicate_canonical_content_identity_fails_before_v14_migration(
        self,
    ) -> None:
        fixture_root = self.root / "duplicate-canonical-identity"
        fixture_root.mkdir()
        repository, library, db_path = self._initialize(fixture_root)
        canonical = self._register_asset(
            repository,
            library,
            relative_path="duplicate/source.jpg",
            content=b"duplicate canonical identity fixture",
            kind="image",
        )
        repository.close()
        self._downgrade_to_exact_v13(db_path)

        assets_schema = next(
            statement
            for statement in SCHEMA_STATEMENTS
            if statement.startswith("CREATE TABLE IF NOT EXISTS assets")
        ).replace("sha256 TEXT NOT NULL UNIQUE", "sha256 TEXT NOT NULL")
        assets_index = next(
            statement
            for statement in SCHEMA_STATEMENTS
            if "idx_assets_kind_probe" in statement
        )
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE assets RENAME TO damaged_assets")
            connection.execute(assets_schema)
            connection.execute("INSERT INTO assets SELECT * FROM damaged_assets")
            connection.execute(
                """INSERT INTO assets(
                       id,kind,sha256,mime_type,file_size,duration_ms,width,height,
                       rotation_degrees,codec_json,captured_at,probe_status,error_code,
                       created_at,updated_at)
                   SELECT ?,kind,sha256,mime_type,file_size,duration_ms,width,height,
                          rotation_degrees,codec_json,captured_at,probe_status,error_code,
                          created_at,updated_at
                     FROM damaged_assets WHERE id=?""",
                ("asset_duplicate_identity", canonical["id"]),
            )
            connection.execute("DROP TABLE damaged_assets")
            connection.execute(assets_index)
            connection.commit()
        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT sha256) FROM assets"
                ).fetchone(),
                (2, 1),
            )

        reopened = MediaRepository(db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "^v14_duplicate_canonical_asset_identity$",
            ):
                reopened.ensure_schema(library)
        finally:
            reopened.close()

        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (13,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=14"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM assets").fetchone(),
                (2,),
            )
            for _object_type, name in V14_SCHEMA_OBJECTS:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name=?",
                        (name,),
                    ).fetchone()
                )
        self.assertFalse((db_path.parent / "migration-backups").exists())

    def test_tampered_exact_v14_physical_schema_is_rejected_on_reopen(self) -> None:
        fixture_root = self.root / "tampered-v14"
        fixture_root.mkdir()
        repository, library, db_path = self._initialize(fixture_root)
        repository.close()

        index_name = "idx_image_analysis_results_run"
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(f"DROP INDEX {index_name}")
            connection.execute(f"CREATE INDEX {index_name} ON image_analysis_results(asset_id,revision)")
            connection.commit()

        reopened = MediaRepository(db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                f"v14_physical_schema_mismatch:{index_name}",
            ):
                reopened.ensure_schema(library)
        finally:
            reopened.close()

        with closing(sqlite3.connect(db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta WHERE singleton=1").fetchone(),
                (SCHEMA_VERSION,),
            )
            self.assertEqual(
                connection.execute("SELECT name,checksum FROM schema_migrations WHERE version=14").fetchone(),
                ("canonical_image_publication_bridge", V14_CHECKSUM),
            )
            tampered_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            self.assertIsNotNone(tampered_sql)
            self.assertIn("asset_id,revision", str(tampered_sql[0]).replace(" ", ""))
        self.assertFalse((db_path.parent / "migration-backups").exists())

    def test_v14_object_registry_covers_every_schema_statement(self) -> None:
        declared: set[tuple[str, str]] = set()
        for statement in V14_SCHEMA_STATEMENTS:
            parts = statement.strip().split()
            self.assertEqual(parts[0], "CREATE")
            if parts[1] == "UNIQUE":
                declared.add((parts[2].casefold(), parts[3]))
            else:
                declared.add((parts[1].casefold(), parts[2]))
        self.assertEqual(set(V14_SCHEMA_OBJECTS), declared)


if __name__ == "__main__":
    unittest.main()
