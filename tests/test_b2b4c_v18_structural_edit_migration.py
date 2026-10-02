from __future__ import annotations

from contextlib import closing
import gc
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

from backend.src import create_app, shutdown_runtime_extensions
from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import (
    SCHEMA_VERSION,
    V18_CHECKSUM,
    V19_CHECKSUM,
    _V18_CAPABILITY_ACTION_ORDER,
    _V18_SCHEMA_OBJECTS,
    MediaMigrationError,
    MediaRepository,
    canonical_json,
)
from tests import test_b2b4_paired_timeline as paired_timeline_fixture
from tests.v18_migration_test_support import (
    downgrade_current_v18_to_exact_v17,
    table_rows,
)


_V18_REBUILT_TABLES = (
    "agent_project_capabilities",
    "canonical_timeline_operations",
    "canonical_timeline_revisions",
    "agent_project_command_receipts",
    "agent_project_capability_events",
)


def _initialize_current(
    root: Path,
) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library, db_path


class V18StructuralEditSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v18-schema-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = _initialize_current(self.root)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_fresh_manifest_and_physical_closed_unions_are_exact(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 20)
        with closing(self.repository._connect()) as connection:
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v18_physical_schema(connection)
            MediaRepository._verify_v19_physical_schema(connection)
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=18"
                    ).fetchone()
                ),
                ("canonical_timeline_structural_edit", V18_CHECKSUM),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=19"
                    ).fetchone()
                ),
                ("canonical_export_output_root_anchor", V19_CHECKSUM),
            )
            expected = MediaRepository._expected_v18_physical_schema()
            actual = {}
            for object_type, name in _V18_SCHEMA_OBJECTS:
                row = connection.execute(
                    "SELECT type,name,sql FROM sqlite_schema WHERE type=? AND name=?",
                    (object_type, name),
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                actual[(str(row["type"]), str(row["name"]))] = (
                    MediaRepository._normalized_schema_sql(row["sql"])
                )
            self.assertEqual(actual, expected)

            for table in (
                "canonical_timeline_operations",
                "agent_project_command_receipts",
                "agent_project_capability_events",
            ):
                sql = str(
                    connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()[0]
                )
                self.assertIn("timeline.apply_structural_edit", sql)
                self.assertNotIn("timeline.*", sql)

            database_uuid = str(
                connection.execute(
                    "SELECT database_uuid FROM database_meta WHERE singleton=1"
                ).fetchone()[0]
            )
            connection.execute("PRAGMA foreign_keys=OFF")
            valid_actions: list[str] = []
            sequence = 0
            for size in range(1, len(_V18_CAPABILITY_ACTION_ORDER) + 1):
                for selection in combinations(_V18_CAPABILITY_ACTION_ORDER, size):
                    sequence += 1
                    actions_json = canonical_json(list(selection))
                    valid_actions.append(actions_json)
                    connection.execute(
                        """INSERT INTO agent_project_capabilities(
                               id,database_uuid,runtime_authority_epoch,project_id,
                               paired_subject_id,claimed_client_label,actions_json,
                               secret_sha256,issued_at,expires_at,max_operations,
                               pairing_receipt_id,facts_sha256)
                             VALUES(?,?,?,'project_v18_schema',?,?,?,?,'2035-01-01T00:00:00Z',
                                    '2035-01-01T00:15:00Z',1,?,?)""",
                        (
                            f"capability_{sequence}",
                            database_uuid,
                            "epoch_v18",
                            f"subject_{sequence}",
                            "Codex or DeepSeek",
                            actions_json,
                            f"{sequence:064x}",
                            f"pairing_{sequence}",
                            "f" * 64,
                        ),
                    )
            self.assertEqual(len(valid_actions), 63)
            self.assertIn(
                '["timeline.apply_structural_edit"]',
                valid_actions,
            )
            self.assertIn(
                canonical_json(list(_V18_CAPABILITY_ACTION_ORDER)),
                valid_actions,
            )
            for invalid_sequence, invalid_actions in enumerate(
                (
                    ["timeline.apply_structural_edit", "timeline.apply_edit"],
                    ["timeline.apply_structural_edit", "timeline.unknown"],
                    ["timeline.*"],
                ),
                start=1,
            ):
                with self.subTest(invalid_actions=invalid_actions), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    connection.execute(
                        """INSERT INTO agent_project_capabilities(
                               id,database_uuid,runtime_authority_epoch,project_id,
                               paired_subject_id,claimed_client_label,actions_json,
                               secret_sha256,issued_at,expires_at,max_operations,
                               pairing_receipt_id,facts_sha256)
                             VALUES(?,?,?,'project_v18_schema',?,?,?,?,'2035-01-01T00:00:00Z',
                                    '2035-01-01T00:15:00Z',1,?,?)""",
                        (
                            f"invalid_capability_{invalid_sequence}",
                            database_uuid,
                            "epoch_v18",
                            f"invalid_subject_{invalid_sequence}",
                            "Codex or DeepSeek",
                            canonical_json(invalid_actions),
                            f"{invalid_sequence + 100:064x}",
                            f"invalid_pairing_{invalid_sequence}",
                            "e" * 64,
                        ),
                    )

    def test_timeline_revision_rows_admit_only_frozen_v1_and_v2(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")

            def insert_revision(schema_version: str, discriminator: int) -> None:
                digest = f"{discriminator:064x}"
                connection.execute(
                    """INSERT INTO canonical_timeline_revisions(
                           project_id,revision,parent_revision,parent_revision_sha256,
                           timeline_id,blueprint_revision,blueprint_content_sha256,
                           blueprint_semantic_sha256,blueprint_operation_id,
                           coverage_revision,coverage_content_sha256,
                           coverage_evidence_manifest_sha256,coverage_operation_id,
                           schema_version,compiler_id,timeline_json,
                           timeline_content_sha256,source_bindings_json,
                           source_bindings_sha256,revision_sha256,operation_id,created_at)
                         VALUES(?,1,NULL,NULL,?,1,?,?,'blueprint_operation',1,?, ?,
                                'coverage_operation',?,'memolens.timeline-lowerer/v1',
                                '{}',?,'[]',?,? ,?,'2035-01-01T00:00:00Z')""",
                    (
                        f"project_schema_{schema_version}",
                        f"timeline_schema_{schema_version}",
                        digest,
                        digest,
                        digest,
                        digest,
                        schema_version,
                        digest,
                        digest,
                        digest,
                        f"operation_schema_{schema_version}",
                    ),
                )

            insert_revision("1", 1)
            insert_revision("2", 2)
            with self.assertRaises(sqlite3.IntegrityError):
                insert_revision("3", 3)
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM canonical_timeline_revisions "
                    "WHERE project_id LIKE 'project_schema_%' ORDER BY schema_version"
                ).fetchall(),
                [("1",), ("2",)],
            )


class V18StructuralEditMigrationFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v18-fault-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = _initialize_current(self.root)
        self.repository.close()
        downgrade_current_v18_to_exact_v17(self.db_path)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _assert_exact_v17_source(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 17)
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=18"
                ).fetchone()
            )
            for table in _V18_REBUILT_TABLES:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
                        (f"{table}_v17",),
                    ).fetchone()
                )

    def test_collision_fails_before_backup_or_source_mutation(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE agent_project_capabilities_v17(attacker TEXT)"
            )
        migrated = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v18_schema_name_collision:agent_project_capabilities_v17:table",
            ):
                migrated.ensure_schema(self.library)
        finally:
            migrated.close()
        self.assertFalse((self.db_path.parent / "migration-backups").exists())
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (17,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT sql FROM sqlite_schema "
                    "WHERE name='agent_project_capabilities_v17'"
                ).fetchone(),
                ("CREATE TABLE agent_project_capabilities_v17(attacker TEXT)",),
            )

    def test_checksum_tamper_fails_before_backup_or_ddl(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum='tampered' WHERE version=17"
            )
        migrated = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "migration_checksum_mismatch for version 17",
            ):
                migrated.ensure_schema(self.library)
        finally:
            migrated.close()
        self.assertFalse((self.db_path.parent / "migration-backups").exists())
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (17,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version=17"
                ).fetchone(),
                ("tampered",),
            )

    def test_injected_copy_fault_rolls_back_and_retry_is_recoverable(self) -> None:
        faulted = MediaRepository(self.db_path)
        try:
            with patch.object(
                MediaRepository,
                "_assert_v13_projection_equal",
                side_effect=MediaMigrationError("injected_v18_copy_fault"),
            ), self.assertRaisesRegex(
                MediaMigrationError,
                "injected_v18_copy_fault",
            ):
                faulted.ensure_schema(self.library)
        finally:
            faulted.close()
        self._assert_exact_v17_source()

        backup_dir = self.db_path.parent / "migration-backups"
        first_manifest_path = next(backup_dir.glob("media-v17-to-v18-*.manifest.json"))
        first_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
        first_backup = backup_dir / first_manifest["backup_filename"]
        self.assertEqual(
            hashlib.sha256(first_backup.read_bytes()).hexdigest(),
            first_manifest["backup_sha256"],
        )
        with closing(sqlite3.connect(first_backup)) as backup:
            backup.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(backup), 17)

        retried = MediaRepository(self.db_path)
        try:
            retried.ensure_schema(self.library)
        finally:
            retried.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v18_physical_schema(connection)
            MediaRepository._verify_v19_physical_schema(connection)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            len(list(backup_dir.glob("media-v17-to-v18-*.manifest.json"))),
            2,
        )

    def test_backup_rejects_same_version_path_source_substitution(self) -> None:
        fake_path = self.root / "same-version-attacker-source.db"
        shutil.copyfile(self.db_path, fake_path)
        with closing(sqlite3.connect(self.db_path)) as live:
            live_updated_at = live.execute(
                "SELECT updated_at FROM database_meta WHERE singleton=1"
            ).fetchone()[0]
        with closing(sqlite3.connect(fake_path)) as fake, fake:
            fake.execute(
                "UPDATE database_meta SET updated_at='ATTACKER-SOURCE-SWAP' "
                "WHERE singleton=1"
            )
        source_uri = f"{self.db_path.as_uri()}?mode=ro&nofollow=1"
        fake_uri = f"{fake_path.as_uri()}?mode=ro&nofollow=1"
        original_connect = sqlite3.connect

        def substitute_source(database, *args, **kwargs):
            if str(database) == source_uri:
                return original_connect(fake_uri, *args, **kwargs)
            return original_connect(database, *args, **kwargs)

        migrated = MediaRepository(self.db_path)
        try:
            with patch(
                "core.media_db.sqlite3.connect",
                side_effect=substitute_source,
            ), self.assertRaisesRegex(
                MediaMigrationError,
                "migration_backup_source_snapshot_mismatch",
            ):
                migrated.ensure_schema(self.library)
        finally:
            migrated.close()

        self._assert_exact_v17_source()
        with closing(sqlite3.connect(self.db_path)) as live:
            self.assertEqual(
                live.execute(
                    "SELECT updated_at FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                live_updated_at,
            )
        backup_dir = self.db_path.parent / "migration-backups"
        self.assertEqual(list(backup_dir.glob("media-v17-to-v18-*")), [])

    def test_backup_directory_swap_cannot_split_copy_and_manifest(self) -> None:
        original_connect = sqlite3.connect
        swapped = False

        def swap_destination_directory(database, *args, **kwargs):
            nonlocal swapped
            value = str(database)
            if (
                not swapped
                and "media-v17-to-v18-" in value
                and "mode=rw" in value
            ):
                swapped = True
                destination = Path(unquote(urlparse(value).path))
                detached = destination.parent.with_name(
                    "migration-backups-detached"
                )
                os.rename(destination.parent, detached)
                os.mkdir(destination.parent, 0o700)
                created = os.open(
                    destination,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                )
                os.close(created)
            return original_connect(database, *args, **kwargs)

        migrated = MediaRepository(self.db_path)
        try:
            with patch(
                "core.media_db.sqlite3.connect",
                side_effect=swap_destination_directory,
            ), self.assertRaisesRegex(
                MediaMigrationError,
                "migration_backup_(?:directory|file)_identity_changed",
            ):
                migrated.ensure_schema(self.library)
        finally:
            migrated.close()

        self.assertTrue(swapped)
        self._assert_exact_v17_source()
        for directory in (
            self.db_path.parent / "migration-backups",
            self.db_path.parent / "migration-backups-detached",
        ):
            self.assertEqual(list(directory.glob("*.manifest.json")), [])
        self.assertEqual(
            [path.stat().st_size for path in (self.db_path.parent / "migration-backups").glob("*.sqlite3")],
            [0],
        )

    def test_future_v21_is_rejected_without_backup_or_managed_ddl(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            before_objects = tuple(
                connection.execute(
                    "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
                )
            )
            connection.execute(
                "UPDATE database_meta SET schema_version=21 WHERE singleton=1"
            )
        migrated = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(MediaMigrationError, "future_schema_version:21"):
                migrated.ensure_schema(self.library)
        finally:
            migrated.close()
        self.assertFalse((self.db_path.parent / "migration-backups").exists())
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (21,),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"
                    )
                ),
                before_objects,
            )


class V18PopulatedStructuralEditMigrationTests(unittest.TestCase):
    _register_image = paired_timeline_fixture.B2B4PairedTimelineApiTests._register_image
    _build_project = paired_timeline_fixture.B2B4PairedTimelineApiTests._build_project
    _pair = paired_timeline_fixture.B2B4PairedTimelineApiTests._pair
    _nonce = paired_timeline_fixture.B2B4PairedTimelineApiTests._nonce
    _edit_body = paired_timeline_fixture.B2B4PairedTimelineApiTests._edit_body
    _save = paired_timeline_fixture.B2B4PairedTimelineApiTests._save

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v18-populated-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "index.db"
        self.desktop_token = "v18-populated-desktop-token"
        self.main_token = "v18-populated-main-token"
        self.epoch = "epoch_v18_populated_test"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(Path(__file__).resolve().parents[1] / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": self.main_token,
                "MEMOLENS_RUNTIME_AUTHORITY_EPOCH": self.epoch,
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()
        self.environment_started = True
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()
        self.repository = self.app.extensions["media_repository"]
        self.blueprints = self.app.extensions["blueprint_service"]
        self.coverage = self.app.extensions["coverage_service"]
        self.timelines = self.app.extensions["timeline_lowering_service"]
        self.legacy_timelines = self.app.extensions["timeline_service"]
        self.token = self.desktop_token
        self._native_nonce_sequence = 0

        self.project_id, self.materialize_payload, _sources = self._build_project()
        materialized = self.timelines.materialize_first_cut(
            self.project_id,
            self.materialize_payload,
            idempotency_key="v18-populated-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(materialized.response_status, 201)
        workspace = self.timelines.read(self.project_id)
        self.timeline_head = workspace["head"]
        timeline = workspace["timeline"]
        assert isinstance(self.timeline_head, dict) and isinstance(timeline, dict)
        self.clip_id = str(timeline["tracks"][0]["clips"][0]["clip_id"])

    def tearDown(self) -> None:
        self._shutdown_live_runtime()
        self.temporary.cleanup()
        gc.collect()

    def _shutdown_live_runtime(self) -> None:
        if self.app is not None:
            shutdown_runtime_extensions(self.app.extensions)
            self.app = None
        if self.environment_started:
            self.environment.stop()
            self.environment_started = False

    def test_populated_v17_to_v18_copy_and_backup_are_byte_exact(self) -> None:
        capability_id, _presentation = self._pair(
            actions=["timeline.apply_edit"],
            max_operations=2,
        )
        edited = self._save(
            capability_id,
            self._edit_body(duration_ms=1_550),
            key="v18-populated-edit",
        )
        self.assertEqual(edited.status_code, 201, edited.json)
        with closing(sqlite3.connect(self.db_path)) as connection:
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in _V18_REBUILT_TABLES
            }
        self.assertTrue(all(count > 0 for count in counts.values()), counts)

        self._shutdown_live_runtime()
        downgrade_current_v18_to_exact_v17(self.db_path)
        before = {table: table_rows(self.db_path, table) for table in _V18_REBUILT_TABLES}
        with closing(sqlite3.connect(self.db_path)) as source:
            source.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(source), 17)
            self.assertEqual(
                [
                    tuple(row)
                    for row in source.execute(
                        "SELECT DISTINCT schema_version "
                        "FROM canonical_timeline_revisions"
                    ).fetchall()
                ],
                [("1",)],
            )

        migrated = MediaRepository(self.db_path)
        try:
            migrated.ensure_schema(self.library)
        finally:
            migrated.close()
        after = {table: table_rows(self.db_path, table) for table in _V18_REBUILT_TABLES}
        self.assertEqual(after, before)

        backup_dir = self.db_path.parent / "migration-backups"
        manifest_path = next(backup_dir.glob("media-v17-to-v18-*.manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = backup_dir / manifest["backup_filename"]
        self.assertEqual(
            (manifest["from_schema_version"], manifest["to_schema_version"]),
            (17, 18),
        )
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        self.assertEqual(
            hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            manifest["backup_sha256"],
        )
        with closing(sqlite3.connect(backup_path)) as backup:
            backup.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(backup), 17)
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("PRAGMA foreign_key_check").fetchall(), [])
        backup_rows = {
            table: table_rows(backup_path, table) for table in _V18_REBUILT_TABLES
        }
        self.assertEqual(backup_rows, before)
        self.assertEqual(len(list(backup_dir.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
