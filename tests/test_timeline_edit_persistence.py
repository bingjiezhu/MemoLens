from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.db import ImageIndexRepository
from core.image_analysis_schema import V14_SCHEMA_OBJECTS
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
from core.media_db import (
    MediaRepository,
    SCHEMA_VERSION,
    V5_SCHEMA_STATEMENTS,
    V9_SCHEMA_STATEMENTS,
    V10_CHECKSUM,
    V11_SCHEMA_STATEMENTS,
    V12_SCHEMA_STATEMENTS,
)
from core.provider_egress_schema import V16_SCHEMA_OBJECTS
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17
try:
    from tests import test_timeline_lowering_integration as timeline_fixture
except ImportError:  # pragma: no cover - unittest discovery from tests/
    import test_timeline_lowering_integration as timeline_fixture


def _downgrade_current_database_to_exact_v12(db_path: Path) -> None:
    """Reverse only V13's closed-union table rebuilds."""

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
            connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
        for object_type, name in reversed(V14_SCHEMA_OBJECTS):
            connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version IN (14,15,16,17)"
        )
        for name in (
            "trg_agent_capabilities_no_update",
            "trg_agent_capabilities_no_delete",
            "trg_canonical_timeline_operations_no_update",
            "trg_canonical_timeline_operations_no_delete",
            "trg_agent_project_receipts_no_update",
            "trg_agent_project_receipts_no_delete",
            "trg_agent_capability_events_no_update",
            "trg_agent_capability_events_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        for name in (
            "idx_agent_capabilities_project_expiry",
            "idx_canonical_timeline_operations_project_sequence",
            "idx_agent_project_receipts_operation",
            "idx_agent_capability_events_sequence",
            "idx_agent_capability_events_native_gesture",
        ):
            connection.execute(f"DROP INDEX {name}")
        for table in (
            "agent_project_capability_events",
            "agent_project_command_receipts",
            "canonical_timeline_operations",
            "agent_project_capabilities",
        ):
            connection.execute(
                f"ALTER TABLE {table} RENAME TO {table}_v13_fixture"
            )
        for statement in (
            V11_SCHEMA_STATEMENTS[0],
            V9_SCHEMA_STATEMENTS[0],
            V12_SCHEMA_STATEMENTS[0],
            V12_SCHEMA_STATEMENTS[1],
        ):
            connection.execute(statement)
        for table in (
            "agent_project_capabilities",
            "canonical_timeline_operations",
            "agent_project_command_receipts",
            "agent_project_capability_events",
        ):
            connection.execute(
                f"INSERT INTO {table} SELECT * FROM {table}_v13_fixture"
            )
        for table in (
            "agent_project_capability_events_v13_fixture",
            "agent_project_command_receipts_v13_fixture",
            "canonical_timeline_operations_v13_fixture",
            "agent_project_capabilities_v13_fixture",
        ):
            connection.execute(f"DROP TABLE {table}")
        for statement in (
            V11_SCHEMA_STATEMENTS[3],
            V11_SCHEMA_STATEMENTS[7],
            V11_SCHEMA_STATEMENTS[8],
            V9_SCHEMA_STATEMENTS[2],
            V9_SCHEMA_STATEMENTS[4],
            V9_SCHEMA_STATEMENTS[5],
            *V12_SCHEMA_STATEMENTS[2:],
        ):
            connection.execute(statement)
        connection.execute("DELETE FROM schema_migrations WHERE version=13")
        guard = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='trg_database_meta_no_schema_downgrade'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
        connection.execute(
            "UPDATE database_meta SET schema_version=12 WHERE singleton=1"
        )
        connection.execute(guard)
        connection.commit()


def _downgrade_current_database_to_exact_v10(db_path: Path) -> None:
    """Reverse V13/V12/V11 to one exact V10 schema."""

    _downgrade_current_database_to_exact_v12(db_path)

    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute("BEGIN IMMEDIATE")
        for name in (
            "trg_agent_capabilities_no_update",
            "trg_agent_capabilities_no_delete",
            "trg_agent_project_receipts_no_update",
            "trg_agent_project_receipts_no_delete",
            "trg_agent_capability_events_no_update",
            "trg_agent_capability_events_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        for name in (
            "idx_agent_capabilities_project_expiry",
            "idx_agent_project_receipts_operation",
            "idx_agent_capability_events_sequence",
            "idx_agent_capability_events_native_gesture",
        ):
            connection.execute(f"DROP INDEX {name}")
        connection.execute(
            "ALTER TABLE agent_project_capability_events "
            "RENAME TO agent_project_capability_events_v11_fixture"
        )
        connection.execute(
            "ALTER TABLE agent_project_capabilities "
            "RENAME TO agent_project_capabilities_v11_fixture"
        )
        connection.execute(V5_SCHEMA_STATEMENTS[2])
        connection.execute(
            """INSERT INTO agent_blueprint_command_receipts(
                 id,database_uuid,capability_id,paired_subject_id,project_id,
                 command_type,command_version,idempotency_key,request_sha256,
                 operation_id,response_status,response_json,response_sha256,
                 receipt_sha256,resource_type,resource_id,created_at)
               SELECT id,database_uuid,capability_id,paired_subject_id,project_id,
                      command_type,command_version,idempotency_key,request_sha256,
                      operation_id,response_status,response_json,response_sha256,
                      receipt_sha256,resource_type,resource_id,created_at
                 FROM agent_project_command_receipts
                WHERE receipt_schema_version='1' ORDER BY rowid"""
        )
        connection.execute(V5_SCHEMA_STATEMENTS[0])
        connection.execute(
            """INSERT INTO agent_project_capabilities(
                 id,database_uuid,runtime_authority_epoch,project_id,
                 paired_subject_id,claimed_client_label,actions_json,
                 secret_sha256,issued_at,expires_at,max_operations,
                 pairing_receipt_id,facts_sha256)
               SELECT id,database_uuid,runtime_authority_epoch,project_id,
                      paired_subject_id,claimed_client_label,actions_json,
                      secret_sha256,issued_at,expires_at,max_operations,
                      pairing_receipt_id,facts_sha256
                 FROM agent_project_capabilities_v11_fixture ORDER BY rowid"""
        )
        connection.execute(V5_SCHEMA_STATEMENTS[3])
        connection.execute(
            """INSERT INTO agent_project_capability_events(
                 id,capability_id,project_id,sequence,parent_event_id,
                 parent_event_sha256,event_type,action,operation_id,
                 agent_command_receipt_id,reason_code,presentation_sha256,
                 native_gesture_nonce,event_sha256,created_at)
               SELECT id,capability_id,project_id,sequence,parent_event_id,
                      parent_event_sha256,event_type,action,operation_id,
                      agent_command_receipt_id,reason_code,presentation_sha256,
                      native_gesture_nonce,event_sha256,created_at
                 FROM agent_project_capability_events_v11_fixture
                ORDER BY capability_id,sequence"""
        )
        connection.execute("DROP TABLE agent_project_capability_events_v11_fixture")
        connection.execute("DROP TABLE agent_project_command_receipts")
        connection.execute("DROP TABLE agent_project_capabilities_v11_fixture")
        for index in (8, 9, 10, 11):
            connection.execute(V5_SCHEMA_STATEMENTS[index])
        for index in (14, 15, 18, 19, 20, 21):
            connection.execute(V5_SCHEMA_STATEMENTS[index])
        connection.execute("DELETE FROM schema_migrations WHERE version>=11")
        downgrade_guard = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
        ).fetchone()
        assert downgrade_guard is not None
        connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
        connection.execute(
            "UPDATE database_meta SET schema_version=10 WHERE singleton=1"
        )
        connection.execute(str(downgrade_guard[0]))
        connection.commit()


class TimelineEditV10MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-edit-v10-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_current_schema_preserves_metadata_only_v10_migration(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V10_CHECKSUM,
            "30431939248aefb84a4f62b96c4f6fcf144672679b2bb8c9ba41540cb353a7af",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (SCHEMA_VERSION,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name,checksum FROM schema_migrations WHERE version=10"
                ).fetchone(),
                ("canonical_timeline_persistent_edit", V10_CHECKSUM),
            )

    def test_v9_to_v10_metadata_row_preserves_history_through_current_v18(self) -> None:
        managed_tables = (
            "canonical_timeline_operations",
            "canonical_timeline_revisions",
            "canonical_timeline_heads",
            "canonical_timeline_receipts",
        )
        # V18 intentionally rebuilds revisions to widen schema_version from
        # frozen v1 to the closed v1|v2 union. Heads and desktop receipts remain
        # physically untouched; all four tables must still preserve exact rows.
        physically_unreplaced_tables = managed_tables[2:]
        project_id, _blueprint, _coverage, payload = (
            timeline_fixture.TimelineLoweringIntegrationTests._build_project(self)
        )
        timeline_fixture.TimelineLoweringIntegrationTests._materialize(
            self,
            project_id,
            payload,
            key="timeline-v9-populated-history",
        )
        _downgrade_current_database_to_exact_v10(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            downgrade_guard = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert downgrade_guard is not None
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute("DELETE FROM schema_migrations WHERE version=10")
            connection.execute(
                "UPDATE database_meta SET schema_version=9 WHERE singleton=1"
            )
            connection.execute(str(downgrade_guard[0]))
            before = connection.execute(
                f"SELECT name,rootpage,sql FROM sqlite_schema WHERE name IN "
                f"({','.join('?' for _ in physically_unreplaced_tables)}) ORDER BY name",
                physically_unreplaced_tables,
            ).fetchall()
            historical_rows = {
                table: connection.execute(
                    f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid",
                    (project_id,),
                ).fetchall()
                for table in managed_tables
            }

        self.repository.ensure_schema(self.library)

        with closing(sqlite3.connect(self.db_path)) as connection:
            after = connection.execute(
                f"SELECT name,rootpage,sql FROM sqlite_schema WHERE name IN "
                f"({','.join('?' for _ in physically_unreplaced_tables)}) ORDER BY name",
                physically_unreplaced_tables,
            ).fetchall()
            self.assertEqual(after, before)
            self.assertEqual(
                {
                    table: connection.execute(
                        f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid",
                        (project_id,),
                    ).fetchall()
                    for table in managed_tables
                },
                historical_rows,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (SCHEMA_VERSION,),
            )

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            content,
        )


if __name__ == "__main__":
    unittest.main()
