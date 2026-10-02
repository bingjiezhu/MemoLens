from __future__ import annotations

import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from backend.src import (
    MAIN_AUTHORITY_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from backend.src.agent_authority import (
    AGENT_CAPABILITY_HEADER,
    AGENT_NONCE_HEADER,
    AGENT_PROOF_HEADER,
    AGENT_STATUS_PROOF_HEADER,
    agent_request_proof,
    agent_status_proof,
    proof_secret_sha256,
)
from core.config import Settings
from core.image_analysis_schema import V14_SCHEMA_OBJECTS
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
from core.media_db import (
    CanonicalTimelineIntegrityError,
    AgentCapabilityError,
    MediaMigrationError,
    MediaRepository,
    SCHEMA_VERSION,
    V5_SCHEMA_STATEMENTS,
    V9_SCHEMA_STATEMENTS,
    V11_CHECKSUM,
    V11_SCHEMA_STATEMENTS,
    V12_CHECKSUM,
    V12_SCHEMA_STATEMENTS,
    canonical_json,
)
from core.provider_egress_schema import V16_SCHEMA_OBJECTS
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17
try:
    from tests import test_timeline_lowering_api as timeline_fixture
except ImportError:  # unittest discover adds tests/ directly to sys.path.
    import test_timeline_lowering_api as timeline_fixture


SECRET = "b2" * 32


def _shutdown_app(app) -> None:
    shutdown_runtime_extensions(app.extensions)


class B2B4PairedTimelineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-b2b4-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "index.db"
        self.desktop_token = "b2b4-desktop-token"
        self.main_token = "b2b4-main-token"
        self.epoch = "epoch_b2b4_test"
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
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()
        self.repository: MediaRepository = self.app.extensions["media_repository"]
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
            idempotency_key="b2b4-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(materialized.response_status, 201)
        workspace = self.timelines.read(self.project_id)
        self.timeline_head = workspace["head"]
        timeline = workspace["timeline"]
        assert isinstance(self.timeline_head, dict) and isinstance(timeline, dict)
        self.clip_id = str(timeline["tracks"][0]["clips"][0]["clip_id"])

    def tearDown(self) -> None:
        _shutdown_app(self.app)
        self.environment.stop()
        self.temporary.cleanup()
        gc.collect()

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringApiTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        return timeline_fixture.TimelineLoweringApiTests._build_project(self)

    def _pair(
        self,
        *,
        actions: list[str] | None = None,
        max_operations: int = 4,
        subject: str = "codex_b2b4_session",
        secret: str = SECRET,
    ) -> tuple[str, dict[str, object]]:
        blueprint = self.repository.get_blueprint_head(self.project_id)
        assert blueprint is not None
        selected_actions = actions or ["timeline.apply_edit"]
        body = {
            "object": "memolens.agent_pairing_request",
            "schema_version": "1",
            "project_id": self.project_id,
            "observed_head": {
                "revision": blueprint["revision"],
                "content_sha256": blueprint["content_sha256"],
            },
            "subject_id": subject,
            "claimed_client_label": "Codex local editor",
            "proof_secret": secret,
            "actions": selected_actions,
            "ttl_seconds": 900,
            "max_operations": max_operations,
        }
        pairing = self.client.post("/v1/agent/pairings", json=body)
        self.assertEqual(pairing.status_code, 201, pairing.json)
        pairing_id = str(pairing.json["pairing_id"])
        presentation = self.client.get(
            f"/v1/main/agent/pairings/{pairing_id}/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        self._native_nonce_sequence += 1
        approved = self.client.post(
            f"/v1/main/agent/pairings/{pairing_id}/approve",
            json={
                "presentation_sha256": presentation.json["presentation_sha256"],
                "native_gesture_nonce": (
                    f"native_b2b4_pairing_{self._native_nonce_sequence}"
                ),
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(approved.status_code, 201, approved.json)
        return str(approved.json["capability_id"]), presentation.json

    def _nonce(
        self,
        capability_id: str,
        *,
        action: str = "timeline.apply_edit",
        secret: str = SECRET,
    ):
        return self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    secret,
                    kind="nonce",
                    identifier=f"{capability_id}:{action}",
                )
            },
        )

    def _edit_body(self, *, duration_ms: int = 1_400) -> dict[str, object]:
        return {
            "expected_blueprint": self.materialize_payload["expected_blueprint"],
            "expected_coverage": self.materialize_payload["expected_coverage"],
            "expected_timeline_head": self.timeline_head,
            "edit": {
                "op": "set_clip_duration",
                "clip_id": self.clip_id,
                "duration_ms": duration_ms,
            },
        }

    def _save(
        self,
        capability_id: str,
        body: dict[str, object],
        *,
        key: str,
        nonce: str | None = None,
        signed_path: str | None = None,
        actual_path: str | None = None,
        proof_body: dict[str, object] | None = None,
        proof_database_uuid: str | None = None,
        proof_project_id: str | None = None,
        proof_idempotency_key: str | None = None,
    ):
        if nonce is None:
            nonce_response = self._nonce(capability_id)
            self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
            nonce = str(nonce_response.json["nonce"])
        canonical_path = (
            signed_path
            or f"/v1/agent/creative/projects/{self.project_id}/timeline/edit"
        )
        request_path = actual_path or canonical_path
        signed_body = proof_body if proof_body is not None else body
        proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=canonical_path,
            database_uuid=proof_database_uuid or self.repository.database_uuid,
            project_id=proof_project_id or self.project_id,
            body_sha256=hashlib.sha256(canonical_json(signed_body).encode()).hexdigest(),
            idempotency_key=proof_idempotency_key or key,
        )
        return self.client.post(
            request_path,
            json=body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: nonce,
                AGENT_PROOF_HEADER: proof,
                "Idempotency-Key": key,
            },
        )

    def _ledger_counts(self) -> tuple[int, ...]:
        with closing(self.repository._connect()) as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "canonical_timeline_operations",
                    "canonical_timeline_revisions",
                    "canonical_timeline_receipts",
                    "agent_project_command_receipts",
                    "agent_project_capability_events",
                )
            )

    def _insert_generic_receipt_copy_attack(
        self,
        *,
        receipt_id: str,
        project_id: str,
        command_type: str,
        operation_id: str,
        resource_type: str,
        receipt_schema_version: str = "2",
        request_capture: str = "exact_canonical_json",
        ignore_check_constraints: bool = False,
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            if ignore_check_constraints:
                connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                """INSERT INTO agent_project_command_receipts(
                       id,database_uuid,authenticated_principal,capability_id,
                       paired_subject_id,claimed_client_label,project_id,command_type,
                       command_version,idempotency_key,request_json,request_sha256,
                       actor_json,origin_json,operation_id,response_status,
                       response_json,response_sha256,receipt_sha256,resource_type,
                       resource_id,created_at,receipt_schema_version,request_capture)
                     SELECT ?,database_uuid,authenticated_principal,capability_id,
                            paired_subject_id,claimed_client_label,?,?,command_version,
                            ?,request_json,request_sha256,actor_json,origin_json,?,
                            response_status,response_json,response_sha256,
                            '0000000000000000000000000000000000000000000000000000000000000000',
                            ?,'rogue',created_at,?,?
                       FROM agent_project_command_receipts
                      WHERE receipt_schema_version='2' ORDER BY rowid LIMIT 1""",
                (
                    receipt_id,
                    project_id,
                    command_type,
                    f"key-{receipt_id}",
                    operation_id,
                    resource_type,
                    receipt_schema_version,
                    request_capture,
                ),
            )
            connection.commit()

    def _downgrade_current_database_to_exact_v12(self) -> None:
        """Reverse only V13's four closed-union table replacements."""

        downgrade_current_v18_to_exact_v17(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            version = connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton=1"
            ).fetchone()[0]
            if version == 12:
                return
            self.assertEqual(version, 17)
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
                connection.execute(
                    f'DROP {object_type.upper()} IF EXISTS "{name}"'
                )
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

    def _downgrade_current_database_to_exact_v10(self) -> None:
        """Test-only reverse fixture preserving populated legacy V5 authority rows."""

        self._downgrade_current_database_to_exact_v12()

        with closing(sqlite3.connect(self.db_path)) as connection:
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
            downgrade_trigger = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=10 WHERE singleton=1"
            )
            connection.execute(downgrade_trigger)
            connection.commit()

    def _downgrade_current_database_to_exact_v11(self) -> None:
        """Reverse only the V12 convergence for a populated migration oracle."""

        self._downgrade_current_database_to_exact_v12()

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            for name in (
                "trg_agent_project_receipts_no_update",
                "trg_agent_project_receipts_no_delete",
                "trg_agent_capability_events_no_update",
                "trg_agent_capability_events_no_delete",
            ):
                connection.execute(f"DROP TRIGGER {name}")
            for name in (
                "idx_agent_project_receipts_operation",
                "idx_agent_capability_events_sequence",
                "idx_agent_capability_events_native_gesture",
            ):
                connection.execute(f"DROP INDEX {name}")
            connection.execute(
                "ALTER TABLE agent_project_capability_events "
                "RENAME TO agent_project_capability_events_v12_fixture"
            )
            connection.execute(
                "ALTER TABLE agent_project_command_receipts "
                "RENAME TO agent_project_command_receipts_v12_fixture"
            )
            connection.execute(V5_SCHEMA_STATEMENTS[2])
            connection.execute(V11_SCHEMA_STATEMENTS[1])
            connection.execute(V11_SCHEMA_STATEMENTS[2])
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
                     FROM agent_project_command_receipts_v12_fixture
                    WHERE receipt_schema_version='1' ORDER BY rowid"""
            )
            connection.execute(
                """INSERT INTO agent_project_command_receipts(
                     id,database_uuid,authenticated_principal,capability_id,
                     paired_subject_id,claimed_client_label,project_id,command_type,
                     command_version,idempotency_key,request_json,request_sha256,
                     actor_json,origin_json,operation_id,response_status,response_json,
                     response_sha256,receipt_sha256,resource_type,resource_id,created_at)
                   SELECT id,database_uuid,authenticated_principal,capability_id,
                          paired_subject_id,claimed_client_label,project_id,command_type,
                          command_version,idempotency_key,request_json,request_sha256,
                          actor_json,origin_json,operation_id,response_status,response_json,
                          response_sha256,receipt_sha256,resource_type,resource_id,created_at
                     FROM agent_project_command_receipts_v12_fixture
                    WHERE receipt_schema_version='2' ORDER BY rowid"""
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
                     FROM agent_project_capability_events_v12_fixture
                    ORDER BY capability_id,sequence"""
            )
            connection.execute("DROP TABLE agent_project_capability_events_v12_fixture")
            connection.execute("DROP TABLE agent_project_command_receipts_v12_fixture")
            for statement in (
                V5_SCHEMA_STATEMENTS[11],
                V5_SCHEMA_STATEMENTS[18],
                V5_SCHEMA_STATEMENTS[19],
                V11_SCHEMA_STATEMENTS[4],
                V11_SCHEMA_STATEMENTS[5],
                V11_SCHEMA_STATEMENTS[6],
                V11_SCHEMA_STATEMENTS[9],
                V11_SCHEMA_STATEMENTS[10],
                V11_SCHEMA_STATEMENTS[11],
                V11_SCHEMA_STATEMENTS[12],
            ):
                connection.execute(statement)
            connection.execute("DELETE FROM schema_migrations WHERE version=12")
            downgrade_trigger = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=11 WHERE singleton=1"
            )
            connection.execute(downgrade_trigger)
            connection.commit()

    def test_populated_v11_converges_v1_v2_bytes_and_event_sequence(self) -> None:
        capability_id, _presentation = self._pair(
            actions=["blueprint.commit_proposal", "timeline.apply_edit"],
            max_operations=4,
        )
        edited = self._save(
            capability_id,
            self._edit_body(duration_ms=1_550),
            key="v12-timeline-edit",
        )
        self.assertEqual(edited.status_code, 201, edited.json)

        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        convergence_semantic = timeline_fixture.semantic("v12-convergence")
        blueprint_body = {
            "expected_head": {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "d" * 64,
            "semantic": convergence_semantic,
        }
        action = "blueprint.commit_proposal"
        nonce_response = self._nonce(capability_id, action=action)
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        nonce = str(nonce_response.json["nonce"])
        path = f"/v1/agent/creative/projects/{self.project_id}/blueprint/commit"
        key = "v12-blueprint-commit"
        proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(
                canonical_json(blueprint_body).encode()
            ).hexdigest(),
            idempotency_key=key,
        )
        committed = self.client.post(
            path,
            json=blueprint_body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: nonce,
                AGENT_PROOF_HEADER: proof,
                "Idempotency-Key": key,
            },
        )
        self.assertEqual(committed.status_code, 201, committed.json)
        no_change_head = self.repository.get_blueprint_head(self.project_id)
        assert no_change_head is not None
        no_change_body = {
            "expected_head": {
                "revision": no_change_head["revision"],
                "content_sha256": no_change_head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "d" * 64,
            "semantic": convergence_semantic,
        }
        no_change_nonce_response = self._nonce(capability_id, action=action)
        self.assertEqual(
            no_change_nonce_response.status_code,
            201,
            no_change_nonce_response.json,
        )
        no_change_nonce = str(no_change_nonce_response.json["nonce"])
        no_change_key = "v12-blueprint-no-change"
        no_change_proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=no_change_nonce,
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(
                canonical_json(no_change_body).encode()
            ).hexdigest(),
            idempotency_key=no_change_key,
        )
        no_change = self.client.post(
            path,
            json=no_change_body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: no_change_nonce,
                AGENT_PROOF_HEADER: no_change_proof,
                "Idempotency-Key": no_change_key,
            },
        )
        self.assertEqual(no_change.status_code, 200, no_change.json)
        self.assertEqual(no_change.json["result"]["kind"], "no_change")

        v1_projection = (
            "id,database_uuid,capability_id,paired_subject_id,project_id,"
            "command_type,command_version,idempotency_key,request_sha256,operation_id,"
            "response_status,CAST(response_json AS BLOB),response_sha256,receipt_sha256,"
            "resource_type,resource_id,created_at"
        )
        v2_projection = (
            "id,database_uuid,authenticated_principal,capability_id,paired_subject_id,"
            "claimed_client_label,project_id,command_type,command_version,idempotency_key,"
            "CAST(request_json AS BLOB),request_sha256,CAST(actor_json AS BLOB),"
            "CAST(origin_json AS BLOB),operation_id,response_status,"
            "CAST(response_json AS BLOB),response_sha256,receipt_sha256,resource_type,"
            "resource_id,created_at"
        )
        event_projection = (
            "id,capability_id,project_id,sequence,parent_event_id,parent_event_sha256,"
            "event_schema_version,event_type,action,operation_id,agent_command_receipt_id,"
            "agent_project_command_receipt_id,reason_code,presentation_sha256,"
            "native_gesture_nonce,event_sha256,created_at"
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            expected_v1 = tuple(
                tuple(row) for row in connection.execute(
                    f"SELECT {v1_projection} FROM agent_project_command_receipts "
                    "WHERE receipt_schema_version='1' ORDER BY id"
                )
            )
            expected_v2 = tuple(
                tuple(row) for row in connection.execute(
                    f"SELECT {v2_projection} FROM agent_project_command_receipts "
                    "WHERE receipt_schema_version='2' ORDER BY id"
                )
            )
            expected_events = tuple(
                tuple(row) for row in connection.execute(
                    f"SELECT {event_projection} FROM agent_project_capability_events "
                    "ORDER BY capability_id,sequence"
                )
            )
        self.assertEqual(len(expected_v1), 2)
        self.assertEqual(len(expected_v2), 1)
        self.assertEqual(len(expected_events), 4)

        self.repository.close()
        self._downgrade_current_database_to_exact_v11()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            MediaRepository._verify_v5_unreplaced_physical_schema(connection)
            MediaRepository._verify_v11_physical_schema(connection)
            self.assertEqual(
                tuple(
                    tuple(row) for row in connection.execute(
                        f"SELECT {v1_projection} FROM agent_blueprint_command_receipts "
                        "ORDER BY id"
                    )
                ),
                expected_v1,
            )
            self.assertEqual(
                tuple(
                    tuple(row) for row in connection.execute(
                        f"SELECT {v2_projection} FROM agent_project_command_receipts "
                        "ORDER BY id"
                    )
                ),
                expected_v2,
            )

        migrated = MediaRepository(self.db_path)
        migrated.ensure_schema(self.library)
        self.repository = migrated
        self.app.extensions["media_repository"] = migrated
        with closing(migrated._connect()) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE name='agent_blueprint_command_receipts'"
                ).fetchone()
            )
            self.assertEqual(
                tuple(
                    tuple(row) for row in connection.execute(
                        f"SELECT {v1_projection} FROM agent_project_command_receipts "
                        "WHERE receipt_schema_version='1' ORDER BY id"
                    )
                ),
                expected_v1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM agent_project_command_receipts receipt
                         JOIN creative_blueprint_operations operation
                           ON operation.project_id=receipt.project_id
                          AND operation.id=receipt.operation_id
                        WHERE receipt.receipt_schema_version='1'
                          AND receipt.request_capture='digest_only_legacy'
                          AND receipt.authenticated_principal IS NULL
                          AND receipt.claimed_client_label IS NULL
                          AND receipt.request_json IS NULL
                          AND receipt.actor_json IS NULL
                          AND receipt.origin_json IS NULL
                          AND operation.result_kind='no_change'"""
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                tuple(
                    tuple(row) for row in connection.execute(
                        f"SELECT {v2_projection} FROM agent_project_command_receipts "
                        "WHERE receipt_schema_version='2' ORDER BY id"
                    )
                ),
                expected_v2,
            )
            self.assertEqual(
                tuple(
                    tuple(row) for row in connection.execute(
                        f"SELECT {event_projection} FROM agent_project_capability_events "
                        "ORDER BY capability_id,sequence"
                    )
                ),
                expected_events,
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=12"
                    ).fetchone()
                ),
                (
                    "paired_agent_project_command_receipt_convergence",
                    V12_CHECKSUM,
                ),
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        backup_files = list((self.db_path.parent / "migration-backups").glob("*"))
        self.assertEqual(len(backup_files), 2)
        self.assertEqual({path.suffix for path in backup_files}, {".sqlite3", ".json"})

    def test_v12_collision_and_copy_fault_roll_back_exact_v11_source(self) -> None:
        self.repository.close()
        self._downgrade_current_database_to_exact_v11()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "CREATE TABLE agent_blueprint_command_receipts_v11(attacker TEXT)"
            )
            connection.commit()

        collided = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v12_schema_name_collision:agent_blueprint_command_receipts_v11:table",
            ):
                collided.ensure_schema(self.library)
        finally:
            collided.close()
        self.assertEqual(
            list((self.db_path.parent / "migration-backups").glob("*")),
            [],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TABLE agent_blueprint_command_receipts_v11")
            connection.commit()

        faulted = MediaRepository(self.db_path)
        try:
            with patch.object(
                MediaRepository,
                "_assert_v12_projection_equal",
                side_effect=MediaMigrationError("injected_v12_copy_fault"),
            ):
                with self.assertRaisesRegex(
                    MediaMigrationError,
                    "injected_v12_copy_fault",
                ):
                    faulted.ensure_schema(self.library)
        finally:
            faulted.close()

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                11,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=12"
                ).fetchone()[0],
                0,
            )
            for temporary_name in (
                "agent_blueprint_command_receipts_v11",
                "agent_project_command_receipts_v11",
                "agent_project_capability_events_v11",
            ):
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name=?",
                        (temporary_name,),
                    ).fetchone()
                )
            MediaRepository._verify_v5_unreplaced_physical_schema(connection)
            MediaRepository._verify_v11_physical_schema(connection)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        backup_files = list((self.db_path.parent / "migration-backups").glob("*"))
        self.assertEqual(len(backup_files), 2)

        recovered = MediaRepository(self.db_path)
        recovered.ensure_schema(self.library)
        self.repository = recovered
        self.app.extensions["media_repository"] = recovered
        with closing(recovered._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE name='agent_blueprint_command_receipts'"
                ).fetchone()
            )

    def test_v11_tamper_fails_before_v12_backup_or_partial_write(self) -> None:
        capability_id, _presentation = self._pair(max_operations=2)
        saved = self._save(
            capability_id,
            self._edit_body(duration_ms=1_525),
            key="v12-tamper-source",
        )
        self.assertEqual(saved.status_code, 201, saved.json)
        operation_id = str(saved.json["operation_id"])
        self.repository.close()
        self._downgrade_current_database_to_exact_v11()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TRIGGER trg_agent_project_receipts_no_update")
            connection.execute(
                "UPDATE agent_project_command_receipts SET response_json='{}' "
                "WHERE operation_id=?",
                (operation_id,),
            )
            connection.execute(V11_SCHEMA_STATEMENTS[9])
            connection.commit()

        rejected = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v11_agent_authority_audit_failed",
            ):
                rejected.ensure_schema(self.library)
        finally:
            rejected.close()
        self.assertEqual(
            list((self.db_path.parent / "migration-backups").glob("*")),
            [],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (11,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=12"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT response_json FROM agent_project_command_receipts "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone(),
                ("{}",),
            )

    def test_pair_save_replay_and_exhaustion_are_exact(self) -> None:
        blueprint_capability, blueprint_presentation = self._pair(
            actions=["blueprint.commit_proposal"],
            subject="blueprint_only_subject",
            secret="c3" * 32,
        )
        self.assertNotIn(
            "observed_timeline_head",
            blueprint_presentation["presentation"],
        )
        denied_nonce = self._nonce(blueprint_capability, secret="c3" * 32)
        self.assertEqual(denied_nonce.status_code, 403, denied_nonce.json)
        self.assertEqual(denied_nonce.json["code"], "agent_pairing_scope_denied")

        capability_id, presentation = self._pair(max_operations=1)
        observed = presentation["presentation"]["observed_timeline_head"]
        self.assertEqual(observed, self.timeline_head)
        self.assertEqual(
            set(observed),
            {
                "revision",
                "revision_sha256",
                "timeline_id",
                "timeline_content_sha256",
                "blueprint_binding",
                "coverage_binding",
                "operation_id",
            },
        )

        body = self._edit_body()
        first = self._save(capability_id, body, key="paired-timeline-edit-1")
        self.assertEqual(first.status_code, 201, first.json)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(first.json["object"], "canonical_timeline.command_result")
        operation_id = str(first.json["operation_id"])

        with closing(self.repository._connect()) as connection:
            receipt = connection.execute(
                "SELECT * FROM agent_project_command_receipts WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            self.assertIsNotNone(receipt)
            assert receipt is not None
            actor = json.loads(receipt["actor_json"])
            origin = json.loads(receipt["origin_json"])
            self.assertEqual(
                actor,
                {
                    "kind": "paired_agent",
                    "capability_id": capability_id,
                    "paired_subject_id": "codex_b2b4_session",
                    "claimed_client_label": "Codex local editor",
                    "capability_secret_possession_verified": True,
                    "vendor_identity_verified": False,
                    "user_authority_verified": False,
                },
            )
            self.assertEqual(
                origin,
                {"principal": "paired_agent", "surface": "agent_plugin_editor"},
            )
            self.assertEqual(receipt["authenticated_principal"], "paired_agent")
            self.assertEqual(receipt["response_status"], 201)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_timeline_receipts "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()[0],
                0,
            )
            event = connection.execute(
                "SELECT * FROM agent_project_capability_events "
                "WHERE capability_id=? AND operation_id=?",
                (capability_id, operation_id),
            ).fetchone()
            self.assertEqual(event["event_schema_version"], "2")
            self.assertIsNone(event["agent_command_receipt_id"])
            self.assertEqual(event["agent_project_command_receipt_id"], receipt["id"])

        committed_counts = self._ledger_counts()
        replay = self._save(capability_id, body, key="paired-timeline-edit-1")
        self.assertEqual(replay.status_code, 201, replay.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json, first.json)
        self.assertEqual(self._ledger_counts(), committed_counts)

        exhausted = self._save(capability_id, body, key="paired-timeline-edit-2")
        self.assertEqual(exhausted.status_code, 403, exhausted.json)
        self.assertEqual(exhausted.json["code"], "agent_pairing_expired")
        self.assertEqual(self._ledger_counts(), committed_counts)

    def test_proof_substitution_and_receipt_tamper_fail_closed(self) -> None:
        capability_id, _presentation = self._pair()
        body = self._edit_body()
        nonce_response = self._nonce(capability_id)
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        nonce = str(nonce_response.json["nonce"])
        before = self._ledger_counts()

        wrong_body = self._save(
            capability_id,
            body,
            key="paired-proof-mismatch",
            nonce=nonce,
            proof_body={**body, "edit": {**body["edit"], "duration_ms": 1_401}},
        )
        self.assertEqual(wrong_body.status_code, 401, wrong_body.json)
        self.assertEqual(wrong_body.json["code"], "agent_pairing_proof_invalid")
        self.assertEqual(self._ledger_counts(), before)

        saved = self._save(
            capability_id,
            body,
            key="paired-proof-mismatch",
            nonce=nonce,
        )
        self.assertEqual(saved.status_code, 201, saved.json)

        update_trigger = next(
            statement
            for statement in V11_SCHEMA_STATEMENTS
            if "CREATE TRIGGER trg_agent_project_receipts_no_update" in statement
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TRIGGER trg_agent_project_receipts_no_update")
            connection.execute(
                "UPDATE agent_project_command_receipts SET actor_json=?",
                (canonical_json({"kind": "desktop_app"}),),
            )
            connection.execute(update_trigger)
            connection.commit()
        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaises(CanonicalTimelineIntegrityError):
                cold.get_canonical_timeline_head(self.project_id)
        finally:
            cold.close()

    def test_closed_union_ddl_rejects_unknown_generic_branch(self) -> None:
        capability_id, _presentation = self._pair()
        saved = self._save(
            capability_id,
            self._edit_body(),
            key="paired-census-rogue-source",
        )
        self.assertEqual(saved.status_code, 201, saved.json)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_generic_receipt_copy_attack(
                receipt_id="agtcmdrcpt_rogue_resource",
                project_id="project_outside_requested_census",
                command_type="rogue.command",
                operation_id="op_rogue_generic",
                resource_type="rogue_resource",
            )
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_command_receipts "
                    "WHERE id='agtcmdrcpt_rogue_resource'"
                ).fetchone()[0],
                0,
            )

    def test_cold_census_rejects_check_bypassed_unknown_discriminator(self) -> None:
        capability_id, _presentation = self._pair()
        saved = self._save(
            capability_id,
            self._edit_body(),
            key="paired-census-unknown-discriminator-source",
        )
        self.assertEqual(saved.status_code, 201, saved.json)
        self._insert_generic_receipt_copy_attack(
            receipt_id="agtcmdrcpt_unknown_discriminator",
            project_id="project_outside_requested_census",
            command_type="rogue.command",
            operation_id="op_unknown_discriminator",
            resource_type="rogue_resource",
            receipt_schema_version="3",
            ignore_check_constraints=True,
        )

        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalTimelineIntegrityError,
                "agent_project_command_receipt_census_invalid",
            ):
                cold.get_canonical_timeline_head(self.project_id)
        finally:
            cold.close()

    def test_cold_census_rejects_check_bypassed_capture_branch_mismatch(self) -> None:
        capability_id, _presentation = self._pair()
        saved = self._save(
            capability_id,
            self._edit_body(),
            key="paired-census-capture-mismatch-source",
        )
        self.assertEqual(saved.status_code, 201, saved.json)
        self._insert_generic_receipt_copy_attack(
            receipt_id="agtcmdrcpt_capture_mismatch",
            project_id=self.project_id,
            command_type="timeline.apply_edit",
            operation_id="op_capture_mismatch",
            resource_type="canonical_timeline",
            receipt_schema_version="2",
            request_capture="digest_only_legacy",
            ignore_check_constraints=True,
        )

        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalTimelineIntegrityError,
                "agent_project_command_receipt_census_invalid",
            ):
                cold.get_canonical_timeline_head(self.project_id)
        finally:
            cold.close()

    def test_cold_census_rejects_canonical_generic_orphan(self) -> None:
        capability_id, _presentation = self._pair()
        saved = self._save(
            capability_id,
            self._edit_body(),
            key="paired-census-orphan-source",
        )
        self.assertEqual(saved.status_code, 201, saved.json)
        self._insert_generic_receipt_copy_attack(
            receipt_id="agtcmdrcpt_orphan_timeline",
            project_id=self.project_id,
            command_type="timeline.apply_edit",
            operation_id="op_orphan_generic",
            resource_type="canonical_timeline",
        )

        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalTimelineIntegrityError,
                "agent_project_command_receipt_census_invalid",
            ):
                cold.get_canonical_timeline_head(self.project_id)
        finally:
            cold.close()

    def test_cold_census_rejects_event_operation_substitution(self) -> None:
        capability_id, _presentation = self._pair()
        saved = self._save(
            capability_id,
            self._edit_body(),
            key="paired-census-event-substitution",
        )
        self.assertEqual(saved.status_code, 201, saved.json)
        operation_id = str(saved.json["operation_id"])
        update_trigger = next(
            statement
            for statement in V11_SCHEMA_STATEMENTS
            if "CREATE TRIGGER trg_agent_capability_events_no_update" in statement
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            event = dict(
                connection.execute(
                    """SELECT * FROM agent_project_capability_events
                        WHERE capability_id=? AND operation_id=?""",
                    (capability_id, operation_id),
                ).fetchone()
            )
            substituted_operation_id = "op_substituted_generic"
            material = MediaRepository._capability_event_material(
                event_id=event["id"],
                capability_id=event["capability_id"],
                project_id=event["project_id"],
                sequence=event["sequence"],
                parent_event_id=event["parent_event_id"],
                parent_event_sha256=event["parent_event_sha256"],
                event_type=event["event_type"],
                action=event["action"],
                operation_id=substituted_operation_id,
                agent_command_receipt_id=event["agent_command_receipt_id"],
                agent_project_command_receipt_id=event[
                    "agent_project_command_receipt_id"
                ],
                event_schema_version=event["event_schema_version"],
                reason_code=event["reason_code"],
                presentation_sha256=event["presentation_sha256"],
                native_gesture_nonce=event["native_gesture_nonce"],
                created_at=event["created_at"],
            )
            connection.execute("DROP TRIGGER trg_agent_capability_events_no_update")
            connection.execute(
                """UPDATE agent_project_capability_events
                      SET operation_id=?,event_sha256=? WHERE id=?""",
                (
                    substituted_operation_id,
                    hashlib.sha256(canonical_json(material).encode()).hexdigest(),
                    event["id"],
                ),
            )
            connection.execute(update_trigger)
            connection.commit()

        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalTimelineIntegrityError,
                "agent_project_command_receipt_census_invalid",
            ):
                cold.get_canonical_timeline_head(self.project_id)
        finally:
            cold.close()

    def test_all_proof_scope_dimensions_and_stale_bindings_write_zero_rows(self) -> None:
        capability_id, _presentation = self._pair(max_operations=8)
        body = self._edit_body()
        path = f"/v1/agent/creative/projects/{self.project_id}/timeline/edit"
        baseline = self._ledger_counts()

        cases = (
            {
                "key": "wrong-proof-db",
                "proof_database_uuid": "00000000-0000-4000-8000-000000000000",
                "expected": {401},
            },
            {
                "key": "wrong-proof-path",
                "signed_path": (
                    f"/v1/agent/creative/projects/{self.project_id}/blueprint/commit"
                ),
                "actual_path": path,
                "expected": {401},
            },
            {
                "key": "wrong-proof-key",
                "proof_idempotency_key": "different-signed-key",
                "expected": {401},
            },
            {
                "key": "wrong-proof-project",
                "signed_path": "/v1/agent/creative/projects/other_project/timeline/edit",
                "actual_path": "/v1/agent/creative/projects/other_project/timeline/edit",
                "proof_project_id": self.project_id,
                "expected": {403},
            },
            {
                "key": "wrong-proof-action",
                "signed_path": (
                    f"/v1/agent/creative/projects/{self.project_id}/blueprint/commit"
                ),
                "actual_path": (
                    f"/v1/agent/creative/projects/{self.project_id}/blueprint/commit"
                ),
                "expected": {403},
            },
        )
        for case in cases:
            nonce_response = self._nonce(capability_id)
            self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
            arguments = {
                name: value for name, value in case.items() if name != "expected"
            }
            response = self._save(
                capability_id,
                body,
                nonce=str(nonce_response.json["nonce"]),
                **arguments,
            )
            self.assertIn(response.status_code, case["expected"], response.json)
            self.assertEqual(self._ledger_counts(), baseline)

        stale_variants = (
            (
                "stale-timeline",
                {
                    **body,
                    "expected_timeline_head": {
                        **body["expected_timeline_head"],
                        "revision_sha256": "f" * 64,
                    },
                },
                "timeline_head_conflict",
            ),
            (
                "stale-blueprint",
                {
                    **body,
                    "expected_blueprint": {
                        **body["expected_blueprint"],
                        "content_sha256": "f" * 64,
                    },
                },
                "blueprint_head_conflict",
            ),
            (
                "stale-coverage",
                {
                    **body,
                    "expected_coverage": {
                        **body["expected_coverage"],
                        "content_sha256": "f" * 64,
                    },
                },
                "coverage_head_conflict",
            ),
        )
        for key, stale_body, code in stale_variants:
            response = self._save(capability_id, stale_body, key=key)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(response.json["code"], code)
            self.assertEqual(self._ledger_counts(), baseline)

    def test_concurrent_exact_cas_has_one_winner_and_no_partial_rows(self) -> None:
        capability_id, _presentation = self._pair(max_operations=4)
        path = f"/v1/agent/creative/projects/{self.project_id}/timeline/edit"
        requests: list[tuple[dict[str, object], dict[str, str]]] = []
        for index, duration_ms in enumerate((1_450, 1_550), start=1):
            body = self._edit_body(duration_ms=duration_ms)
            key = f"paired-cas-{index}"
            nonce_response = self._nonce(capability_id)
            self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
            nonce = str(nonce_response.json["nonce"])
            proof = agent_request_proof(
                SECRET,
                capability_id=capability_id,
                nonce=nonce,
                method="POST",
                canonical_path=path,
                database_uuid=self.repository.database_uuid,
                project_id=self.project_id,
                body_sha256=hashlib.sha256(canonical_json(body).encode()).hexdigest(),
                idempotency_key=key,
            )
            requests.append(
                (
                    body,
                    {
                        AGENT_CAPABILITY_HEADER: capability_id,
                        AGENT_NONCE_HEADER: nonce,
                        AGENT_PROOF_HEADER: proof,
                        "Idempotency-Key": key,
                    },
                )
            )
        before = self._ledger_counts()

        def post(item: tuple[dict[str, object], dict[str, str]]):
            body, headers = item
            with self.app.test_client() as client:
                response = client.post(path, json=body, headers=headers)
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(post, requests))

        self.assertEqual(sorted(status for status, _body in results), [201, 409])
        loser = next(body for status, body in results if status == 409)
        self.assertEqual(loser["code"], "timeline_head_conflict")
        after = self._ledger_counts()
        self.assertEqual(after[0] - before[0], 1)
        self.assertEqual(after[1] - before[1], 1)
        self.assertEqual(after[2] - before[2], 0)
        self.assertEqual(after[3] - before[3], 1)
        self.assertEqual(after[4] - before[4], 1)

    def test_populated_v10_migration_preserves_legacy_rows_and_creates_backup(self) -> None:
        legacy_secret = "d4" * 32
        capability_id, _presentation = self._pair(
            actions=["blueprint.commit_proposal"],
            subject="legacy_v10_subject",
            secret=legacy_secret,
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        body = {
            "expected_head": {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "e" * 64,
            "semantic": timeline_fixture.semantic("b2b4-v10-preservation"),
        }
        action = "blueprint.commit_proposal"
        nonce_response = self._nonce(
            capability_id,
            action=action,
            secret=legacy_secret,
        )
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        path = f"/v1/agent/creative/projects/{self.project_id}/blueprint/commit"
        key = "b2b4-v10-blueprint-use"
        nonce = str(nonce_response.json["nonce"])
        proof = agent_request_proof(
            legacy_secret,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(canonical_json(body).encode()).hexdigest(),
            idempotency_key=key,
        )
        committed = self.client.post(
            path,
            json=body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: nonce,
                AGENT_PROOF_HEADER: proof,
                "Idempotency-Key": key,
            },
        )
        self.assertEqual(committed.status_code, 201, committed.json)

        capability_columns = (
            "id,database_uuid,runtime_authority_epoch,project_id,paired_subject_id,"
            "claimed_client_label,actions_json,secret_sha256,issued_at,expires_at,"
            "max_operations,pairing_receipt_id,facts_sha256"
        )
        event_columns = (
            "id,capability_id,project_id,sequence,parent_event_id,parent_event_sha256,"
            "event_type,action,operation_id,agent_command_receipt_id,reason_code,"
            "presentation_sha256,native_gesture_nonce,event_sha256,created_at"
        )
        receipt_columns = (
            "id,database_uuid,capability_id,paired_subject_id,project_id,"
            "command_type,command_version,idempotency_key,request_sha256,operation_id,"
            "response_status,response_json,response_sha256,receipt_sha256,resource_type,"
            "resource_id,created_at"
        )
        with closing(self.repository._connect()) as connection:
            legacy_snapshot = {
                "capability": tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {capability_columns} FROM agent_project_capabilities "
                        "ORDER BY id"
                    )
                ),
                "pairing": tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM agent_pairing_confirmation_receipts ORDER BY id"
                    )
                ),
                "receipt": tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {receipt_columns} FROM agent_project_command_receipts "
                        "WHERE receipt_schema_version='1' ORDER BY id"
                    )
                ),
                "event": tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {event_columns} FROM agent_project_capability_events "
                        "ORDER BY capability_id,sequence"
                    )
                ),
            }
        self.assertTrue(legacy_snapshot["receipt"])
        self.assertEqual(len(legacy_snapshot["event"]), 2)

        self.repository.close()
        self._downgrade_current_database_to_exact_v10()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                10,
            )
            MediaRepository._verify_v5_physical_schema(connection)

        migrated = MediaRepository(self.db_path)
        migrated.ensure_schema(self.library)
        self.repository = migrated
        self.app.extensions["media_repository"] = migrated
        with closing(migrated._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=11"
                    ).fetchone()
                ),
                ("paired_agent_canonical_timeline_edit", V11_CHECKSUM),
            )
            observed = {
                "capability": tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {capability_columns} FROM agent_project_capabilities "
                        "ORDER BY id"
                    )
                ),
                "pairing": tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM agent_pairing_confirmation_receipts ORDER BY id"
                    )
                ),
                "receipt": tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {receipt_columns} FROM agent_project_command_receipts "
                        "WHERE receipt_schema_version='1' ORDER BY id"
                    )
                ),
                "event": tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {event_columns} FROM agent_project_capability_events "
                        "ORDER BY capability_id,sequence"
                    )
                ),
            }
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_command_receipts "
                    "WHERE receipt_schema_version='2'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(observed, legacy_snapshot)
        backup_files = list((self.db_path.parent / "migration-backups").glob("*"))
        self.assertEqual(len(backup_files), 2)
        self.assertEqual(
            {path.suffix for path in backup_files},
            {".sqlite3", ".json"},
        )

    def test_future_schema_and_v11_name_collision_fail_without_partial_migration(self) -> None:
        self.repository.close()
        self._downgrade_current_database_to_exact_v10()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "CREATE TABLE agent_project_command_receipts(attacker_value TEXT)"
            )
            connection.commit()
        collided = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v11_schema_name_collision:agent_project_command_receipts:table",
            ):
                collided.ensure_schema(self.library)
        finally:
            collided.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                10,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=11"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT sql FROM sqlite_schema "
                    "WHERE type='table' AND name='agent_project_command_receipts'"
                ).fetchone()[0],
                "CREATE TABLE agent_project_command_receipts(attacker_value TEXT)",
            )

        # A future metadata version is refused before any managed DDL.
        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            future_version = SCHEMA_VERSION + 1
            connection.execute(
                "UPDATE database_meta SET schema_version=? WHERE singleton=1",
                (future_version,),
            )
            connection.execute(trigger_sql)
            connection.commit()
        future = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                f"future_schema_version:{SCHEMA_VERSION + 1}",
            ):
                future.ensure_schema(self.library)
        finally:
            future.close()

    def test_revoked_and_expired_capabilities_cannot_start_timeline_writes(self) -> None:
        capability_id, _presentation = self._pair(max_operations=4)
        revoke_envelope = self.client.get(
            f"/v1/main/agent/capabilities/{capability_id}/revoke/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoke_envelope.status_code, 200, revoke_envelope.json)
        revoked = self.client.post(
            f"/v1/main/agent/capabilities/{capability_id}/revoke",
            json={
                "presentation": revoke_envelope.json["presentation"],
                "presentation_sha256": revoke_envelope.json["presentation_sha256"],
                "native_gesture_nonce": "native_b2b4_revoke_1",
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoked.status_code, 200, revoked.json)
        self.assertEqual(revoked.json["status"], "revoked")
        after_revoke = self._ledger_counts()
        denied_nonce = self._nonce(capability_id)
        self.assertEqual(denied_nonce.status_code, 403, denied_nonce.json)
        self.assertEqual(denied_nonce.json["code"], "agent_pairing_revoked")
        with self.assertRaises(AgentCapabilityError) as revoked_error:
            self.timelines.apply_paired_edit(
                self.project_id,
                self._edit_body(),
                idempotency_key="revoked-direct-edit",
                expected_database_uuid=self.repository.database_uuid,
                paired_capability_id=capability_id,
                runtime_authority_epoch=self.epoch,
                presented_secret_sha256=proof_secret_sha256(SECRET),
            )
        self.assertEqual(revoked_error.exception.code, "agent_pairing_revoked")
        self.assertEqual(self._ledger_counts(), after_revoke)

        expired_secret = "f5" * 32
        pairing_request_sha256 = "a" * 64
        pairing_id = "pair_expired_b2b4"
        expired_capability_id = "cap_expired_b2b4"
        envelope = self.repository.build_agent_pairing_presentation(
            pairing_id=pairing_id,
            runtime_authority_epoch=self.epoch,
            project_id=self.project_id,
            paired_subject_id="expired_b2b4_subject",
            claimed_client_label="Expired local editor",
            actions=["timeline.apply_edit"],
            ttl_seconds=60,
            max_operations=2,
            pairing_request_sha256=pairing_request_sha256,
        )
        issued_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.repository.issue_agent_project_capability(
            capability_id=expired_capability_id,
            pairing_id=pairing_id,
            runtime_authority_epoch=self.epoch,
            project_id=self.project_id,
            paired_subject_id="expired_b2b4_subject",
            claimed_client_label="Expired local editor",
            actions=["timeline.apply_edit"],
            secret_sha256=proof_secret_sha256(expired_secret),
            ttl_seconds=60,
            max_operations=2,
            pairing_request_sha256=pairing_request_sha256,
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="native_b2b4_expired_1",
            issued_at=issued_at,
        )
        expired_baseline = self._ledger_counts()
        with self.assertRaises(AgentCapabilityError) as expired_error:
            self.timelines.apply_paired_edit(
                self.project_id,
                self._edit_body(),
                idempotency_key="expired-direct-edit",
                expected_database_uuid=self.repository.database_uuid,
                paired_capability_id=expired_capability_id,
                runtime_authority_epoch=self.epoch,
                presented_secret_sha256=proof_secret_sha256(expired_secret),
            )
        self.assertEqual(expired_error.exception.code, "agent_pairing_expired")
        self.assertEqual(self._ledger_counts(), expired_baseline)


if __name__ == "__main__":
    unittest.main()
