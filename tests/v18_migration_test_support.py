from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from core.media_db import (
    MediaRepository,
    V7_SCHEMA_STATEMENTS,
    V13_SCHEMA_STATEMENTS,
    V15_SCHEMA_STATEMENTS,
)


_V19_EXPORT_ROOT_TRIGGERS = (
    "trg_output_roots_identity_immutable",
    "trg_user_export_output_roots_no_delete",
)


_V18_REBUILT_TABLES = (
    "agent_project_capability_events",
    "agent_project_command_receipts",
    "canonical_timeline_revisions",
    "canonical_timeline_operations",
    "agent_project_capabilities",
)

_V18_REBUILT_TRIGGERS = (
    "trg_agent_capabilities_no_update",
    "trg_agent_capabilities_no_delete",
    "trg_canonical_timeline_operations_no_update",
    "trg_canonical_timeline_operations_no_delete",
    "trg_canonical_timeline_revisions_no_update",
    "trg_canonical_timeline_revisions_no_delete",
    "trg_agent_project_receipts_no_update",
    "trg_agent_project_receipts_no_delete",
    "trg_agent_capability_events_no_update",
    "trg_agent_capability_events_no_delete",
)

_V18_REBUILT_INDEXES = (
    "idx_agent_capabilities_project_expiry",
    "idx_canonical_timeline_operations_project_sequence",
    "idx_canonical_timeline_revisions_coverage",
    "idx_agent_project_receipts_operation",
    "idx_agent_capability_events_sequence",
    "idx_agent_capability_events_native_gesture",
)


def downgrade_current_v19_to_exact_v18(db_path: Path) -> None:
    """Remove V20 then V19 additions for exact historical migration fixtures."""

    with closing(sqlite3.connect(db_path)) as connection, connection:
        current = connection.execute(
            "SELECT schema_version FROM database_meta WHERE singleton=1"
        ).fetchone()
        if current == (18,):
            return
        if current not in {(19,), (20,)}:
            raise AssertionError(f"expected current V20/V19 fixture, observed {current!r}")
        guard = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='trg_database_meta_no_schema_downgrade'"
        ).fetchone()
        assert guard is not None and isinstance(guard[0], str)
        connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
        if current == (20,):
            for object_type, name in reversed(MediaRepository._V20_SCHEMA_OBJECTS):
                connection.execute(f"DROP {object_type.upper()} {name}")
            connection.execute("DELETE FROM schema_migrations WHERE version=20")
        for name in _V19_EXPORT_ROOT_TRIGGERS:
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DELETE FROM schema_migrations WHERE version=19")
        connection.execute(
            "UPDATE database_meta SET schema_version=18 WHERE singleton=1"
        )
        connection.execute(str(guard[0]))

    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        assert MediaRepository._schema_preflight(connection) == 18
        MediaRepository._verify_v18_physical_schema(connection)


def downgrade_current_v18_to_exact_v17(db_path: Path) -> None:
    """Reverse only V18's five closed-union replacements for test fixtures.

    V17 remains an immutable migration source.  Older migration tests must call
    this helper before removing V17/V16/etc.; otherwise they leave V18 rows and
    physical tables behind while claiming an older managed schema.
    """

    downgrade_current_v19_to_exact_v18(db_path)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        current = connection.execute(
            "SELECT schema_version FROM database_meta WHERE singleton=1"
        ).fetchone()
        if current == (17,):
            return
        if current != (18,):
            raise AssertionError(f"expected current V18 fixture, observed {current!r}")
        guard = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='trg_database_meta_no_schema_downgrade'"
        ).fetchone()
        assert guard is not None and isinstance(guard[0], str)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name in _V18_REBUILT_TRIGGERS:
                connection.execute(f"DROP TRIGGER {name}")
            for name in _V18_REBUILT_INDEXES:
                connection.execute(f"DROP INDEX {name}")
            for name in _V18_REBUILT_TABLES:
                connection.execute(
                    f"ALTER TABLE {name} RENAME TO {name}_v18_fixture"
                )

            connection.execute(V15_SCHEMA_STATEMENTS[0])
            connection.execute(V13_SCHEMA_STATEMENTS[1])
            connection.execute(V7_SCHEMA_STATEMENTS[1])
            connection.execute(V13_SCHEMA_STATEMENTS[2])
            connection.execute(V13_SCHEMA_STATEMENTS[3])

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
                     FROM agent_project_capabilities_v18_fixture ORDER BY rowid"""
            )
            connection.execute(
                """INSERT INTO canonical_timeline_operations(
                     id,project_id,sequence,parent_operation_id,command_type,
                     command_version,effect_class,expected_blueprint_revision,
                     expected_blueprint_content_sha256,
                     expected_blueprint_semantic_sha256,
                     expected_blueprint_operation_id,expected_coverage_revision,
                     expected_coverage_content_sha256,
                     expected_coverage_evidence_manifest_sha256,
                     expected_coverage_operation_id,expected_timeline_head_json,
                     request_sha256,result_revision,result_revision_sha256,
                     result_timeline_content_sha256,result_json,created_at)
                   SELECT id,project_id,sequence,parent_operation_id,command_type,
                          command_version,effect_class,expected_blueprint_revision,
                          expected_blueprint_content_sha256,
                          expected_blueprint_semantic_sha256,
                          expected_blueprint_operation_id,expected_coverage_revision,
                          expected_coverage_content_sha256,
                          expected_coverage_evidence_manifest_sha256,
                          expected_coverage_operation_id,expected_timeline_head_json,
                          request_sha256,result_revision,result_revision_sha256,
                          result_timeline_content_sha256,result_json,created_at
                     FROM canonical_timeline_operations_v18_fixture
                    ORDER BY project_id,sequence"""
            )
            connection.execute(
                """INSERT INTO canonical_timeline_revisions(
                     project_id,revision,parent_revision,parent_revision_sha256,
                     timeline_id,blueprint_revision,blueprint_content_sha256,
                     blueprint_semantic_sha256,blueprint_operation_id,
                     coverage_revision,coverage_content_sha256,
                     coverage_evidence_manifest_sha256,coverage_operation_id,
                     schema_version,compiler_id,timeline_json,timeline_content_sha256,
                     source_bindings_json,source_bindings_sha256,revision_sha256,
                     operation_id,created_at)
                   SELECT project_id,revision,parent_revision,parent_revision_sha256,
                          timeline_id,blueprint_revision,blueprint_content_sha256,
                          blueprint_semantic_sha256,blueprint_operation_id,
                          coverage_revision,coverage_content_sha256,
                          coverage_evidence_manifest_sha256,coverage_operation_id,
                          schema_version,compiler_id,timeline_json,timeline_content_sha256,
                          source_bindings_json,source_bindings_sha256,revision_sha256,
                          operation_id,created_at
                     FROM canonical_timeline_revisions_v18_fixture
                    ORDER BY project_id,revision"""
            )
            connection.execute(
                """INSERT INTO agent_project_command_receipts(
                     id,database_uuid,authenticated_principal,capability_id,
                     paired_subject_id,claimed_client_label,project_id,command_type,
                     command_version,idempotency_key,request_json,request_sha256,
                     actor_json,origin_json,operation_id,response_status,response_json,
                     response_sha256,receipt_sha256,resource_type,resource_id,created_at,
                     receipt_schema_version,request_capture)
                   SELECT id,database_uuid,authenticated_principal,capability_id,
                          paired_subject_id,claimed_client_label,project_id,command_type,
                          command_version,idempotency_key,request_json,request_sha256,
                          actor_json,origin_json,operation_id,response_status,response_json,
                          response_sha256,receipt_sha256,resource_type,resource_id,created_at,
                          receipt_schema_version,request_capture
                     FROM agent_project_command_receipts_v18_fixture ORDER BY rowid"""
            )
            connection.execute(
                """INSERT INTO agent_project_capability_events(
                     id,capability_id,project_id,sequence,parent_event_id,
                     parent_event_sha256,event_schema_version,event_type,action,
                     operation_id,agent_command_receipt_id,
                     agent_project_command_receipt_id,reason_code,presentation_sha256,
                     native_gesture_nonce,event_sha256,created_at)
                   SELECT id,capability_id,project_id,sequence,parent_event_id,
                          parent_event_sha256,event_schema_version,event_type,action,
                          operation_id,agent_command_receipt_id,
                          agent_project_command_receipt_id,reason_code,presentation_sha256,
                          native_gesture_nonce,event_sha256,created_at
                     FROM agent_project_capability_events_v18_fixture
                    ORDER BY capability_id,sequence"""
            )

            for name in _V18_REBUILT_TABLES:
                connection.execute(f"DROP TABLE {name}_v18_fixture")
            for statement in V13_SCHEMA_STATEMENTS[4:]:
                connection.execute(statement)
            for statement in (
                V7_SCHEMA_STATEMENTS[5],
                V7_SCHEMA_STATEMENTS[9],
                V7_SCHEMA_STATEMENTS[10],
            ):
                connection.execute(statement)

            connection.execute("DELETE FROM schema_migrations WHERE version=18")
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=17 WHERE singleton=1"
            )
            connection.execute(str(guard[0]))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if not connection.in_transaction:
                connection.execute("PRAGMA legacy_alter_table=OFF")
                connection.execute("PRAGMA foreign_keys=ON")

    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        assert MediaRepository._schema_preflight(connection) == 17


def table_rows(db_path: Path, table: str) -> tuple[tuple[object, ...], ...]:
    with closing(sqlite3.connect(db_path)) as connection:
        return tuple(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
