from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from core.db import ImageIndexRepository
from core.media_db import (
    BASELINE_CHECKSUM,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    V2_CHECKSUM,
    V3_CHECKSUM,
    V3_SCHEMA_STATEMENTS,
    V4_CHECKSUM,
    V4_SCHEMA_STATEMENTS,
    V5_CHECKSUM,
    V5_SCHEMA_STATEMENTS,
    V6_CHECKSUM,
    V6_SCHEMA_STATEMENTS,
    V7_CHECKSUM,
    V7_SCHEMA_STATEMENTS,
    V8_CHECKSUM,
    V8_SCHEMA_STATEMENTS,
    V9_CHECKSUM,
    V10_CHECKSUM,
    V11_CHECKSUM,
    V12_CHECKSUM,
    _V7_SCHEMA_OBJECTS,
    MediaMigrationError,
    MediaRepository,
    utc_now_iso,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def initialize_v6(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    connection = repository._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._migration(connection, 1, "image_index_baseline", BASELINE_CHECKSUM)
        repository._migration(connection, 2, "video_creative_workbench", V2_CHECKSUM)
        for statement in V3_SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._migration(connection, 3, "creator_memory_media_inbox", V3_CHECKSUM)
        for statement in V4_SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._migration(
            connection,
            4,
            "canonical_blueprint_proposal_ledger",
            V4_CHECKSUM,
        )
        for statement in V5_SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._migration(
            connection,
            5,
            "paired_agent_decision_authority",
            V5_CHECKSUM,
        )
        for statement in V6_SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._migration(
            connection,
            6,
            "canonical_coverage_plan_baseline",
            V6_CHECKSUM,
        )
        now = utc_now_iso()
        connection.execute(
            """INSERT INTO database_meta(
                 singleton,database_uuid,schema_version,created_at,updated_at)
               VALUES(1,'database_v6_fixture',6,?,?)""",
            (now, now),
        )
        repository._upsert_library_root(connection, library)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return repository, library, db_path


def initialize_fresh(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library, db_path


def initialize_v8(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    connection = repository._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statements in (
            SCHEMA_STATEMENTS,
            V3_SCHEMA_STATEMENTS,
            V4_SCHEMA_STATEMENTS,
            V5_SCHEMA_STATEMENTS,
            V6_SCHEMA_STATEMENTS,
            V7_SCHEMA_STATEMENTS,
            V8_SCHEMA_STATEMENTS,
        ):
            for statement in statements:
                connection.execute(statement)
        for version, name, checksum in (
            (1, "image_index_baseline", BASELINE_CHECKSUM),
            (2, "video_creative_workbench", V2_CHECKSUM),
            (3, "creator_memory_media_inbox", V3_CHECKSUM),
            (4, "canonical_blueprint_proposal_ledger", V4_CHECKSUM),
            (5, "paired_agent_decision_authority", V5_CHECKSUM),
            (6, "canonical_coverage_plan_baseline", V6_CHECKSUM),
            (7, "canonical_timeline_first_cut", V7_CHECKSUM),
            (8, "canonical_export_usage_ledger", V8_CHECKSUM),
        ):
            repository._migration(connection, version, name, checksum)
        now = utc_now_iso()
        connection.execute(
            """INSERT INTO database_meta(
                 singleton,database_uuid,schema_version,created_at,updated_at)
               VALUES(1,'database_v8_fixture',8,?,?)""",
            (now, now),
        )
        repository._upsert_library_root(connection, library)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return repository, library, db_path


def insert_timeline_rows(connection: sqlite3.Connection) -> None:
    now = utc_now_iso()
    connection.execute(
        """INSERT INTO canonical_timeline_operations(
             id,project_id,sequence,parent_operation_id,command_type,command_version,
             effect_class,expected_blueprint_revision,
             expected_blueprint_content_sha256,expected_blueprint_semantic_sha256,
             expected_blueprint_operation_id,expected_coverage_revision,
             expected_coverage_content_sha256,
             expected_coverage_evidence_manifest_sha256,
             expected_coverage_operation_id,expected_timeline_head_json,request_sha256,
             result_revision,result_revision_sha256,result_timeline_content_sha256,
             result_json,created_at)
           VALUES('timeline_operation','project',1,NULL,'timeline.materialize_first_cut',
                  '1','reversible_project_write',1,?,?, 'blueprint_operation',1,?,?,
                  'coverage_operation','null',?,1,?,?, '{}',?)""",
        (SHA_A, SHA_B, SHA_C, SHA_D, SHA_A, SHA_B, SHA_C, now),
    )
    connection.execute(
        """INSERT INTO canonical_timeline_revisions(
             project_id,revision,parent_revision,parent_revision_sha256,timeline_id,
             blueprint_revision,blueprint_content_sha256,blueprint_semantic_sha256,
             blueprint_operation_id,coverage_revision,coverage_content_sha256,
             coverage_evidence_manifest_sha256,coverage_operation_id,schema_version,
             compiler_id,timeline_json,timeline_content_sha256,source_bindings_json,
             source_bindings_sha256,revision_sha256,operation_id,created_at)
           VALUES('project',1,NULL,NULL,'timeline',1,?,?,'blueprint_operation',1,?,?,
                  'coverage_operation','1','memolens.timeline-lowerer/v1','{}',?,
                  '[]',? ,?,'timeline_operation',?)""",
        (SHA_A, SHA_B, SHA_C, SHA_D, SHA_C, SHA_D, SHA_B, now),
    )
    connection.execute(
        """INSERT INTO canonical_timeline_heads(
             project_id,revision,revision_sha256,timeline_id,timeline_content_sha256,
             blueprint_revision,blueprint_content_sha256,blueprint_semantic_sha256,
             blueprint_operation_id,coverage_revision,coverage_content_sha256,
             coverage_evidence_manifest_sha256,coverage_operation_id,operation_id,
             updated_at)
           VALUES('project',1,?,'timeline',?,1,?,?,'blueprint_operation',1,?,?,
                  'coverage_operation','timeline_operation',?)""",
        (SHA_B, SHA_C, SHA_A, SHA_B, SHA_C, SHA_D, now),
    )
    database_uuid = connection.execute(
        "SELECT database_uuid FROM database_meta WHERE singleton=1"
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO canonical_timeline_receipts(
             database_uuid,authenticated_principal,project_id,command_type,
             command_version,idempotency_key,request_sha256,operation_id,
             response_status,response_json,response_sha256,receipt_sha256,
             resource_id,created_at)
           VALUES(?,'desktop_app','project','timeline.materialize_first_cut','1',
                  'timeline-key',?,'timeline_operation',201,'{}',?,?,'timeline',?)""",
        (database_uuid, SHA_A, SHA_B, SHA_C, now),
    )


class TimelineV7MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-v7-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fresh_schema_has_frozen_v6_and_complete_v7_catalogue(self) -> None:
        repository, _, db_path = initialize_fresh(self.root)
        try:
            self.assertEqual(SCHEMA_VERSION, 20)
            self.assertEqual(
                V6_CHECKSUM,
                "cddb91190311db0f5593db6af5788d54f51c2d82385cad4ea6067da51461716e",
            )
            self.assertEqual(
                V7_CHECKSUM,
                "687918fbb4692d872b025467321d7c2509c64836b8bad7717eef46960783edf2",
            )
            with closing(sqlite3.connect(db_path)) as connection, connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT schema_version FROM database_meta WHERE singleton=1"
                    ).fetchone(),
                    (SCHEMA_VERSION,),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=7"
                    ).fetchone(),
                    ("canonical_timeline_first_cut", V7_CHECKSUM),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=9"
                    ).fetchone(),
                    ("canonical_timeline_reconciliation", V9_CHECKSUM),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=10"
                    ).fetchone(),
                    ("canonical_timeline_persistent_edit", V10_CHECKSUM),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=11"
                    ).fetchone(),
                    ("paired_agent_canonical_timeline_edit", V11_CHECKSUM),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=12"
                    ).fetchone(),
                    (
                        "paired_agent_project_command_receipt_convergence",
                        V12_CHECKSUM,
                    ),
                )
                for object_type, name in _V7_SCHEMA_OBJECTS:
                    self.assertEqual(
                        connection.execute(
                            "SELECT type FROM sqlite_schema WHERE name=?",
                            (name,),
                        ).fetchone(),
                        (object_type,),
                    )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            repository.close()

    def test_v6_to_v7_backup_is_verified_recoverable_and_pre_v7(self) -> None:
        repository, library, db_path = initialize_v6(self.root)
        try:
            repository.ensure_schema(library)
            backup_dir = db_path.parent / "migration-backups"
            manifest_path = next(backup_dir.glob("media-v6-to-v7-*.manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            backup_path = backup_dir / manifest["backup_filename"]
            self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            self.assertEqual(
                hashlib.sha256(backup_path.read_bytes()).hexdigest(),
                manifest["backup_sha256"],
            )
            with closing(sqlite3.connect(backup_path)) as backup, backup:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(backup.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    backup.execute("SELECT schema_version FROM database_meta").fetchone(),
                    (6,),
                )
                self.assertEqual(
                    backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone(),
                    (6,),
                )
                for _, name in _V7_SCHEMA_OBJECTS:
                    self.assertIsNone(
                        backup.execute(
                            "SELECT 1 FROM sqlite_schema WHERE name=?",
                            (name,),
                        ).fetchone()
                    )
            restored_path = self.root / "restored-v6.db"
            restored_path.write_bytes(backup_path.read_bytes())
            restored = MediaRepository(restored_path)
            try:
                restored.ensure_schema(library)
            finally:
                restored.close()
            with closing(sqlite3.connect(restored_path)) as connection, connection:
                self.assertEqual(
                    connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                    (SCHEMA_VERSION,),
                )
        finally:
            repository.close()

    def test_v7_name_collision_and_physical_corruption_fail_before_mutation(self) -> None:
        repository, library, db_path = initialize_v6(self.root)
        try:
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute("CREATE TABLE canonical_timeline_heads(fake TEXT)")
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v7_schema_name_collision:canonical_timeline_heads:table",
            ):
                repository.ensure_schema(library)
            self.assertFalse((db_path.parent / "migration-backups").exists())
        finally:
            repository.close()

        other_root = self.root / "physical"
        other_root.mkdir()
        repository, library, db_path = initialize_fresh(other_root)
        repository.close()
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_canonical_timeline_receipts_no_delete")
        reopened = MediaRepository(db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v9_physical_schema_mismatch:trg_canonical_timeline_receipts_no_delete",
            ):
                reopened.ensure_schema(library)
        finally:
            reopened.close()


class TimelineV9MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-v9-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v8_to_v9_backup_is_recoverable_and_foreign_keys_are_clean(self) -> None:
        repository, library, db_path = initialize_v8(self.root)
        try:
            repository.ensure_schema(library)
            manifest_path = next(
                (db_path.parent / "migration-backups").glob(
                    "media-v8-to-v9-*.manifest.json"
                )
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            backup_path = manifest_path.parent / manifest["backup_filename"]
            self.assertEqual(
                hashlib.sha256(backup_path.read_bytes()).hexdigest(),
                manifest["backup_sha256"],
            )
            with closing(sqlite3.connect(backup_path)) as backup, backup:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(backup.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    backup.execute("SELECT schema_version FROM database_meta").fetchone(),
                    (8,),
                )
                self.assertEqual(
                    backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone(),
                    (8,),
                )
            with closing(sqlite3.connect(db_path)) as connection, connection:
                self.assertEqual(
                    connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                    (SCHEMA_VERSION,),
                )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

            restored_path = self.root / "restored-v8.db"
            restored_path.write_bytes(backup_path.read_bytes())
            restored = MediaRepository(restored_path)
            try:
                restored.ensure_schema(library)
            finally:
                restored.close()
            with closing(sqlite3.connect(restored_path)) as restored_connection:
                self.assertEqual(
                    restored_connection.execute(
                        "SELECT schema_version FROM database_meta"
                    ).fetchone(),
                    (SCHEMA_VERSION,),
                )
        finally:
            repository.close()

    def test_v8_to_v9_restores_connection_pragmas_after_commit(self) -> None:
        repository, library, _db_path = initialize_v8(self.root)

        class RecordingConnection:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self.inner = inner
                self.pragmas_at_close: tuple[int, int] | None = None

            def __getattr__(self, name: str):
                return getattr(self.inner, name)

            def close(self) -> None:
                self.pragmas_at_close = (
                    int(self.inner.execute("PRAGMA foreign_keys").fetchone()[0]),
                    int(
                        self.inner.execute(
                            "PRAGMA legacy_alter_table"
                        ).fetchone()[0]
                    ),
                )
                self.inner.close()

        recording = RecordingConnection(repository._connect())
        try:
            with patch.object(repository, "_connect", return_value=recording):
                repository.ensure_schema(library)
            self.assertEqual(recording.pragmas_at_close, (1, 0))
        finally:
            repository.close()

    def test_rebuild_copies_historic_operation_and_receipt_bytes_without_rehash(self) -> None:
        repository, _library, _db_path = initialize_v8(self.root)
        connection = repository._connect()
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            insert_timeline_rows(connection)
            operation_before = tuple(
                connection.execute(
                    "SELECT * FROM canonical_timeline_operations"
                ).fetchone()
            )
            receipt_before = tuple(
                connection.execute(
                    "SELECT * FROM canonical_timeline_receipts"
                ).fetchone()
            )
            repository._rebuild_v9_timeline_command_tables(connection)
            operation_after = tuple(
                connection.execute(
                    "SELECT * FROM canonical_timeline_operations"
                ).fetchone()
            )
            receipt_after = tuple(
                connection.execute(
                    "SELECT * FROM canonical_timeline_receipts"
                ).fetchone()
            )
            self.assertEqual(operation_after, operation_before)
            self.assertEqual(receipt_after, receipt_before)
            self.assertNotIn(
                "command_type='timeline.materialize_first_cut'",
                "".join(
                    connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE name IN "
                        "('canonical_timeline_operations','canonical_timeline_receipts')"
                    ).fetchall()[0]
                ),
            )
            connection.rollback()
        finally:
            connection.close()
            repository.close()

    def test_failed_v9_verification_rolls_back_both_same_name_rebuilds(self) -> None:
        repository, library, db_path = initialize_v8(self.root)
        try:
            with patch.object(
                MediaRepository,
                "_verify_v9_physical_schema",
                side_effect=MediaMigrationError("injected_v9_verification_failure"),
            ), self.assertRaisesRegex(
                MediaMigrationError,
                "injected_v9_verification_failure",
            ):
                repository.ensure_schema(library)
            with closing(sqlite3.connect(db_path)) as connection, connection:
                self.assertEqual(
                    connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                    (8,),
                )
                self.assertEqual(
                    connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone(),
                    (8,),
                )
                operation_sql = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE name='canonical_timeline_operations'"
                ).fetchone()[0]
                receipt_sql = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE name='canonical_timeline_receipts'"
                ).fetchone()[0]
                self.assertIn("command_type='timeline.materialize_first_cut'", operation_sql)
                self.assertIn("command_type='timeline.materialize_first_cut'", receipt_sql)
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM sqlite_schema WHERE name LIKE '%_v8'"
                    ).fetchall(),
                    [],
                )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            repository.close()


class TimelineV7TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-triggers-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, _, self.db_path = initialize_fresh(self.root)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA foreign_keys=OFF")
        insert_timeline_rows(self.connection)
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.repository.close()
        self.temporary.cleanup()

    def test_operation_revision_receipt_rows_are_physically_immutable(self) -> None:
        for table in (
            "canonical_timeline_operations",
            "canonical_timeline_revisions",
            "canonical_timeline_receipts",
        ):
            with self.subTest(table=table, action="update"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    self.connection.execute(f"UPDATE {table} SET created_at='tampered'")
            with self.subTest(table=table, action="delete"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    self.connection.execute(f"DELETE FROM {table}")

    def test_head_must_start_at_one_move_forward_by_one_and_never_delete(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "start at revision one"):
            self.connection.execute(
                """INSERT INTO canonical_timeline_heads(
                     project_id,revision,revision_sha256,timeline_id,
                     timeline_content_sha256,blueprint_revision,
                     blueprint_content_sha256,blueprint_semantic_sha256,
                     blueprint_operation_id,coverage_revision,coverage_content_sha256,
                     coverage_evidence_manifest_sha256,coverage_operation_id,
                     operation_id,updated_at)
                   VALUES('other-project',2,?,'timeline',?,1,?,?,'blueprint_operation',
                          1,?,?,'coverage_operation','timeline_operation',?)""",
                (SHA_B, SHA_C, SHA_A, SHA_B, SHA_C, SHA_D, utc_now_iso()),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
            self.connection.execute("DELETE FROM canonical_timeline_heads")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "advance by one revision"):
            self.connection.execute(
                "UPDATE canonical_timeline_heads SET revision=3 WHERE project_id='project'"
            )
        self.connection.execute(
            "UPDATE canonical_timeline_heads SET revision=2 WHERE project_id='project'"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT revision FROM canonical_timeline_heads WHERE project_id='project'"
            ).fetchone(),
            (2,),
        )

    def test_source_bindings_reject_path_metadata(self) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK constraint failed"):
            self.connection.execute(
                """INSERT INTO canonical_timeline_revisions(
                     project_id,revision,parent_revision,parent_revision_sha256,timeline_id,
                     blueprint_revision,blueprint_content_sha256,
                     blueprint_semantic_sha256,blueprint_operation_id,coverage_revision,
                     coverage_content_sha256,coverage_evidence_manifest_sha256,
                     coverage_operation_id,schema_version,compiler_id,timeline_json,
                     timeline_content_sha256,source_bindings_json,source_bindings_sha256,
                     revision_sha256,operation_id,created_at)
                   VALUES('other-project',1,NULL,NULL,'timeline',1,?,?,'blueprint_operation',
                          1,?,?,'coverage_operation','1','memolens.timeline-lowerer/v1',
                          '{}',?,'[{\"relative_path\":\"secret.mov\"}]',?,?,
                          'other-operation',?)""",
                (SHA_A, SHA_B, SHA_C, SHA_D, SHA_C, SHA_D, SHA_B, utc_now_iso()),
            )


if __name__ == "__main__":
    unittest.main()
