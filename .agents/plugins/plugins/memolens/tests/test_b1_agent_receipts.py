from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
REPOSITORY_TESTS = REPOSITORY_ROOT / "tests"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPOSITORY_TESTS))

from backend.src.media.blueprint import BlueprintService  # noqa: E402
from core.db import ImageIndexRepository  # noqa: E402
from core.image_analysis_schema import V14_SCHEMA_STATEMENTS  # noqa: E402
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS  # noqa: E402
from core.media_db import (  # noqa: E402
    MediaRepository,
    SCHEMA_VERSION,
    V11_CHECKSUM,
    V12_CHECKSUM,
    V13_CHECKSUM,
    V13_SCHEMA_STATEMENTS,
    V14_CHECKSUM,
    V15_CHECKSUM,
    V16_CHECKSUM,
    V17_CHECKSUM,
    V18_CHECKSUM,
    V19_CHECKSUM,
    V20_CHECKSUM,
    canonical_json,
)
from tests.v18_migration_test_support import (  # noqa: E402
    downgrade_current_v18_to_exact_v17,
)
import memolens_agent_receipts as receipt_validator  # noqa: E402
from memolens_agent_receipts import validate_agent_receipt_ledger  # noqa: E402
import memolens_agent_credentials as plugin_credentials  # noqa: E402
import memolens_blueprint_authority as plugin_authority  # noqa: E402
from memolens_blueprint_authority import (  # noqa: E402
    validate_exact_managed_schema_manifest,
)
from memolens_contracts import MemoLensError  # noqa: E402
import test_b2b4_paired_timeline as paired_timeline_fixture  # noqa: E402
import test_b2c0_v13_timeline_restore as restore_timeline_fixture  # noqa: E402
import test_timeline_structural_edit_backend as structural_timeline_fixture  # noqa: E402


_EVENT_MATERIAL_KEYS = (
    "id",
    "capability_id",
    "project_id",
    "sequence",
    "parent_event_id",
    "parent_event_sha256",
    "event_type",
    "action",
    "operation_id",
    "agent_command_receipt_id",
    "reason_code",
    "presentation_sha256",
    "native_gesture_nonce",
    "created_at",
)
_DESKTOP_RECEIPT_MATERIAL_KEYS = (
    "database_uuid",
    "authenticated_principal",
    "project_id",
    "command_type",
    "command_version",
    "idempotency_key",
    "request_sha256",
    "operation_id",
    "response_status",
    "response_sha256",
    "resource_type",
    "resource_id",
    "created_at",
)
_AGENT_V1_RECEIPT_MATERIAL_KEYS = (
    "id",
    "database_uuid",
    "capability_id",
    "paired_subject_id",
    "project_id",
    "command_type",
    "command_version",
    "idempotency_key",
    "request_sha256",
    "operation_id",
    "response_status",
    "response_sha256",
    "resource_type",
    "resource_id",
    "created_at",
)


def _event_sha256(row: dict[str, Any]) -> str:
    material = {key: row[key] for key in _EVENT_MATERIAL_KEYS}
    return hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _v2_event_sha256(row: dict[str, Any]) -> str:
    material = {key: row[key] for key in _EVENT_MATERIAL_KEYS}
    material["event_schema_version"] = "2"
    material["agent_project_command_receipt_id"] = row[
        "agent_project_command_receipt_id"
    ]
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


def _generic_receipt_sha256(row: dict[str, Any]) -> str:
    material = {
        "id": row["id"],
        "database_uuid": row["database_uuid"],
        "authenticated_principal": row["authenticated_principal"],
        "capability_id": row["capability_id"],
        "paired_subject_id": row["paired_subject_id"],
        "claimed_client_label": row["claimed_client_label"],
        "project_id": row["project_id"],
        "command_type": row["command_type"],
        "command_version": row["command_version"],
        "idempotency_key": row["idempotency_key"],
        "request": json.loads(row["request_json"]),
        "request_sha256": row["request_sha256"],
        "actor": json.loads(row["actor_json"]),
        "origin": json.loads(row["origin_json"]),
        "operation_id": row["operation_id"],
        "response_status": row["response_status"],
        "response": json.loads(row["response_json"]),
        "response_sha256": row["response_sha256"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "created_at": row["created_at"],
    }
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


def _desktop_receipt_sha256(row: dict[str, Any]) -> str:
    material = {key: row[key] for key in _DESKTOP_RECEIPT_MATERIAL_KEYS}
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


def _semantic(label: str = "one") -> dict[str, Any]:
    return {
        "intent": {
            "goal": f"turn local footage into story {label}",
            "stance": "ordinary memories deserve careful editing",
            "audience": "individual creators",
            "platform": "short-video",
        },
        "script": {"blocks": [{"block_id": "opening", "text": f"opening {label}"}]},
        "direction": {
            "theme": "rediscovery",
            "narrative_arc": "forgotten to found",
            "emotion": "calm",
            "tone": "restrained",
            "pace": "measured",
        },
        "output": {"duration_target_ms": 30_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


class _B1ReceiptFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-b1-receipts-")
        root = Path(self.temporary.name).resolve()
        library = root / "library"
        library.mkdir()
        self.db_path = root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        repository = MediaRepository(self.db_path)
        self.repository = repository
        repository.ensure_schema(library)
        project = repository.create_project(
            "B1 receipt validation",
            {"goal": "legacy"},
            {"source": "test"},
        )
        self.project_id = str(project["id"])
        brief = repository.get_brief(self.project_id, 1)
        assert brief is not None
        service = BlueprintService(repository)
        initial = service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": _semantic(),
            },
            idempotency_key="desktop-initial",
        )
        self.desktop_operation_id = initial.operation_id

        pairing_arguments = {
            "pairing_id": "pair_receipt_test",
            "runtime_authority_epoch": "epoch_receipt_test",
            "project_id": self.project_id,
            "paired_subject_id": "agent_receipt_test",
            "claimed_client_label": "Codex (claimed)",
            "actions": [
                "blueprint.commit_proposal",
                "blueprint.restore_revision",
            ],
            "ttl_seconds": 900,
            "max_operations": 20,
            "pairing_request_sha256": "a" * 64,
        }
        envelope = repository.build_agent_pairing_presentation(**pairing_arguments)
        repository.issue_agent_project_capability(
            capability_id="cap_receipt_test",
            secret_sha256="b" * 64,
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="pair_gesture_receipt_test",
            **pairing_arguments,
        )
        head = repository.get_blueprint_head(self.project_id)
        assert head is not None
        changed = deepcopy(_semantic())
        changed["script"]["blocks"][0]["text"] = "paired edit"
        paired = service.commit_proposal(
            self.project_id,
            {
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": changed,
            },
            idempotency_key="paired-edit",
            paired_capability_id="cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
            presented_secret_sha256="b" * 64,
        )
        self.agent_operation_id = paired.operation_id

    def close(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def mutate(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
        *,
        drop_triggers: tuple[str, ...] = (),
        restore_triggers: bool = True,
        ignore_check_constraints: bool = False,
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            if ignore_check_constraints:
                connection.execute("PRAGMA ignore_check_constraints=ON")
            trigger_sql: list[str] = []
            for trigger in drop_triggers:
                rows = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (trigger,),
                ).fetchall()
                if len(rows) != 1 or not isinstance(rows[0]["sql"], str):
                    raise AssertionError(f"missing fixture trigger: {trigger}")
                trigger_sql.append(str(rows[0]["sql"]))
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute(statement, parameters)
            if restore_triggers:
                for sql in trigger_sql:
                    connection.execute(sql)


class B1AgentReceiptValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _B1ReceiptFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _validate(self) -> dict[str, int | bool]:
        with closing(self.fixture.connection()) as connection:
            return validate_agent_receipt_ledger(
                connection,
                project_id=self.fixture.project_id,
            )

    def _assert_corrupt(self, *, manifest_intact: bool = True) -> None:
        if manifest_intact:
            with closing(self.fixture.connection()) as connection:
                validate_exact_managed_schema_manifest(connection)
        with self.assertRaises(MemoLensError) as raised:
            self._validate()
        self.assertEqual(raised.exception.code, "blueprint_receipt_integrity_error")

    def _downgrade_v17_fixture_to_exact_v16(self) -> None:
        downgrade_current_v18_to_exact_v17(self.fixture.db_path)
        historical_activation_sql = next(
            statement
            for statement in V14_SCHEMA_STATEMENTS
            if "trg_image_generations_activate_clean_only" in statement
        )
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            meta_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            for object_type in ("trigger", "index", "table"):
                for identity in V17_SCHEMA_OBJECTS:
                    if identity[0] == object_type:
                        connection.execute(
                            f'DROP {object_type.upper()} "{identity[1]}"'
                        )
            connection.execute(historical_activation_sql)
            connection.execute("DELETE FROM schema_migrations WHERE version=17")
            connection.execute(
                "UPDATE database_meta SET schema_version=16 WHERE singleton=1"
            )
            connection.execute(meta_trigger_sql)

    def _insert_generic_receipt_attack(
        self,
        *,
        receipt_id: str,
        project_id: str,
        command_type: str,
        operation_id: str,
        resource_type: str,
    ) -> None:
        payload = "{}"
        payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        self.fixture.mutate(
            """INSERT INTO agent_project_command_receipts(
                   id,database_uuid,authenticated_principal,capability_id,
                   paired_subject_id,claimed_client_label,project_id,command_type,
                   command_version,idempotency_key,request_json,request_sha256,
                   actor_json,origin_json,operation_id,response_status,
                   response_json,response_sha256,receipt_sha256,resource_type,
                   resource_id,created_at,receipt_schema_version,request_capture)
                 SELECT ?,database_uuid,'paired_agent',id,paired_subject_id,
                        claimed_client_label,?,?,'1',?, ?,?, ?,?, ?,201,
                        ?,?,'0000000000000000000000000000000000000000000000000000000000000000',
                        ?,'rogue',issued_at,'2','exact_canonical_json'
                   FROM agent_project_capabilities WHERE id='cap_receipt_test'""",
            (
                receipt_id,
                project_id,
                command_type,
                f"key-{receipt_id}",
                payload,
                payload_sha256,
                payload,
                payload,
                operation_id,
                payload,
                payload_sha256,
                resource_type,
            ),
        )

    def test_real_core_desktop_and_agent_receipts_validate_without_disclosure(self) -> None:
        result = self._validate()
        self.assertEqual(
            result,
            {
                "validated": True,
                "operation_count": 2,
                "desktop_operation_count": 1,
                "paired_agent_operation_count": 1,
                "paired_capability_count": 1,
            },
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("receipt_test", serialized)
        self.assertNotIn("response_json", serialized)

    def test_v1_generic_projection_preserves_legacy_digest_and_null_exact_fields(self) -> None:
        with closing(self.fixture.connection()) as connection:
            rows = connection.execute(
                """SELECT * FROM agent_project_command_receipts
                    WHERE project_id=? AND receipt_schema_version='1'""",
                (self.fixture.project_id,),
            ).fetchall()
            legacy = connection.execute(
                """SELECT 1 FROM sqlite_schema
                    WHERE name='agent_blueprint_command_receipts' LIMIT 1"""
            ).fetchone()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(legacy)
        receipt = dict(rows[0])
        self.assertEqual(receipt["request_capture"], "digest_only_legacy")
        for column in (
            "authenticated_principal",
            "claimed_client_label",
            "request_json",
            "actor_json",
            "origin_json",
        ):
            self.assertIsNone(receipt[column])
        material = {
            key: receipt[key] for key in _AGENT_V1_RECEIPT_MATERIAL_KEYS
        }
        self.assertEqual(
            hashlib.sha256(canonical_json(material).encode()).hexdigest(),
            receipt["receipt_sha256"],
        )

    def test_v1_restore_projection_stays_digest_only_and_null_exact_fields(self) -> None:
        head = self.fixture.repository.get_blueprint_head(self.fixture.project_id)
        restore_from = self.fixture.repository.get_blueprint_revision(
            self.fixture.project_id,
            1,
        )
        assert head is not None and restore_from is not None
        restored = BlueprintService(self.fixture.repository).restore_revision(
            self.fixture.project_id,
            {
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "restore_from": {
                    "revision": restore_from["revision"],
                    "content_sha256": restore_from["content_sha256"],
                },
            },
            idempotency_key="paired-restore-digest-only",
            paired_capability_id="cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
            presented_secret_sha256="b" * 64,
        )
        self.assertEqual(restored.response_status, 201)

        with closing(self.fixture.connection()) as connection:
            receipt = dict(
                connection.execute(
                    """SELECT * FROM agent_project_command_receipts
                        WHERE operation_id=? AND receipt_schema_version='1'""",
                    (restored.operation_id,),
                ).fetchone()
            )
            validation = validate_agent_receipt_ledger(
                connection,
                project_id=self.fixture.project_id,
            )

        self.assertEqual(receipt["command_type"], "blueprint.restore_revision")
        self.assertEqual(receipt["request_capture"], "digest_only_legacy")
        for column in (
            "authenticated_principal",
            "claimed_client_label",
            "request_json",
            "actor_json",
            "origin_json",
        ):
            self.assertIsNone(receipt[column])
        material = {
            key: receipt[key] for key in _AGENT_V1_RECEIPT_MATERIAL_KEYS
        }
        self.assertEqual(
            hashlib.sha256(canonical_json(material).encode()).hexdigest(),
            receipt["receipt_sha256"],
        )
        self.assertTrue(validation["validated"])
        self.assertEqual(validation["paired_agent_operation_count"], 2)

    def test_retired_legacy_receipt_table_fails_closed(self) -> None:
        self.fixture.mutate(
            "CREATE TABLE agent_blueprint_command_receipts(id TEXT PRIMARY KEY)"
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v1_discriminator_tamper_fails_closed_without_rehashing_history(self) -> None:
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET request_capture='exact_canonical_json'
                WHERE receipt_schema_version='1'""",
            drop_triggers=("trg_agent_project_receipts_no_update",),
            ignore_check_constraints=True,
        )
        self._assert_corrupt()

    def test_v1_exact_request_field_injection_fails_closed(self) -> None:
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET request_json='{}'
                WHERE receipt_schema_version='1'""",
            drop_triggers=("trg_agent_project_receipts_no_update",),
            ignore_check_constraints=True,
        )
        self._assert_corrupt()

    def test_rehashed_v1_cross_resource_graft_fails_closed(self) -> None:
        with closing(self.fixture.connection()) as connection:
            receipt = dict(
                connection.execute(
                    """SELECT * FROM agent_project_command_receipts
                        WHERE receipt_schema_version='1'"""
                ).fetchone()
            )
        receipt["resource_type"] = "canonical_timeline"
        receipt["receipt_sha256"] = hashlib.sha256(
            canonical_json(
                {
                    key: receipt[key]
                    for key in _AGENT_V1_RECEIPT_MATERIAL_KEYS
                }
            ).encode()
        ).hexdigest()
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET resource_type=?,receipt_sha256=?
                WHERE id=?""",
            (
                receipt["resource_type"],
                receipt["receipt_sha256"],
                receipt["id"],
            ),
            drop_triggers=("trg_agent_project_receipts_no_update",),
            ignore_check_constraints=True,
        )
        self._assert_corrupt()

    def test_rehashed_v1_cross_project_graft_fails_closed(self) -> None:
        graft_project = self.fixture.repository.create_project(
            "Cross-project graft target",
            {"goal": "legacy"},
            {"source": "test"},
        )
        with closing(self.fixture.connection()) as connection:
            receipt = dict(
                connection.execute(
                    """SELECT * FROM agent_project_command_receipts
                        WHERE receipt_schema_version='1'"""
                ).fetchone()
            )
        receipt["project_id"] = str(graft_project["id"])
        receipt["receipt_sha256"] = hashlib.sha256(
            canonical_json(
                {
                    key: receipt[key]
                    for key in _AGENT_V1_RECEIPT_MATERIAL_KEYS
                }
            ).encode()
        ).hexdigest()
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET project_id=?,receipt_sha256=?
                WHERE id=?""",
            (
                receipt["project_id"],
                receipt["receipt_sha256"],
                receipt["id"],
            ),
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        self._assert_corrupt()

    def test_canonical_generic_receipt_without_event_or_operation_fails_closed(self) -> None:
        self._insert_generic_receipt_attack(
            receipt_id="agtcmdrcpt_orphan_timeline",
            project_id=self.fixture.project_id,
            command_type="timeline.apply_edit",
            operation_id="op_orphan_generic",
            resource_type="canonical_timeline",
        )
        self._assert_corrupt()

    def test_real_core_revoked_capability_chain_remains_valid_history(self) -> None:
        envelope = self.fixture.repository.build_agent_capability_revoke_presentation(
            "cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
        )
        self.fixture.repository.revoke_agent_project_capability(
            "cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="revoke_gesture_receipt_test",
        )
        self.assertTrue(self._validate()["validated"])

    def test_revoke_presentation_digest_rehash_tamper_fails_closed(self) -> None:
        envelope = self.fixture.repository.build_agent_capability_revoke_presentation(
            "cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
        )
        self.fixture.repository.revoke_agent_project_capability(
            "cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="revoke_gesture_receipt_test",
        )
        with closing(self.fixture.connection()) as connection:
            row = dict(
                connection.execute(
                    """SELECT * FROM agent_project_capability_events
                        WHERE event_type='revoked'"""
                ).fetchone()
            )
        row["presentation_sha256"] = "f" * 64
        event_sha256 = _event_sha256(row)
        self.fixture.mutate(
            """UPDATE agent_project_capability_events
                  SET presentation_sha256=?,event_sha256=?
                WHERE event_type='revoked'""",
            ("f" * 64, event_sha256),
            drop_triggers=("trg_agent_capability_events_no_update",),
        )
        self._assert_corrupt()

    def test_missing_agent_receipt_fails_closed(self) -> None:
        self.fixture.mutate(
            """DELETE FROM agent_project_command_receipts
                WHERE operation_id=? AND receipt_schema_version='1'""",
            (self.fixture.agent_operation_id,),
            drop_triggers=("trg_agent_project_receipts_no_delete",),
        )
        self._assert_corrupt()

    def test_missing_desktop_receipt_sidecar_fails_closed(self) -> None:
        self.fixture.mutate(
            """DELETE FROM blueprint_desktop_receipt_integrity
                WHERE operation_id=?""",
            (self.fixture.desktop_operation_id,),
            drop_triggers=("trg_blueprint_desktop_receipt_integrity_no_delete",),
        )
        self._assert_corrupt()

    def test_extra_desktop_receipt_sidecar_fails_closed(self) -> None:
        self.fixture.mutate(
            """INSERT INTO blueprint_desktop_receipt_integrity(
                   project_id,operation_id,receipt_sha256) VALUES(?,?,?)""",
            (
                self.fixture.project_id,
                self.fixture.agent_operation_id,
                "e" * 64,
            ),
        )
        self._assert_corrupt()

    def test_desktop_valid_key_rebind_fails_sidecar_digest(self) -> None:
        self.fixture.mutate(
            """UPDATE blueprint_command_receipts
                  SET idempotency_key='another valid desktop key'
                WHERE operation_id=?""",
            (self.fixture.desktop_operation_id,),
            drop_triggers=("trg_blueprint_receipts_no_update",),
        )
        self._assert_corrupt()

    def test_desktop_and_agent_receipt_for_one_operation_fails_closed(self) -> None:
        self.fixture.mutate(
            """INSERT INTO blueprint_command_receipts(
                   database_uuid,authenticated_principal,project_id,command_type,
                   command_version,idempotency_key,request_sha256,operation_id,
                   response_status,response_json,response_sha256,resource_type,
                   resource_id,created_at)
                 SELECT database_uuid,'desktop_app',project_id,command_type,
                        command_version,'forged-desktop-copy',request_sha256,
                        operation_id,response_status,response_json,response_sha256,
                        resource_type,resource_id,created_at
                   FROM agent_project_command_receipts
                  WHERE operation_id=? AND receipt_schema_version='1'""",
            (self.fixture.agent_operation_id,),
        )
        with closing(self.fixture.connection()) as connection:
            row = dict(
                connection.execute(
                    """SELECT * FROM blueprint_command_receipts
                        WHERE operation_id=?""",
                    (self.fixture.agent_operation_id,),
                ).fetchone()
            )
        self.fixture.mutate(
            """INSERT INTO blueprint_desktop_receipt_integrity(
                   project_id,operation_id,receipt_sha256) VALUES(?,?,?)""",
            (
                self.fixture.project_id,
                self.fixture.agent_operation_id,
                _desktop_receipt_sha256(row),
            ),
        )
        self._assert_corrupt()

    def test_agent_actor_capability_binding_tamper_fails_closed(self) -> None:
        forged_actor = {
            "capability_id": "cap_forged",
            "claimed_client_label": "Codex (claimed)",
            "identity_verified": True,
            "kind": "paired_agent_command",
            "paired_subject_id": "agent_receipt_test",
            "user_authority_verified": False,
            "vendor_identity_verified": False,
        }
        self.fixture.mutate(
            "UPDATE creative_blueprint_operations SET actor_json=? WHERE id=?",
            (
                json.dumps(
                    forged_actor,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                self.fixture.agent_operation_id,
            ),
            drop_triggers=("trg_creative_blueprint_operations_no_update",),
        )
        self._assert_corrupt()

    def test_capability_facts_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE agent_project_capabilities SET claimed_client_label='forged'",
            drop_triggers=("trg_agent_capabilities_no_update",),
        )
        self._assert_corrupt()

    def test_pairing_receipt_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            """UPDATE agent_pairing_confirmation_receipts
                  SET pairing_request_sha256=?""",
            ("c" * 64,),
            drop_triggers=("trg_agent_pairing_receipts_no_update",),
        )
        self._assert_corrupt()

    def test_unique_used_event_scope_tamper_fails_closed(self) -> None:
        with closing(self.fixture.connection()) as connection:
            row = dict(
                connection.execute(
                    """SELECT * FROM agent_project_capability_events
                        WHERE event_type='used'"""
                ).fetchone()
            )
        row["action"] = "blueprint.restore_revision"
        self.fixture.mutate(
            """UPDATE agent_project_capability_events
                  SET action='blueprint.restore_revision',event_sha256=?
                WHERE event_type='used'""",
            (_event_sha256(row),),
            drop_triggers=("trg_agent_capability_events_no_update",),
        )
        self._assert_corrupt()

    def test_rehashed_v1_receipt_and_event_operation_substitution_fails_closed(self) -> None:
        with closing(self.fixture.connection()) as connection:
            receipt = dict(
                connection.execute(
                    """SELECT * FROM agent_project_command_receipts
                        WHERE receipt_schema_version='1'"""
                ).fetchone()
            )
            event = dict(
                connection.execute(
                    """SELECT * FROM agent_project_capability_events
                        WHERE event_schema_version='1' AND event_type='used'"""
                ).fetchone()
            )
        receipt["operation_id"] = self.fixture.desktop_operation_id
        receipt_material = {
            key: receipt[key] for key in _AGENT_V1_RECEIPT_MATERIAL_KEYS
        }
        receipt["receipt_sha256"] = hashlib.sha256(
            canonical_json(receipt_material).encode()
        ).hexdigest()
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET operation_id=?,receipt_sha256=?
                WHERE id=?""",
            (
                receipt["operation_id"],
                receipt["receipt_sha256"],
                receipt["id"],
            ),
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        event["operation_id"] = self.fixture.desktop_operation_id
        event["event_sha256"] = _event_sha256(event)
        self.fixture.mutate(
            """UPDATE agent_project_capability_events
                  SET operation_id=?,event_sha256=?
                WHERE id=?""",
            (
                event["operation_id"],
                event["event_sha256"],
                event["id"],
            ),
            drop_triggers=("trg_agent_capability_events_no_update",),
        )
        self._assert_corrupt()

    def test_agent_response_tamper_fails_closed(self) -> None:
        response = '{"tampered":true}'
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET response_json=?,response_sha256=?
                WHERE receipt_schema_version='1'""",
            (response, hashlib.sha256(response.encode()).hexdigest()),
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        self._assert_corrupt()

    def test_oversized_response_is_rejected_before_json_decode(self) -> None:
        oversized = '{"value":"' + ("x" * 1_048_577) + '"}'
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts SET response_json=?
                WHERE receipt_schema_version='1'""",
            (oversized,),
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        original = receipt_validator._strict_receipt_json

        def bounded_decode(raw: object) -> Any:
            if isinstance(raw, str) and len(raw.encode()) > 1_048_576:
                raise AssertionError("oversized JSON reached the decoder")
            return original(raw)

        with mock.patch.object(
            receipt_validator,
            "_strict_receipt_json",
            side_effect=bounded_decode,
        ):
            self._assert_corrupt()

    def test_non_text_response_is_rejected_during_preflight_before_decode(self) -> None:
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts SET response_json=?
                WHERE receipt_schema_version='1'""",
            (sqlite3.Binary(b'{"object":"forged"}'),),
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        with mock.patch.object(
            receipt_validator,
            "_strict_receipt_json",
            side_effect=AssertionError("non-text JSON reached the decoder"),
        ):
            self._assert_corrupt()

    def test_json_preflight_rejects_blob_and_null_storage_classes(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE payloads(project_id TEXT,payload)")
            for value in (sqlite3.Binary(b"{}"), None):
                connection.execute("DELETE FROM payloads")
                connection.execute(
                    "INSERT INTO payloads(project_id,payload) VALUES(?,?)",
                    ("project_preflight", value),
                )
                with self.assertRaises(MemoLensError) as raised:
                    receipt_validator._preflight_json_budget(
                        connection,
                        table="payloads",
                        project_id="project_preflight",
                        columns=("payload",),
                        expected_count=1,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "blueprint_receipt_integrity_error",
                )
        finally:
            connection.close()

    def test_space_idempotency_key_rebind_fails_receipt_digest(self) -> None:
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET idempotency_key='bad key'
                WHERE receipt_schema_version='1'""",
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        self._assert_corrupt()

    def test_core_roundtrip_accepts_256_character_unicode_space_key(self) -> None:
        repository = self.fixture.repository
        service = BlueprintService(repository)
        head = repository.get_blueprint_head(self.fixture.project_id)
        assert head is not None
        expected_head = {
            "revision": head["revision"],
            "content_sha256": head["content_sha256"],
        }
        changed = _semantic("unicode-key")
        normalized = {
            "object": "memolens.blueprint_commit_command",
            "schema_version": "1",
            "project_id": self.fixture.project_id,
            "expected_head": expected_head,
            "initial_legacy_brief": None,
            "source_candidate_sha256": None,
            "semantic": changed,
        }
        request_sha256 = hashlib.sha256(canonical_json(normalized).encode()).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
            command_context: object,
        ) -> tuple[dict[str, object], int, str, str]:
            return service._commit_mutation(
                connection,
                project_id=self.fixture.project_id,
                expected_head=expected_head,
                initial_legacy_brief=None,
                source_candidate_sha256=None,
                semantic=changed,
                request_sha256=request_sha256,
                command_context=command_context,
            )

        key = "键 空" + ("x" * 253)
        self.assertEqual(len(key), 256)
        repository.execute_paired_blueprint_command(
            capability_id="cap_receipt_test",
            runtime_authority_epoch="epoch_receipt_test",
            presented_secret_sha256="b" * 64,
            project_id=self.fixture.project_id,
            command_type="blueprint.commit_proposal",
            command_version="1",
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )
        result = self._validate()
        self.assertEqual(result["paired_agent_operation_count"], 2)
        self.assertTrue(result["validated"])

    def test_valid_idempotency_key_rebind_fails_receipt_digest(self) -> None:
        self.fixture.mutate(
            """UPDATE agent_project_command_receipts
                  SET idempotency_key='another-valid-key'
                WHERE receipt_schema_version='1'""",
            drop_triggers=("trg_agent_project_receipts_no_update",),
        )
        self._assert_corrupt()

    def test_current_plugin_oracle_accepts_only_schema_v20(self) -> None:
        self.assertEqual(receipt_validator.CURRENT_MANAGED_SCHEMA_VERSION, 20)
        self.assertEqual(plugin_authority.CURRENT_MANAGED_SCHEMA_VERSION, SCHEMA_VERSION)
        expected_actions = (
            "blueprint.commit_proposal",
            "blueprint.restore_revision",
            "timeline.apply_edit",
            "timeline.apply_structural_edit",
            "timeline.restore_revision",
            "timeline.preview_media",
        )
        self.assertEqual(plugin_authority._AGENT_ACTIONS, expected_actions)
        self.assertEqual(receipt_validator._CAPABILITY_ACTIONS, expected_actions)
        self.assertEqual(plugin_credentials._ALLOWED_ACTIONS, expected_actions)
        self.assertEqual(plugin_authority._V11_MIGRATIONS[-1][2], V11_CHECKSUM)
        self.assertEqual(plugin_authority._V12_MIGRATIONS[-1][2], V12_CHECKSUM)
        self.assertEqual(plugin_authority._V13_MIGRATIONS[-1][2], V13_CHECKSUM)
        self.assertEqual(plugin_authority._V14_MIGRATIONS[-1][2], V14_CHECKSUM)
        self.assertEqual(plugin_authority._V15_MIGRATIONS[-1][2], V15_CHECKSUM)
        self.assertEqual(plugin_authority._V16_MIGRATIONS[-1][2], V16_CHECKSUM)
        self.assertEqual(
            plugin_authority._V17_MIGRATIONS[-1],
            (17, "canonical_image_read_cutover", V17_CHECKSUM),
        )
        self.assertEqual(
            plugin_authority._V18_MIGRATIONS[-1],
            (18, "canonical_timeline_structural_edit", V18_CHECKSUM),
        )
        self.assertEqual(
            plugin_authority._V19_MIGRATIONS[-1],
            (19, "canonical_export_output_root_anchor", V19_CHECKSUM),
        )
        self.assertEqual(
            plugin_authority._V20_MIGRATIONS[-1],
            (20, "plugin_first_library_bootstrap", V20_CHECKSUM),
        )
        core_v13_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v13_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V13_ONLY_PHYSICAL_SCHEMA_SHA256),
            set(core_v13_hashes),
        )
        for identity, digest in core_v13_hashes.items():
            self.assertEqual(
                plugin_authority._V13_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        core_v14_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v14_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V14_ONLY_PHYSICAL_SCHEMA_SHA256),
            set(core_v14_hashes),
        )
        for identity, digest in core_v14_hashes.items():
            self.assertEqual(
                plugin_authority._V14_ONLY_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
            self.assertEqual(
                plugin_authority._V14_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        core_v15_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v15_physical_schema().items()
        }
        for identity, digest in core_v15_hashes.items():
            self.assertEqual(
                plugin_authority._V15_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        core_v16_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v16_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V16_ONLY_PHYSICAL_SCHEMA_SHA256),
            set(core_v16_hashes),
        )
        for identity, digest in core_v16_hashes.items():
            self.assertEqual(
                plugin_authority._V16_ONLY_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
            self.assertEqual(
                plugin_authority._V16_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        core_v17_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v17_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V17_ONLY_PHYSICAL_SCHEMA_SHA256),
            set(core_v17_hashes),
        )
        for identity, digest in core_v17_hashes.items():
            self.assertEqual(
                plugin_authority._V17_ONLY_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
            self.assertEqual(
                plugin_authority._V17_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        activation_identity = (
            "trigger",
            "trg_image_generations_activate_clean_only",
        )
        self.assertNotEqual(
            plugin_authority._V16_PHYSICAL_SCHEMA_SHA256[activation_identity],
            plugin_authority._V17_PHYSICAL_SCHEMA_SHA256[activation_identity],
        )
        core_v18_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v18_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V18_REBUILT_PHYSICAL_SCHEMA_SHA256),
            {
                ("table", "agent_project_capabilities"),
                ("table", "canonical_timeline_operations"),
                ("table", "canonical_timeline_revisions"),
                ("table", "agent_project_command_receipts"),
                ("table", "agent_project_capability_events"),
            },
        )
        for identity, digest in core_v18_hashes.items():
            self.assertEqual(
                plugin_authority._V18_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        core_v19_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v19_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V19_ONLY_PHYSICAL_SCHEMA_SHA256),
            set(core_v19_hashes),
        )
        for identity, digest in core_v19_hashes.items():
            self.assertEqual(
                plugin_authority._V19_ONLY_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
            self.assertEqual(
                plugin_authority._V19_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        core_v20_hashes = {
            identity: hashlib.sha256(sql.encode()).hexdigest()
            for identity, sql in MediaRepository._expected_v20_physical_schema().items()
        }
        self.assertEqual(
            set(plugin_authority._V20_ONLY_PHYSICAL_SCHEMA_SHA256),
            set(core_v20_hashes),
        )
        for identity, digest in core_v20_hashes.items():
            self.assertEqual(
                plugin_authority._V20_ONLY_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
            self.assertEqual(
                plugin_authority._V20_PHYSICAL_SCHEMA_SHA256[identity],
                digest,
            )
        with closing(self.fixture.connection()) as connection:
            schema_version = connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton=1"
            ).fetchone()[0]
        self.assertEqual(schema_version, 20)
        self.assertTrue(self._validate()["validated"])

    def test_v20_manifest_rejects_unknown_bootstrap_namespace_object(self) -> None:
        self.fixture.mutate(
            "CREATE VIEW library_bootstrap_shadow AS SELECT 1 AS unexpected"
        )
        with closing(self.fixture.connection()) as connection:
            with self.assertRaises(MemoLensError):
                validate_exact_managed_schema_manifest(connection)

    def test_exact_v17_manifest_remains_frozen_but_is_not_current(self) -> None:
        downgrade_current_v18_to_exact_v17(self.fixture.db_path)
        with closing(self.fixture.connection()) as connection:
            plugin_authority.validate_exact_v17_schema_manifest(connection)
            with self.assertRaises(MemoLensError):
                validate_exact_managed_schema_manifest(connection)
        self._assert_corrupt(manifest_intact=False)

    def test_exact_v16_database_keeps_historical_activation_trigger(self) -> None:
        self._downgrade_v17_fixture_to_exact_v16()

        with closing(self.fixture.connection()) as connection:
            plugin_authority.validate_exact_v16_schema_manifest(connection)
            with self.assertRaises(MemoLensError):
                validate_exact_managed_schema_manifest(connection)

    def test_v11_schema_version_is_refused(self) -> None:
        self.fixture.mutate(
            "UPDATE database_meta SET schema_version=11 WHERE singleton=1",
            drop_triggers=("trg_database_meta_no_schema_downgrade",),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v13_schema_version_is_refused_by_current_oracle(self) -> None:
        self.fixture.mutate(
            "UPDATE database_meta SET schema_version=13 WHERE singleton=1",
            drop_triggers=("trg_database_meta_no_schema_downgrade",),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_exact_v13_database_is_legacy_not_current(self) -> None:
        self._downgrade_v17_fixture_to_exact_v16()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            meta_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            for object_type in ("trigger", "index", "table"):
                for identity in plugin_authority._V16_ONLY_PHYSICAL_SCHEMA_SHA256:
                    if identity[0] == object_type:
                        connection.execute(
                            f'DROP {object_type.upper()} "{identity[1]}"'
                        )
            connection.execute("DELETE FROM schema_migrations WHERE version=16")
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
            connection.execute("DELETE FROM schema_migrations WHERE version=15")
            for object_type in ("trigger", "index", "table"):
                for identity in plugin_authority._V14_ONLY_PHYSICAL_SCHEMA_SHA256:
                    if identity[0] == object_type:
                        connection.execute(
                            f'DROP {object_type.upper()} "{identity[1]}"'
                        )
            connection.execute("DELETE FROM schema_migrations WHERE version=14")
            connection.execute(
                "UPDATE database_meta SET schema_version=13 WHERE singleton=1"
            )
            connection.execute(meta_trigger_sql)
        with closing(self.fixture.connection()) as connection:
            plugin_authority.validate_exact_v13_schema_manifest(connection)
            with self.assertRaises(MemoLensError):
                validate_exact_managed_schema_manifest(connection)
        self._assert_corrupt(manifest_intact=False)

    def test_exact_v15_database_is_historical_not_current(self) -> None:
        self._downgrade_v17_fixture_to_exact_v16()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            meta_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            for object_type in ("trigger", "index", "table"):
                for identity in plugin_authority._V16_ONLY_PHYSICAL_SCHEMA_SHA256:
                    if identity[0] == object_type:
                        connection.execute(
                            f'DROP {object_type.upper()} "{identity[1]}"'
                        )
            connection.execute("DELETE FROM schema_migrations WHERE version=16")
            connection.execute(
                "UPDATE database_meta SET schema_version=15 WHERE singleton=1"
            )
            connection.execute(meta_trigger_sql)
        with closing(self.fixture.connection()) as connection:
            plugin_authority.validate_exact_v15_schema_manifest(connection)
            with self.assertRaises(MemoLensError):
                validate_exact_managed_schema_manifest(connection)
        self._assert_corrupt(manifest_intact=False)

    def test_v15_manifest_rejects_v16_provider_namespace_graft(self) -> None:
        self._downgrade_v17_fixture_to_exact_v16()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            meta_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='trigger' AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute("DELETE FROM schema_migrations WHERE version=16")
            connection.execute(
                "UPDATE database_meta SET schema_version=15 WHERE singleton=1"
            )
            connection.execute(meta_trigger_sql)
        with closing(self.fixture.connection()) as connection:
            with self.assertRaises(MemoLensError):
                plugin_authority.validate_exact_v15_schema_manifest(connection)
        self._assert_corrupt(manifest_intact=False)

    def test_future_schema_version_is_not_treated_as_v20(self) -> None:
        self.fixture.mutate("UPDATE database_meta SET schema_version=21 WHERE singleton=1")
        self._assert_corrupt(manifest_intact=False)

    def test_v17_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=17",
            ("6" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v17_replaced_activation_trigger_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_image_generations_activate_clean_only
                 BEFORE UPDATE ON image_projection_generations
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_image_generations_activate_clean_only",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_unknown_v17_read_manifest_namespace_object_fails_closed(self) -> None:
        self.fixture.mutate(
            "CREATE TABLE image_projection_read_unknown(id TEXT PRIMARY KEY)"
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v16_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=16",
            ("7" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v16_physical_trigger_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_provider_egress_grants_no_delete
                 BEFORE DELETE ON provider_egress_grants
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_provider_egress_grants_no_delete",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_unknown_v16_namespace_object_fails_closed(self) -> None:
        self.fixture.mutate(
            "CREATE TABLE provider_egress_unknown_object(id TEXT PRIMARY KEY)"
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v14_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=14",
            ("8" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v14_physical_trigger_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_image_job_bindings_no_update
                 BEFORE UPDATE ON image_analysis_job_bindings
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_image_job_bindings_no_update",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_unknown_v14_namespace_object_fails_closed(self) -> None:
        self.fixture.mutate(
            "CREATE TABLE image_analysis_unknown_object(id TEXT PRIMARY KEY)"
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v13_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=13",
            ("9" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v12_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=12",
            ("f" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v10_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=10",
            ("e" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v8_migration_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            "UPDATE schema_migrations SET checksum=? WHERE version=8",
            ("d" * 64,),
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v5_authority_trigger_manifest_tamper_still_fails_closed_in_v8(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_blueprint_authority_events_no_update
                 BEFORE UPDATE ON blueprint_decision_authority_events
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_blueprint_authority_events_no_update",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v6_coverage_trigger_manifest_tamper_still_fails_closed_in_v8(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_coverage_receipts_no_update
                 BEFORE UPDATE ON coverage_plan_receipts
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_coverage_receipts_no_update",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v7_timeline_trigger_manifest_tamper_still_fails_closed_in_v8(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_canonical_timeline_receipts_no_update
                 BEFORE UPDATE ON canonical_timeline_receipts
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_canonical_timeline_receipts_no_update",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v8_commit_attestation_trigger_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_canonical_export_jobs_commit_requires_attestation
                 BEFORE UPDATE OF status ON canonical_export_jobs
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_canonical_export_jobs_commit_requires_attestation",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)

    def test_v12_generic_receipt_trigger_manifest_tamper_fails_closed(self) -> None:
        self.fixture.mutate(
            """CREATE TRIGGER trg_agent_project_receipts_no_update
                 BEFORE UPDATE ON agent_project_command_receipts
                 BEGIN SELECT 1; END""",
            drop_triggers=("trg_agent_project_receipts_no_update",),
            restore_triggers=False,
        )
        self._assert_corrupt(manifest_intact=False)


class V12TimelineReceiptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = paired_timeline_fixture.B2B4PairedTimelineApiTests(
            methodName="test_pair_save_replay_and_exhaustion_are_exact"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        paired_timeline_fixture._shutdown_app(self.fixture.app)
        self.fixture.repository.close()
        self.fixture.environment.stop()
        self.fixture.temporary.cleanup()

    def _write_mixed_lane_history(
        self,
        *,
        suffix: str,
    ) -> tuple[str, dict[str, object], object]:
        capability_id, _presentation = self.fixture._pair(
            actions=["blueprint.commit_proposal", "timeline.apply_edit"]
        )
        body = self.fixture._edit_body(duration_ms=1_500)
        first = self.fixture._save(
            capability_id,
            body,
            key=f"plugin-v12-v2-{suffix}",
        )
        self.assertEqual(first.status_code, 201, first.json)

        blueprint_read = self.fixture.blueprints.get(self.fixture.project_id).response
        blueprint_document = blueprint_read["blueprint"]
        self.assertIsInstance(blueprint_document, dict)
        semantic = deepcopy(blueprint_document["semantic"])
        semantic["direction"]["tone"] = "restrained but newly mixed"
        blueprint_write = self.fixture.blueprints.commit_proposal(
            self.fixture.project_id,
            {
                "expected_head": {
                    "revision": blueprint_read["revision"],
                    "content_sha256": blueprint_read["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic,
            },
            idempotency_key=f"plugin-v12-v1-{suffix}",
            paired_capability_id=capability_id,
            runtime_authority_epoch=self.fixture.epoch,
            presented_secret_sha256=paired_timeline_fixture.proof_secret_sha256(
                paired_timeline_fixture.SECRET
            ),
        )
        self.assertEqual(blueprint_write.response_status, 201)
        return capability_id, body, first

    def test_v2_exact_timeline_receipt_and_replay_pass_plugin_cold_audit(self) -> None:
        capability_id, body, first = self._write_mixed_lane_history(
            suffix="replay"
        )

        with closing(self.fixture.repository._connect()) as connection:
            before = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (self.fixture.project_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_timeline_operations",
                    "agent_project_command_receipts",
                    "agent_project_capability_events",
                )
            )
            receipt = dict(
                connection.execute(
                    """SELECT * FROM agent_project_command_receipts
                        WHERE project_id=? AND receipt_schema_version='2'""",
                    (self.fixture.project_id,),
                ).fetchone()
            )
            validation = validate_agent_receipt_ledger(
                connection,
                project_id=self.fixture.project_id,
            )
        self.assertEqual(receipt["request_capture"], "exact_canonical_json")
        for column in (
            "authenticated_principal",
            "claimed_client_label",
            "request_json",
            "actor_json",
            "origin_json",
        ):
            self.assertIsNotNone(receipt[column])
        self.assertTrue(validation["validated"])
        self.assertEqual(validation["paired_agent_operation_count"], 1)

        replay = self.fixture._save(
            capability_id,
            body,
            key="plugin-v12-v2-replay",
        )
        self.assertEqual(replay.status_code, 201, replay.json)
        self.assertEqual(replay.json, first.json)
        with closing(self.fixture.repository._connect()) as connection:
            after = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (self.fixture.project_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_timeline_operations",
                    "agent_project_command_receipts",
                    "agent_project_capability_events",
                )
            )
            self.assertTrue(
                validate_agent_receipt_ledger(
                    connection,
                    project_id=self.fixture.project_id,
                )["validated"]
            )
        self.assertEqual(after, before)

    def test_v1_event_cannot_substitute_a_v2_receipt_from_same_capability(self) -> None:
        self._write_mixed_lane_history(suffix="cross-lane")
        with closing(self.fixture.repository._connect()) as connection:
            v2_receipt_id = connection.execute(
                """SELECT id FROM agent_project_command_receipts
                    WHERE project_id=? AND receipt_schema_version='2'""",
                (self.fixture.project_id,),
            ).fetchone()[0]
            event = dict(
                connection.execute(
                    """SELECT * FROM agent_project_capability_events
                        WHERE project_id=? AND event_schema_version='1'
                          AND event_type='used'""",
                    (self.fixture.project_id,),
                ).fetchone()
            )
        event["agent_command_receipt_id"] = v2_receipt_id
        event["event_sha256"] = _event_sha256(event)
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            trigger_sql = connection.execute(
                """SELECT sql FROM sqlite_schema
                    WHERE type='trigger'
                      AND name='trg_agent_capability_events_no_update'"""
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_agent_capability_events_no_update")
            connection.execute(
                """UPDATE agent_project_capability_events
                      SET agent_command_receipt_id=?,event_sha256=?
                    WHERE id=?""",
                (v2_receipt_id, event["event_sha256"], event["id"]),
            )
            connection.execute(trigger_sql)
        with closing(self.fixture.repository._connect()) as connection:
            with self.assertRaises(MemoLensError) as raised:
                validate_agent_receipt_ledger(
                    connection,
                    project_id=self.fixture.project_id,
                )
        self.assertEqual(
            raised.exception.code,
            "blueprint_receipt_integrity_error",
        )


class V18TimelineStructuralReceiptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = structural_timeline_fixture.TimelineStructuralEditBackendTests(
            methodName="test_paired_route_requires_distinct_structural_authority"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _write_structural_edit(
        self,
    ) -> tuple[str, dict[str, object], str]:
        capability_id = self.fixture._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=1,
        )
        body = self.fixture._body()
        response = self.fixture._agent_save(
            capability_id,
            body,
            key="plugin-v18-structural-delete",
        )
        self.assertEqual(response.status_code, 201, response.json)
        return capability_id, body, str(response.json["operation_id"])

    def _validate(self) -> dict[str, int | bool]:
        with closing(self.fixture.repository._connect()) as connection:
            return validate_agent_receipt_ledger(
                connection,
                project_id=self.fixture.project_id,
            )

    def _assert_corrupt(self) -> None:
        with self.assertRaises(MemoLensError) as raised:
            self._validate()
        self.assertEqual(
            raised.exception.code,
            "blueprint_receipt_integrity_error",
        )

    def _rehash_structural_claim(self, operation_id: str, mutate) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            receipt = dict(
                connection.execute(
                    "SELECT * FROM agent_project_command_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            )
            operation = dict(
                connection.execute(
                    "SELECT * FROM canonical_timeline_operations WHERE id=?",
                    (operation_id,),
                ).fetchone()
            )
            request = json.loads(receipt["request_json"])
            result = json.loads(operation["result_json"])
            mutate(request, result)
            response = json.loads(receipt["response_json"])
            response["result"] = deepcopy(result)
            request_json = canonical_json(request)
            result_json = canonical_json(result)
            response_json = canonical_json(response)
            request_sha256 = hashlib.sha256(request_json.encode()).hexdigest()
            response_sha256 = hashlib.sha256(response_json.encode()).hexdigest()
            receipt.update(
                request_json=request_json,
                request_sha256=request_sha256,
                response_json=response_json,
                response_sha256=response_sha256,
            )
            receipt["receipt_sha256"] = _generic_receipt_sha256(receipt)
            trigger_rows = connection.execute(
                """SELECT name,sql FROM sqlite_schema
                    WHERE type='trigger' AND name IN (
                        'trg_canonical_timeline_operations_no_update',
                        'trg_agent_project_receipts_no_update')
                    ORDER BY name"""
            ).fetchall()
            self.assertEqual(len(trigger_rows), 2)
            for row in trigger_rows:
                connection.execute(f"DROP TRIGGER {row['name']}")
            connection.execute(
                """UPDATE canonical_timeline_operations
                      SET request_sha256=?,result_json=? WHERE id=?""",
                (request_sha256, result_json, operation_id),
            )
            connection.execute(
                """UPDATE agent_project_command_receipts
                      SET request_json=?,request_sha256=?,response_json=?,
                          response_sha256=?,receipt_sha256=?
                    WHERE operation_id=?""",
                (
                    request_json,
                    request_sha256,
                    response_json,
                    response_sha256,
                    receipt["receipt_sha256"],
                    operation_id,
                ),
            )
            for row in trigger_rows:
                connection.execute(row["sql"])

    def test_v18_structural_receipt_request_result_and_v2_row_are_exact(self) -> None:
        capability_id, body, operation_id = self._write_structural_edit()
        with closing(self.fixture.repository._connect()) as connection:
            receipt = dict(
                connection.execute(
                    "SELECT * FROM agent_project_command_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            )
            revision = dict(
                connection.execute(
                    """SELECT schema_version,timeline_json
                         FROM canonical_timeline_revisions
                        WHERE project_id=? AND revision=2""",
                    (self.fixture.project_id,),
                ).fetchone()
            )
            event = dict(
                connection.execute(
                    "SELECT * FROM agent_project_capability_events WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            )
        request = json.loads(receipt["request_json"])
        response = json.loads(receipt["response_json"])
        timeline = json.loads(revision["timeline_json"])
        self.assertEqual(
            request,
            {
                "object": "memolens.timeline_apply_structural_edit_command",
                "schema_version": "1",
                "project_id": self.fixture.project_id,
                "expected_database_uuid": self.fixture.repository.database_uuid,
                "expected_blueprint": body["expected_blueprint"],
                "expected_coverage": body["expected_coverage"],
                "expected_timeline_head": body["expected_timeline_head"],
                "structural_edit": body["structural_edit"],
            },
        )
        self.assertEqual(
            response["result"],
            {
                "kind": "revision_created",
                "result_head": response["result"]["result_head"],
                "structural_edit": body["structural_edit"],
            },
        )
        self.assertEqual(
            (receipt["command_type"], event["action"]),
            ("timeline.apply_structural_edit", "timeline.apply_structural_edit"),
        )
        self.assertEqual(receipt["capability_id"], capability_id)
        self.assertEqual((revision["schema_version"], timeline["schema_version"]), ("2", "2"))
        validation = self._validate()
        self.assertTrue(validation["validated"])

    def test_v18_timeline_row_and_document_schema_must_match(self) -> None:
        _capability_id, _body, _operation_id = self._write_structural_edit()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            trigger_sql = connection.execute(
                """SELECT sql FROM sqlite_schema
                    WHERE type='trigger'
                      AND name='trg_canonical_timeline_revisions_no_update'"""
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_canonical_timeline_revisions_no_update")
            connection.execute(
                """UPDATE canonical_timeline_revisions SET schema_version='1'
                    WHERE project_id=? AND revision=2""",
                (self.fixture.project_id,),
            )
            connection.execute(trigger_sql)
        self._assert_corrupt()

    def test_v18_structural_result_and_operation_shapes_remain_closed(self) -> None:
        _capability_id, _body, operation_id = self._write_structural_edit()

        def add_result_alias(_request, result):
            result["edit"] = deepcopy(result["structural_edit"])

        self._rehash_structural_claim(operation_id, add_result_alias)
        self._assert_corrupt()

    def test_v18_structural_edit_rejects_rehashed_extra_fields(self) -> None:
        _capability_id, _body, operation_id = self._write_structural_edit()

        def add_unsupported_offset(request, result):
            request["structural_edit"]["offset_ms"] = 0
            result["structural_edit"]["offset_ms"] = 0

        self._rehash_structural_claim(operation_id, add_unsupported_offset)
        self._assert_corrupt()


class V13TimelineRestoreReceiptParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = restore_timeline_fixture.V13TimelineRestoreCoreTests(
            methodName=(
                "test_paired_restore_replays_exact_k_to_n_plus_one_and_edit_only_denies"
            )
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _advance_to_revision_three(
        self,
        project_id: str,
        current: dict[str, object],
    ) -> dict[str, object]:
        timeline = current["timeline"]
        assert isinstance(timeline, dict)
        clip_id = str(timeline["tracks"][0]["clips"][0]["clip_id"])
        self.fixture.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": current["blueprint_binding"],
                "expected_coverage": current["coverage_binding"],
                "expected_timeline_head": (
                    self.fixture.repository._canonical_timeline_head_projection(
                        current
                    )
                ),
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip_id,
                    "duration_ms": 1_625,
                },
            },
            idempotency_key="plugin-v13-second-edit",
            expected_database_uuid=self.fixture.repository.database_uuid,
        )
        head = self.fixture.repository.get_canonical_timeline_head(project_id)
        assert head is not None and head["revision"] == 3
        return head

    def _write_restore(
        self,
        *,
        current_revision_three: bool = False,
    ) -> tuple[
        str,
        dict[str, object],
        dict[str, object],
        dict[str, object],
        object,
    ]:
        project_id, current, target = self.fixture._materialize_edit()
        if current_revision_three:
            current = self._advance_to_revision_three(project_id, current)
        request = self.fixture._restore_request(
            self.fixture.repository,
            project_id,
            current,
            target,
        )
        request_sha256 = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        capability, secret_sha256 = self.fixture._pair(
            project_id,
            actions=["timeline.restore_revision"],
            max_operations=1,
        )
        first = self.fixture.repository.execute_paired_canonical_timeline_command(
            capability_id=str(capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=secret_sha256,
            project_id=project_id,
            command_type="timeline.restore_revision",
            command_version="1",
            idempotency_key="plugin-v13-restore",
            request_value=request,
            request_sha256=request_sha256,
            mutation=self.fixture._restore_mutation(
                project_id=project_id,
                request=request,
                request_sha256=request_sha256,
            ),
        )
        return project_id, current, target, request, first

    def _validate(self, project_id: str) -> dict[str, int | bool]:
        with closing(self.fixture.repository._connect()) as connection:
            return validate_agent_receipt_ledger(
                connection,
                project_id=project_id,
            )

    def _assert_corrupt(self, project_id: str) -> None:
        with self.assertRaises(MemoLensError) as raised:
            self._validate(project_id)
        self.assertEqual(
            raised.exception.code,
            "blueprint_receipt_integrity_error",
        )

    def _rehash_restore_claim(
        self,
        *,
        project_id: str,
        operation_id: str,
        restore_from: dict[str, object],
        request_object: str = "memolens.timeline_restore_revision_command",
    ) -> None:
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            receipt = dict(
                connection.execute(
                    "SELECT * FROM agent_project_command_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
            )
            operation = dict(
                connection.execute(
                    "SELECT * FROM canonical_timeline_operations WHERE id=?",
                    (operation_id,),
                ).fetchone()
            )
            request = json.loads(receipt["request_json"])
            result = json.loads(operation["result_json"])
            response = json.loads(receipt["response_json"])
            request["object"] = request_object
            request["restore_from"] = deepcopy(restore_from)
            result["restore_from"] = deepcopy(restore_from)
            response["result"] = deepcopy(result)
            request_json = canonical_json(request)
            result_json = canonical_json(result)
            response_json = canonical_json(response)
            request_sha256 = hashlib.sha256(request_json.encode()).hexdigest()
            response_sha256 = hashlib.sha256(response_json.encode()).hexdigest()
            receipt.update(
                request_json=request_json,
                request_sha256=request_sha256,
                response_json=response_json,
                response_sha256=response_sha256,
            )
            receipt["receipt_sha256"] = _generic_receipt_sha256(receipt)
            trigger_rows = connection.execute(
                """SELECT name,sql FROM sqlite_schema
                    WHERE type='trigger' AND name IN (
                        'trg_canonical_timeline_operations_no_update',
                        'trg_agent_project_receipts_no_update')
                    ORDER BY name"""
            ).fetchall()
            self.assertEqual(len(trigger_rows), 2)
            for row in trigger_rows:
                connection.execute(f"DROP TRIGGER {row['name']}")
            connection.execute(
                """UPDATE canonical_timeline_operations
                      SET request_sha256=?,result_json=? WHERE id=?""",
                (request_sha256, result_json, operation_id),
            )
            connection.execute(
                """UPDATE agent_project_command_receipts
                      SET request_json=?,request_sha256=?,response_json=?,
                          response_sha256=?,receipt_sha256=?
                    WHERE operation_id=?""",
                (
                    request_json,
                    request_sha256,
                    response_json,
                    response_sha256,
                    receipt["receipt_sha256"],
                    operation_id,
                ),
            )
            for row in trigger_rows:
                connection.execute(row["sql"])

    def test_explicit_v12_manifest_validator_remains_frozen(self) -> None:
        self.fixture._downgrade_to_exact_v12()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            for object_type, name in reversed(V17_SCHEMA_OBJECTS):
                connection.execute(
                    f'DROP {object_type.upper()} IF EXISTS "{name}"'
                )
            connection.execute("DELETE FROM schema_migrations WHERE version=17")
        with closing(self.fixture.repository._connect()) as connection:
            plugin_authority.validate_exact_v12_schema_manifest(connection)
            plugin_authority.validate_v12_agent_project_receipt_census(
                connection
            )
            with self.assertRaises(MemoLensError):
                validate_exact_managed_schema_manifest(connection)

    def test_restore_receipt_event_and_replay_pass_exact_plugin_audit(self) -> None:
        project_id, _current, _target, request, first = self._write_restore()
        with closing(self.fixture.repository._connect()) as connection:
            before = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_timeline_operations",
                    "agent_project_command_receipts",
                    "agent_project_capability_events",
                )
            )
            receipt = dict(
                connection.execute(
                    "SELECT * FROM agent_project_command_receipts WHERE operation_id=?",
                    (first.operation_id,),
                ).fetchone()
            )
            event = dict(
                connection.execute(
                    "SELECT * FROM agent_project_capability_events WHERE operation_id=?",
                    (first.operation_id,),
                ).fetchone()
            )
        receipt_request = json.loads(receipt["request_json"])
        receipt_response = json.loads(receipt["response_json"])
        self.assertEqual(
            receipt_request["object"],
            "memolens.timeline_restore_revision_command",
        )
        self.assertEqual(receipt_request["restore_from"], request["restore_from"])
        self.assertEqual(
            receipt_response["command_type"],
            "timeline.restore_revision",
        )
        self.assertEqual(
            (event["event_schema_version"], event["action"]),
            ("2", "timeline.restore_revision"),
        )
        validation = self._validate(project_id)
        self.assertTrue(validation["validated"])

        mutation_called = False

        def forbidden_mutation(_connection: sqlite3.Connection):
            nonlocal mutation_called
            mutation_called = True
            raise AssertionError("replay reached mutation")

        capability_id = receipt["capability_id"]
        secret_sha256 = hashlib.sha256(b"secret-1").hexdigest()
        replay = self.fixture.repository.execute_paired_canonical_timeline_command(
            capability_id=capability_id,
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=secret_sha256,
            project_id=project_id,
            command_type="timeline.restore_revision",
            command_version="1",
            idempotency_key="plugin-v13-restore",
            request_value=request,
            request_sha256=hashlib.sha256(canonical_json(request).encode()).hexdigest(),
            mutation=forbidden_mutation,
        )
        self.assertTrue(replay.replayed)
        self.assertFalse(mutation_called)
        with closing(self.fixture.repository._connect()) as connection:
            after = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_timeline_operations",
                    "agent_project_command_receipts",
                    "agent_project_capability_events",
                )
            )
        self.assertEqual(after, before)

    def test_rehashed_restore_event_cannot_claim_edit_action(self) -> None:
        project_id, _current, _target, _request, first = self._write_restore()
        with closing(sqlite3.connect(self.fixture.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            event = dict(
                connection.execute(
                    "SELECT * FROM agent_project_capability_events WHERE operation_id=?",
                    (first.operation_id,),
                ).fetchone()
            )
            event["action"] = "timeline.apply_edit"
            event["event_sha256"] = _v2_event_sha256(event)
            trigger_sql = connection.execute(
                """SELECT sql FROM sqlite_schema
                    WHERE name='trg_agent_capability_events_no_update'"""
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_agent_capability_events_no_update")
            connection.execute(
                """UPDATE agent_project_capability_events
                      SET action=?,event_sha256=? WHERE id=?""",
                (event["action"], event["event_sha256"], event["id"]),
            )
            connection.execute(trigger_sql)
        self._assert_corrupt(project_id)

    def test_rehashed_restore_request_discriminator_fails_closed(self) -> None:
        project_id, _current, _target, request, first = self._write_restore()
        self._rehash_restore_claim(
            project_id=project_id,
            operation_id=first.operation_id,
            restore_from=request["restore_from"],
            request_object="memolens.timeline_apply_edit_command",
        )
        self._assert_corrupt(project_id)

    def test_synchronized_alternate_restore_claim_fails_exact_replay(self) -> None:
        project_id, current, _target, _request, first = self._write_restore(
            current_revision_three=True
        )
        alternate = self.fixture.repository.get_canonical_timeline_revision(
            project_id,
            2,
        )
        assert alternate is not None and current["revision"] == 3
        self._rehash_restore_claim(
            project_id=project_id,
            operation_id=first.operation_id,
            restore_from=(
                self.fixture.repository._canonical_timeline_head_projection(
                    alternate
                )
            ),
        )
        self._assert_corrupt(project_id)

    def test_synchronized_current_head_restore_claim_fails_k_before_n(self) -> None:
        project_id, current, _target, _request, first = self._write_restore()
        self._rehash_restore_claim(
            project_id=project_id,
            operation_id=first.operation_id,
            restore_from=(
                self.fixture.repository._canonical_timeline_head_projection(current)
            ),
        )
        self._assert_corrupt(project_id)


if __name__ == "__main__":
    unittest.main()
