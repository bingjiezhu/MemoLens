from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from backend.src.media.blueprint import BlueprintService
from core.db import ImageIndexRepository
from core.image_analysis_schema import V14_SCHEMA_OBJECTS
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
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
    _V5_SCHEMA_OBJECTS,
    V6_CHECKSUM,
    _V6_SCHEMA_OBJECTS,
    _V7_SCHEMA_OBJECTS,
    _V8_SCHEMA_OBJECTS,
    _V11_SCHEMA_OBJECTS,
    _V13_SCHEMA_OBJECTS,
    V12_SCHEMA_STATEMENTS,
    AgentCapabilityError,
    AgentCapabilityReceiptConflictError,
    BlueprintIntegrityError,
    MediaMigrationError,
    MediaRepository,
    canonical_json,
    content_sha256,
    utc_now_iso,
)
from core.provider_egress_schema import V16_SCHEMA_OBJECTS
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17


def semantic(label: str = "one") -> dict[str, object]:
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


def initialize_v4(root: Path) -> tuple[MediaRepository, Path, Path]:
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
        now = utc_now_iso()
        connection.execute(
            """INSERT INTO database_meta(
                 singleton,database_uuid,schema_version,created_at,updated_at)
               VALUES(1,'database_v4_fixture',4,?,?)""",
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


def initialize_v5(root: Path) -> tuple[MediaRepository, Path, Path]:
    repository, library, db_path = initialize_v4(root)
    connection = repository._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in V5_SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._verify_v5_physical_schema(connection)
        repository._migration(
            connection,
            5,
            "paired_agent_decision_authority",
            V5_CHECKSUM,
        )
        connection.execute("UPDATE database_meta SET schema_version=5 WHERE singleton=1")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return repository, library, db_path


class B1CoreLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-b1-core-")
        self.root = Path(self.temporary.name).resolve()
        self._owned_repositories: list[MediaRepository] = []

    def tearDown(self) -> None:
        for repository in reversed(self._owned_repositories):
            repository.close()
        self.temporary.cleanup()

    def _create_blueprint_project(
        self,
    ) -> tuple[MediaRepository, BlueprintService, str, dict[str, object]]:
        library = self.root / "library"
        library.mkdir()
        db_path = self.root / "state" / "media.db"
        db_path.parent.mkdir()
        ImageIndexRepository(db_path).ensure_schema()
        repository = MediaRepository(db_path)
        repository.ensure_schema(library)
        self._owned_repositories.append(repository)
        project = repository.create_project("B1", {"goal": "legacy"}, {"source": "test"})
        project_id = str(project["id"])
        brief = repository.get_brief(project_id, 1)
        assert brief is not None
        service = BlueprintService(repository)
        service.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": semantic(),
            },
            idempotency_key="initial",
        )
        return repository, service, project_id, semantic()

    def _pair(
        self,
        repository: MediaRepository,
        project_id: str,
        *,
        max_operations: int = 20,
    ) -> dict[str, object]:
        arguments = {
            "pairing_id": "pair_one",
            "runtime_authority_epoch": "epoch_one",
            "project_id": project_id,
            "paired_subject_id": "agent_one",
            "claimed_client_label": "Codex (claimed)",
            "actions": ["blueprint.commit_proposal", "blueprint.restore_revision"],
            "ttl_seconds": 900,
            "max_operations": max_operations,
            "pairing_request_sha256": "a" * 64,
        }
        envelope = repository.build_agent_pairing_presentation(**arguments)
        issued = repository.issue_agent_project_capability(
            capability_id="cap_one",
            secret_sha256="b" * 64,
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="pair_gesture_one",
            **arguments,
        )
        return issued.capability

    def test_v4_upgrade_keeps_v4_to_v5_backup_and_reaches_current_schema(self) -> None:
        repository, library, db_path = initialize_v4(self.root)
        project = repository.create_project("before v5", {"goal": "keep"}, {})

        repository.ensure_schema(library)

        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V4_CHECKSUM,
            "dfc3227eff26b8c8b2fde38269a9549fe890c13a364a517f740959a95b528d39",
        )
        self.assertEqual(
            V5_CHECKSUM,
            "5889c2f79a36aeed8937e4d8f31994767cd56ae193d8bf27dc636906abc72526",
        )
        self.assertEqual(repository.database_uuid, "database_v4_fixture")
        self.assertIsNotNone(repository.get_project(str(project["id"])))
        backup_dir = db_path.parent / "migration-backups"
        manifest_path = next(backup_dir.glob("media-v4-to-v5-*.manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = backup_dir / manifest["backup_filename"]
        self.assertEqual(manifest["from_schema_version"], 4)
        self.assertEqual(manifest["to_schema_version"], 5)
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(backup_path.read_bytes()).hexdigest(), manifest["backup_sha256"])
        with closing(sqlite3.connect(backup_path)) as backup, backup:
            self.assertEqual(backup.execute("SELECT schema_version FROM database_meta").fetchone(), (4,))
        with closing(sqlite3.connect(db_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                (SCHEMA_VERSION,),
            )
            for table in (
                "agent_project_capabilities",
                "agent_pairing_confirmation_receipts",
                "agent_project_capability_events",
                "agent_project_command_receipts",
                "blueprint_decision_authority_events",
                "blueprint_decision_authority_receipts",
            ):
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND tbl_name=?",
                        (table,),
                    ).fetchone()[0],
                    2,
                )

    def test_v5_to_v6_backup_is_verified_and_recoverable(self) -> None:
        repository, library, db_path = initialize_v5(self.root)
        project = repository.create_project("before v6", {"goal": "keep"}, {})

        repository.ensure_schema(library)

        self.assertEqual(
            V6_CHECKSUM,
            "cddb91190311db0f5593db6af5788d54f51c2d82385cad4ea6067da51461716e",
        )
        backup_dir = db_path.parent / "migration-backups"
        manifest_path = next(backup_dir.glob("media-v5-to-v6-*.manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = backup_dir / manifest["backup_filename"]
        self.assertEqual(manifest["database_uuid"], "database_v4_fixture")
        self.assertEqual(manifest["from_schema_version"], 5)
        self.assertEqual(manifest["to_schema_version"], 6)
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
                (5,),
            )
            self.assertEqual(
                backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone(),
                (5,),
            )
            self.assertEqual(
                backup.execute(
                    "SELECT title FROM creative_projects WHERE id=?",
                    (project["id"],),
                ).fetchone(),
                ("before v6",),
            )

        with closing(sqlite3.connect(db_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                (SCHEMA_VERSION,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name,checksum FROM schema_migrations WHERE version=6"
                ).fetchone(),
                ("canonical_coverage_plan_baseline", V6_CHECKSUM),
            )
            for object_type, name in _V6_SCHEMA_OBJECTS:
                self.assertEqual(
                    connection.execute(
                        "SELECT type,name FROM sqlite_schema WHERE type=? AND name=?",
                        (object_type, name),
                    ).fetchone(),
                    (object_type, name),
                )

        restored_root = self.root / "restored"
        restored_library = restored_root / "library"
        restored_library.mkdir(parents=True)
        restored_db = restored_root / "state" / "media.db"
        restored_db.parent.mkdir(parents=True)
        restored_db.write_bytes(backup_path.read_bytes())
        restored_repository = MediaRepository(restored_db)
        restored_repository.ensure_schema(restored_library)
        self.assertEqual(restored_repository.database_uuid, "database_v4_fixture")
        self.assertIsNotNone(restored_repository.get_project(str(project["id"])))

    def test_v4_receipts_are_sealed_during_v5_migration_and_never_self_healed(self) -> None:
        repository, service, project_id, first = self._create_blueprint_project()
        brief = repository.get_brief(project_id, 1)
        assert brief is not None
        initial_payload = {
            "expected_head": None,
            "initial_legacy_brief": {
                "revision": 1,
                "content_sha256": brief["content_sha256"],
            },
            "source_candidate_sha256": None,
            "semantic": first,
        }
        library = self.root / "library"
        downgrade_current_v18_to_exact_v17(repository.db_path)
        with closing(sqlite3.connect(repository.db_path)) as connection, connection:
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
            for object_type, name in reversed(_V13_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            for object_type, name in reversed(_V11_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            connection.execute(
                "DELETE FROM schema_migrations "
                "WHERE version IN (9,10,11,12,13,14,15,16,17)"
            )
            for object_type, name in reversed(_V8_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            for object_type, name in reversed(_V7_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            for object_type, name in reversed(_V6_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            for object_type, name in reversed(_V5_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            connection.execute("DELETE FROM schema_migrations WHERE version=8")
            connection.execute("DELETE FROM schema_migrations WHERE version=7")
            connection.execute("DELETE FROM schema_migrations WHERE version=6")
            connection.execute("DELETE FROM schema_migrations WHERE version=5")
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute("UPDATE database_meta SET schema_version=4")
            downgrade_guard = next(
                statement
                for statement in V4_SCHEMA_STATEMENTS
                if "trg_database_meta_no_schema_downgrade" in statement
            )
            connection.execute(downgrade_guard)

        repository.ensure_schema(library)
        with closing(sqlite3.connect(repository.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM blueprint_command_receipts").fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_desktop_receipt_integrity"
                ).fetchone(),
                (1,),
            )

        replay = service.commit_proposal(
            project_id,
            initial_payload,
            idempotency_key="initial",
        )
        self.assertTrue(replay.replayed)
        head = repository.get_blueprint_head(project_id)
        assert head is not None
        changed = deepcopy(first)
        changed["script"]["blocks"][0]["text"] = "after migration"  # type: ignore[index]
        created = service.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": changed,
            },
            idempotency_key="after-migration",
        )
        self.assertFalse(created.replayed)

        with closing(sqlite3.connect(repository.db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_blueprint_desktop_receipt_integrity_no_delete"
            )
            connection.execute(
                "DELETE FROM blueprint_desktop_receipt_integrity WHERE operation_id=?",
                (created.operation_id,),
            )
            delete_guard = next(
                statement
                for statement in V5_SCHEMA_STATEMENTS
                if "trg_blueprint_desktop_receipt_integrity_no_delete" in statement
            )
            connection.execute(delete_guard)
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_desktop_receipt_integrity_incomplete",
        ):
            repository.get_blueprint_head(project_id)
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_desktop_receipt_integrity_incomplete",
        ):
            repository.get_blueprint_authority_projection(project_id)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v5_desktop_receipt_integrity_incomplete",
        ):
            repository.ensure_schema(library)

    def test_v5_name_collision_and_physical_corruption_fail_before_mutation(self) -> None:
        collision = self.root / "collision"
        collision.mkdir()
        repository, library, db_path = initialize_v4(collision)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("CREATE TABLE agent_project_capabilities(fake TEXT)")
        with self.assertRaisesRegex(MediaMigrationError, "v5_schema_name_collision"):
            repository.ensure_schema(library)
        self.assertFalse((db_path.parent / "migration-backups").exists())

        damaged = self.root / "damaged"
        damaged.mkdir()
        repository, library, db_path = initialize_v4(damaged)
        repository.ensure_schema(library)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_agent_capabilities_no_update")
            connection.execute(
                """CREATE TRIGGER trg_agent_capabilities_no_update
                   BEFORE UPDATE ON agent_project_capabilities BEGIN SELECT 1; END"""
            )
        with self.assertRaisesRegex(MediaMigrationError, "v5_physical_schema_mismatch"):
            repository.ensure_schema(library)

    def test_pair_issue_replay_revoke_and_secret_non_disclosure(self) -> None:
        repository, _service, project_id, _semantic = self._create_blueprint_project()
        capability = self._pair(repository, project_id)
        self.assertEqual(capability["status"], "active")
        self.assertNotIn("secret", canonical_json(capability))

        arguments = {
            "pairing_id": "pair_one",
            "runtime_authority_epoch": "epoch_one",
            "project_id": project_id,
            "paired_subject_id": "agent_one",
            "claimed_client_label": "Codex (claimed)",
            "actions": ["blueprint.commit_proposal", "blueprint.restore_revision"],
            "ttl_seconds": 900,
            "max_operations": 20,
            "pairing_request_sha256": "a" * 64,
        }
        envelope = repository.build_agent_pairing_presentation(**arguments)
        replay = repository.issue_agent_project_capability(
            capability_id="cap_one",
            secret_sha256="b" * 64,
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="pair_gesture_one",
            **arguments,
        )
        self.assertTrue(replay.replayed)
        with self.assertRaises(AgentCapabilityReceiptConflictError):
            repository.issue_agent_project_capability(
                capability_id="cap_one",
                secret_sha256="c" * 64,
                presentation=envelope["presentation"],
                presentation_sha256=str(envelope["presentation_sha256"]),
                native_gesture_nonce="pair_gesture_one",
                **arguments,
            )
        revoke = repository.build_agent_capability_revoke_presentation(
            "cap_one",
            runtime_authority_epoch="epoch_one",
        )
        revoked = repository.revoke_agent_project_capability(
            "cap_one",
            runtime_authority_epoch="epoch_one",
            presentation=revoke["presentation"],
            presentation_sha256=str(revoke["presentation_sha256"]),
            native_gesture_nonce="revoke_gesture_one",
        )
        self.assertEqual(revoked["status"], "revoked")

    def test_actionable_capability_list_excludes_more_than_ui_limit_of_old_epochs(self) -> None:
        repository, _service, project_id, _semantic = self._create_blueprint_project()

        def issue(index: int, epoch: str) -> str:
            suffix = f"{index:03d}"
            capability_id = f"cap_lifecycle_{suffix}"
            arguments = {
                "pairing_id": f"pair_lifecycle_{suffix}",
                "runtime_authority_epoch": epoch,
                "project_id": project_id,
                "paired_subject_id": "agent_lifecycle",
                "claimed_client_label": "Codex (claimed)",
                "actions": ["blueprint.commit_proposal"],
                "ttl_seconds": 900,
                "max_operations": 1,
                "pairing_request_sha256": hashlib.sha256(
                    f"pairing-{suffix}".encode()
                ).hexdigest(),
            }
            envelope = repository.build_agent_pairing_presentation(**arguments)
            repository.issue_agent_project_capability(
                capability_id=capability_id,
                secret_sha256=hashlib.sha256(f"secret-{suffix}".encode()).hexdigest(),
                presentation=envelope["presentation"],
                presentation_sha256=str(envelope["presentation_sha256"]),
                native_gesture_nonce=f"gesture_lifecycle_{suffix}",
                **arguments,
            )
            return capability_id

        for index in range(257):
            issue(index, "epoch_historical")
        current_id = issue(257, "epoch_current")

        actionable = repository.list_agent_project_capabilities(
            runtime_authority_epoch="epoch_current",
        )
        self.assertEqual([row["id"] for row in actionable], [current_id])
        self.assertTrue(all(row["status"] == "active" for row in actionable))
        with closing(repository._connect()) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_capabilities"
                ).fetchone()[0],
                258,
            )

    def test_paired_write_replay_actor_receipt_and_definitive_secret_check(self) -> None:
        repository, service, project_id, first = self._create_blueprint_project()
        self._pair(repository, project_id, max_operations=2)
        head = repository.get_blueprint_head(project_id)
        assert head is not None
        changed = deepcopy(first)
        changed["script"]["blocks"][0]["text"] = "paired edit"  # type: ignore[index]
        payload = {
            "expected_head": {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": None,
            "semantic": changed,
        }
        result = service.commit_proposal(
            project_id,
            payload,
            idempotency_key="paired-one",
            paired_capability_id="cap_one",
            runtime_authority_epoch="epoch_one",
            presented_secret_sha256="b" * 64,
        )
        replay = service.commit_proposal(
            project_id,
            payload,
            idempotency_key="paired-one",
            paired_capability_id="cap_one",
            runtime_authority_epoch="epoch_one",
            presented_secret_sha256="b" * 64,
        )
        self.assertFalse(result.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(
            repository.get_agent_project_capability(
                "cap_one", runtime_authority_epoch="epoch_one"
            )["operations_used"],
            1,
        )
        with repository.transaction() as connection:
            operation = repository.get_blueprint_operation_in_transaction(
                connection,
                result.operation_id,
                project_id=project_id,
            )
            assert operation is not None
            self.assertEqual(operation["actor"]["kind"], "paired_agent_command")
            self.assertFalse(operation["actor"]["vendor_identity_verified"])
            self.assertFalse(operation["actor"]["user_authority_verified"])
            self.assertEqual(operation["origin"], {"principal": "paired_agent", "surface": "agent_cli"})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blueprint_command_receipts WHERE operation_id=?", (result.operation_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_project_command_receipts WHERE operation_id=? AND receipt_schema_version='1'", (result.operation_id,)).fetchone()[0], 1)
        with self.assertRaisesRegex(AgentCapabilityError, "agent_pairing_proof_invalid"):
            service.commit_proposal(
                project_id,
                payload,
                idempotency_key="wrong-secret",
                paired_capability_id="cap_one",
                runtime_authority_epoch="epoch_one",
                presented_secret_sha256="c" * 64,
            )
        with closing(repository._connect()) as connection, connection:
            connection.execute("DROP TRIGGER trg_agent_project_receipts_no_update")
            connection.execute(
                "UPDATE agent_project_command_receipts "
                "SET idempotency_key='paired-rebound' WHERE operation_id=?",
                (result.operation_id,),
            )
            connection.execute(V12_SCHEMA_STATEMENTS[5])
            connection.commit()
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "agent_blueprint_receipt_digest_mismatch",
        ):
            repository.get_blueprint_head(project_id)

    def test_agent_receipt_body_tamper_blocks_head_and_authority_reads(self) -> None:
        repository, service, project_id, first = self._create_blueprint_project()
        self._pair(repository, project_id, max_operations=2)
        head = repository.get_blueprint_head(project_id)
        assert head is not None
        changed = deepcopy(first)
        changed["script"]["blocks"][0]["text"] = "paired body proof"  # type: ignore[index]
        result = service.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": changed,
            },
            idempotency_key="paired-body",
            paired_capability_id="cap_one",
            runtime_authority_epoch="epoch_one",
            presented_secret_sha256="b" * 64,
        )
        with closing(sqlite3.connect(repository.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_agent_project_receipts_no_update")
            connection.execute(
                "UPDATE agent_project_command_receipts SET response_json='{}' "
                "WHERE operation_id=?",
                (result.operation_id,),
            )
            connection.execute(V12_SCHEMA_STATEMENTS[5])
            connection.commit()
        for read in (
            lambda: repository.get_blueprint_head(project_id),
            lambda: repository.get_blueprint_authority_projection(project_id),
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "agent_blueprint_receipt_response_digest_mismatch",
            ):
                read()

    def test_unused_capability_fact_tamper_blocks_all_blueprint_reads(self) -> None:
        repository, _service, project_id, _first = self._create_blueprint_project()
        self._pair(repository, project_id, max_operations=2)
        with closing(sqlite3.connect(repository.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_agent_capabilities_no_update")
            connection.execute(
                "UPDATE agent_project_capabilities SET claimed_client_label='forged client' "
                "WHERE id='cap_one'"
            )
        for read in (
            lambda: repository.get_blueprint_head(project_id),
            lambda: repository.get_blueprint_authority_projection(project_id),
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "agent_capability_facts_digest_mismatch",
            ):
                read()

    def test_pairing_presentation_rebinding_blocks_all_blueprint_reads(self) -> None:
        repository, _service, project_id, _first = self._create_blueprint_project()
        self._pair(repository, project_id, max_operations=2)
        with closing(repository._connect()) as connection, connection:
            receipt_row = connection.execute(
                "SELECT * FROM agent_pairing_confirmation_receipts WHERE capability_id='cap_one'"
            ).fetchone()
            assert receipt_row is not None
            presentation = json.loads(str(receipt_row["presentation_json"]))
            presentation["claimed_client_label"] = "forged client"
            presentation_sha256 = content_sha256(presentation)
            receipt_material = repository._pairing_receipt_material(
                receipt_id=str(receipt_row["id"]),
                database_uuid=str(receipt_row["database_uuid"]),
                runtime_authority_epoch=str(receipt_row["runtime_authority_epoch"]),
                pairing_id=str(receipt_row["pairing_id"]),
                capability_id=str(receipt_row["capability_id"]),
                project_id=str(receipt_row["project_id"]),
                pairing_request_sha256=str(receipt_row["pairing_request_sha256"]),
                observed_revision=int(receipt_row["observed_revision"]),
                observed_content_sha256=str(receipt_row["observed_content_sha256"]),
                presentation_sha256=presentation_sha256,
                native_gesture_nonce=str(receipt_row["native_gesture_nonce"]),
                created_at=str(receipt_row["created_at"]),
            )
            connection.execute("DROP TRIGGER trg_agent_pairing_receipts_no_update")
            connection.execute(
                """UPDATE agent_pairing_confirmation_receipts
                      SET presentation_json=?,presentation_sha256=?,receipt_sha256=?
                    WHERE capability_id='cap_one'""",
                (
                    canonical_json(presentation),
                    presentation_sha256,
                    content_sha256(receipt_material),
                ),
            )
        for read in (
            lambda: repository.get_agent_project_capability(
                "cap_one",
                runtime_authority_epoch="epoch_one",
            ),
            lambda: repository.get_blueprint_head(project_id),
            lambda: repository.get_blueprint_authority_projection(project_id),
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "agent_pairing_receipt_invalid",
            ):
                read()

    def test_revoke_presentation_digest_rebinding_blocks_all_blueprint_reads(self) -> None:
        repository, _service, project_id, _first = self._create_blueprint_project()
        self._pair(repository, project_id, max_operations=2)
        revoke = repository.build_agent_capability_revoke_presentation(
            "cap_one",
            runtime_authority_epoch="epoch_one",
        )
        repository.revoke_agent_project_capability(
            "cap_one",
            runtime_authority_epoch="epoch_one",
            presentation=revoke["presentation"],
            presentation_sha256=str(revoke["presentation_sha256"]),
            native_gesture_nonce="revoke_digest_rebind",
        )
        with closing(repository._connect()) as connection, connection:
            event_row = connection.execute(
                """SELECT * FROM agent_project_capability_events
                    WHERE capability_id='cap_one' AND event_type='revoked'"""
            ).fetchone()
            assert event_row is not None
            rebound_presentation_sha256 = "d" * 64
            event_material = repository._capability_event_material(
                event_id=str(event_row["id"]),
                capability_id=str(event_row["capability_id"]),
                project_id=str(event_row["project_id"]),
                sequence=int(event_row["sequence"]),
                parent_event_id=str(event_row["parent_event_id"]),
                parent_event_sha256=str(event_row["parent_event_sha256"]),
                event_type=str(event_row["event_type"]),
                action=None,
                operation_id=None,
                agent_command_receipt_id=None,
                reason_code=str(event_row["reason_code"]),
                presentation_sha256=rebound_presentation_sha256,
                native_gesture_nonce=str(event_row["native_gesture_nonce"]),
                created_at=str(event_row["created_at"]),
            )
            connection.execute("DROP TRIGGER trg_agent_capability_events_no_update")
            connection.execute(
                """UPDATE agent_project_capability_events
                      SET presentation_sha256=?,event_sha256=?
                    WHERE id=?""",
                (
                    rebound_presentation_sha256,
                    content_sha256(event_material),
                    event_row["id"],
                ),
            )
        for read in (
            lambda: repository.get_agent_project_capability(
                "cap_one",
                runtime_authority_epoch="epoch_one",
            ),
            lambda: repository.get_blueprint_head(project_id),
            lambda: repository.get_blueprint_authority_projection(project_id),
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "agent_capability_revoke_event_invalid",
            ):
                read()

    def test_authority_direct_parent_continuity_revoke_and_restore_non_resurrection(self) -> None:
        repository, service, project_id, first = self._create_blueprint_project()
        confirm = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="confirm",
            decision_units=["script", "output"],
            runtime_authority_epoch="epoch_one",
        )
        repository.confirm_blueprint_decision_units(
            project_id,
            runtime_authority_epoch="epoch_one",
            presentation=confirm["presentation"],
            presentation_sha256=str(confirm["presentation_sha256"]),
            native_gesture_nonce="confirm_gesture_one",
            idempotency_key="confirm-one",
        )
        revision_one = repository.get_blueprint_head(project_id)
        assert revision_one is not None
        changed = deepcopy(first)
        changed["script"]["blocks"][0]["text"] = "changed script"  # type: ignore[index]
        service.commit_proposal(
            project_id,
            {
                "expected_head": {"revision": 1, "content_sha256": revision_one["content_sha256"]},
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": changed,
            },
            idempotency_key="change-script",
        )
        projection = repository.get_blueprint_authority_projection(project_id)
        self.assertFalse(projection["decision_units"]["script"]["confirmed"])
        self.assertTrue(projection["decision_units"]["output"]["confirmed"])
        revision_two = repository.get_blueprint_head(project_id)
        assert revision_two is not None
        service.restore_revision(
            project_id,
            {
                "expected_head": {"revision": 2, "content_sha256": revision_two["content_sha256"]},
                "restore_from": {"revision": 1, "content_sha256": revision_one["content_sha256"]},
            },
            idempotency_key="restore-one",
        )
        restored = repository.get_blueprint_authority_projection(project_id)
        self.assertFalse(restored["decision_units"]["script"]["confirmed"])
        self.assertTrue(restored["decision_units"]["output"]["confirmed"])
        historical = repository.get_blueprint_authority_projection(project_id, revision=1)
        self.assertTrue(historical["decision_units"]["script"]["confirmed"])

        revoke = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="revoke",
            decision_units=["output"],
            runtime_authority_epoch="epoch_one",
        )
        repository.revoke_blueprint_decision_units(
            project_id,
            runtime_authority_epoch="epoch_one",
            presentation=revoke["presentation"],
            presentation_sha256=str(revoke["presentation_sha256"]),
            native_gesture_nonce="revoke_authority_one",
            idempotency_key="revoke-output",
        )
        self.assertEqual(repository.get_blueprint_authority_projection(project_id)["confirmed_decision_unit_count"], 0)

    def test_authority_receipt_and_exact_presentation_corruption_fail_closed(self) -> None:
        repository, _service, project_id, _first = self._create_blueprint_project()
        confirm = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="confirm",
            decision_units=["script"],
            runtime_authority_epoch="epoch_one",
        )
        repository.confirm_blueprint_decision_units(
            project_id,
            runtime_authority_epoch="epoch_one",
            presentation=confirm["presentation"],
            presentation_sha256=str(confirm["presentation_sha256"]),
            native_gesture_nonce="confirm_corrupt_one",
            idempotency_key="confirm-corrupt",
        )
        with closing(repository._connect()) as connection, connection:
            connection.execute("DROP TRIGGER trg_blueprint_authority_receipts_no_update")
            connection.execute(
                "UPDATE blueprint_decision_authority_receipts SET response_json='{}'"
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_authority_receipt"):
            repository.get_blueprint_authority_projection(project_id)

        # Restore the frozen response by recreating this test in a separate database,
        # then tamper with presentation content while recomputing both local digests.
        other = self.root / "presentation"
        other.mkdir()
        original_root = self.root
        self.root = other
        try:
            repository, _service, project_id, _first = self._create_blueprint_project()
        finally:
            self.root = original_root
        confirm = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="confirm",
            decision_units=["script"],
            runtime_authority_epoch="epoch_one",
        )
        repository.confirm_blueprint_decision_units(
            project_id,
            runtime_authority_epoch="epoch_one",
            presentation=confirm["presentation"],
            presentation_sha256=str(confirm["presentation_sha256"]),
            native_gesture_nonce="confirm_corrupt_two",
            idempotency_key="confirm-corrupt-two",
        )
        with closing(repository._connect()) as connection, connection:
            row = connection.execute("SELECT * FROM blueprint_decision_authority_events").fetchone()
            assert row is not None
            event = repository._authority_event_from_row(row)
            presentation = deepcopy(event["presentation"])
            presentation["decision_units"][0]["content"] = {"blocks": []}
            new_presentation_sha256 = content_sha256(presentation)
            material = repository._authority_event_material(
                event_id=str(event["id"]),
                database_uuid=str(event["database_uuid"]),
                project_id=str(event["project_id"]),
                sequence=int(event["sequence"]),
                parent_event_id=None,
                parent_event_sha256=None,
                authority_operation=str(event["authority_operation"]),
                observed_head=event["observed_head"],
                decision_units=event["decision_units"],
                unit_digests=event["unit_digests"],
                active_confirmation_ids=event["active_confirmation_ids"],
                presentation_sha256=new_presentation_sha256,
                runtime_authority_epoch=str(event["runtime_authority_epoch"]),
                native_gesture_nonce=str(event["native_gesture_nonce"]),
                request_sha256=str(event["request_sha256"]),
                created_at=str(event["created_at"]),
            )
            new_event_sha256 = content_sha256(material)
            connection.execute("DROP TRIGGER trg_blueprint_authority_events_no_update")
            connection.execute("DROP TRIGGER trg_blueprint_authority_heads_forward_only")
            connection.execute(
                """UPDATE blueprint_decision_authority_events
                      SET presentation_json=?,presentation_sha256=?,event_sha256=?""",
                (canonical_json(presentation), new_presentation_sha256, new_event_sha256),
            )
            connection.execute(
                "UPDATE blueprint_decision_authority_heads SET event_sha256=?",
                (new_event_sha256,),
            )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_authority_presentation_binding_invalid",
        ):
            repository.get_blueprint_authority_projection(project_id)

    def test_authority_receipt_digest_detects_valid_identity_rebinding(self) -> None:
        repository, _service, project_id, _first = self._create_blueprint_project()
        confirm = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="confirm",
            decision_units=["script"],
            runtime_authority_epoch="epoch_one",
        )
        repository.confirm_blueprint_decision_units(
            project_id,
            runtime_authority_epoch="epoch_one",
            presentation=confirm["presentation"],
            presentation_sha256=str(confirm["presentation_sha256"]),
            native_gesture_nonce="confirm_identity_digest",
            idempotency_key="confirm-original",
        )
        with closing(repository._connect()) as connection, connection:
            connection.execute("DROP TRIGGER trg_blueprint_authority_receipts_no_update")
            connection.execute(
                "UPDATE blueprint_decision_authority_receipts "
                "SET idempotency_key='confirm-rebound'"
            )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_authority_receipt_digest_mismatch",
        ):
            repository.get_blueprint_authority_projection(project_id)


if __name__ == "__main__":
    unittest.main()
