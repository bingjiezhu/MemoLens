from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from core.blueprint_contract import (
    BLUEPRINT_SCHEMA_SHA256,
    blueprint_content_sha256,
    blueprint_semantic_sha256,
    compile_blueprint_document,
    diff_blueprint_sections,
)
from core.db import ImageIndexRepository
from core.media_db import (
    BASELINE_CHECKSUM,
    SCHEMA_STATEMENTS,
    SCHEMA_VERSION,
    V2_CHECKSUM,
    V3_CHECKSUM,
    V3_SCHEMA_STATEMENTS,
    V4_CHECKSUM,
    V5_CHECKSUM,
    V6_CHECKSUM,
    _V6_SCHEMA_OBJECTS,
    V7_CHECKSUM,
    _V7_SCHEMA_OBJECTS,
    BlueprintHeadConflictError,
    BlueprintIntegrityError,
    BlueprintReceiptConflictError,
    MediaMigrationError,
    MediaRepository,
    canonical_json,
    content_sha256,
    utc_now_iso,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def semantic_fixture(label: str = "one") -> dict[str, object]:
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


def initialize_fresh(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library, db_path


def initialize_v3(root: Path) -> tuple[MediaRepository, Path, Path]:
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
        now = utc_now_iso()
        connection.execute(
            """INSERT INTO database_meta(singleton,database_uuid,schema_version,created_at,updated_at)
               VALUES(1,?,3,?,?)""",
            ("database_v3_fixture", now, now),
        )
        repository._upsert_library_root(connection, library)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return repository, library, db_path


class BlueprintMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-blueprint-migration-")
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v2_through_v7_checksums_remain_frozen_and_fresh_current_skips_backup(self) -> None:
        self.assertEqual(V2_CHECKSUM, "8563c16d6ccb95c36e13abf4143a888b109e8a6606da9744c64307755f052654")
        self.assertEqual(V3_CHECKSUM, "4ba03fd7b65b59683d721cd5340805d893dda59d72a087bf824af117f265fc90")
        self.assertEqual(V4_CHECKSUM, "dfc3227eff26b8c8b2fde38269a9549fe890c13a364a517f740959a95b528d39")
        self.assertEqual(V5_CHECKSUM, "5889c2f79a36aeed8937e4d8f31994767cd56ae193d8bf27dc636906abc72526")
        self.assertEqual(V6_CHECKSUM, "cddb91190311db0f5593db6af5788d54f51c2d82385cad4ea6067da51461716e")
        self.assertEqual(V7_CHECKSUM, "687918fbb4692d872b025467321d7c2509c64836b8bad7717eef46960783edf2")
        self.assertEqual(SCHEMA_VERSION, 20)
        repository, _, db_path = initialize_fresh(self.root)
        self.addCleanup(repository.close)
        self.assertFalse((db_path.parent / "migration-backups").exists())
        with closing(sqlite3.connect(db_path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                (SCHEMA_VERSION,),
            )
            self.assertEqual(
                connection.execute("SELECT name FROM schema_migrations WHERE version=4").fetchone(),
                ("canonical_blueprint_proposal_ledger",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM schema_migrations WHERE version=5"
                ).fetchone(),
                ("paired_agent_decision_authority",),
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
            self.assertEqual(
                connection.execute(
                    "SELECT name,checksum FROM schema_migrations WHERE version=7"
                ).fetchone(),
                ("canonical_timeline_first_cut", V7_CHECKSUM),
            )
            for object_type, name in _V7_SCHEMA_OBJECTS:
                self.assertEqual(
                    connection.execute(
                        "SELECT type,name FROM sqlite_schema WHERE type=? AND name=?",
                        (object_type, name),
                    ).fetchone(),
                    (object_type, name),
                )

    def test_v3_upgrade_creates_restricted_verified_backup_and_preserves_rows(self) -> None:
        repository, library, db_path = initialize_v3(self.root)
        self.addCleanup(repository.close)
        project = repository.create_project("Before v4", {"goal": "preserve me"}, {"source": "test"})

        repository.ensure_schema(library)

        self.assertEqual(repository.database_uuid, "database_v3_fixture")
        self.assertIsNotNone(repository.get_project(str(project["id"])))
        backup_dir = db_path.parent / "migration-backups"
        files = list(backup_dir.iterdir())
        self.assertEqual(len(files), 2)
        manifest_path = next(path for path in files if path.name.endswith(".manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = backup_dir / manifest["backup_filename"]
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o600)
        self.assertEqual(hashlib.sha256(backup_path.read_bytes()).hexdigest(), manifest["backup_sha256"])
        self.assertEqual(manifest["database_uuid"], "database_v3_fixture")
        with closing(sqlite3.connect(backup_path)) as backup, backup:
            self.assertEqual(backup.execute("SELECT schema_version FROM database_meta").fetchone(), (3,))
            self.assertEqual(backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone(), (3,))
            self.assertEqual(
                backup.execute("SELECT title FROM creative_projects WHERE id=?", (project["id"],)).fetchone(),
                ("Before v4",),
            )

    def test_future_and_checksum_faults_fail_before_managed_ddl(self) -> None:
        for fault in ("future", "checksum"):
            with self.subTest(fault=fault):
                child = self.root / fault
                child.mkdir()
                repository, library, db_path = initialize_v3(child)
                self.addCleanup(repository.close)
                with closing(sqlite3.connect(db_path)) as connection, connection:
                    if fault == "future":
                        connection.execute(
                            "INSERT INTO schema_migrations VALUES(?,?,?,?)",
                            (SCHEMA_VERSION + 1, "future", "future", utc_now_iso()),
                        )
                        connection.execute(
                            "UPDATE database_meta SET schema_version=?",
                            (SCHEMA_VERSION + 1,),
                        )
                    else:
                        connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=2")
                expected_error = (
                    f"future_schema_version:{SCHEMA_VERSION + 1}"
                    if fault == "future"
                    else "migration_checksum_mismatch for version 2"
                )
                with self.assertRaisesRegex(MediaMigrationError, expected_error):
                    repository.ensure_schema(library)
                with closing(sqlite3.connect(db_path)) as connection, connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_blueprint_revisions'"
                        ).fetchone()
                    )

    def test_failed_v4_ddl_rolls_back_but_keeps_recovery_backup(self) -> None:
        repository, library, db_path = initialize_v3(self.root)
        self.addCleanup(repository.close)
        broken = (
            "CREATE TABLE should_rollback(value TEXT)",
            "CREATE TABLE syntactically_broken(",
        )
        with patch("core.media_db.V4_SCHEMA_STATEMENTS", broken), self.assertRaises(sqlite3.Error):
            repository.ensure_schema(library)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT schema_version FROM database_meta").fetchone(), (3,))
            self.assertIsNone(connection.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone())
        self.assertEqual(len(list((db_path.parent / "migration-backups").glob("*.sqlite3"))), 1)

    def test_v3_backup_rejects_a_symlink_directory_without_external_writes_or_chmod(self) -> None:
        repository, library, db_path = initialize_v3(self.root)
        self.addCleanup(repository.close)
        external = self.root / "external"
        external.mkdir(mode=0o755)
        os_mode = stat.S_IMODE(external.stat().st_mode)
        (db_path.parent / "migration-backups").symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(MediaMigrationError, "migration_backup_directory_unsafe"):
            repository.ensure_schema(library)

        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), os_mode)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT schema_version FROM database_meta").fetchone(), (3,))
            self.assertIsNone(connection.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone())

    def test_v4_guards_schema_downgrade_and_immutable_relations(self) -> None:
        repository, _, db_path = initialize_fresh(self.root)
        self.addCleanup(repository.close)
        project = repository.create_project("Immutable", {"goal": "test"}, {})
        with closing(sqlite3.connect(db_path)) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "downgrade"):
                connection.execute("UPDATE database_meta SET schema_version=3")
            for table in (
                "creative_blueprint_revisions",
                "creative_blueprint_operations",
                "blueprint_command_receipts",
            ):
                triggers = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                    (table,),
                ).fetchone()[0]
                self.assertEqual(triggers, 2)
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM sqlite_master
                        WHERE type='trigger' AND tbl_name='creative_blueprint_heads'"""
                ).fetchone()[0],
                3,
            )
        self.assertIsNotNone(repository.get_project(str(project["id"])))

    def test_v4_name_collisions_and_physical_lookalikes_fail_closed(self) -> None:
        collision_root = self.root / "collision"
        collision_root.mkdir()
        repository, library, db_path = initialize_v3(collision_root)
        self.addCleanup(repository.close)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("CREATE TABLE creative_blueprint_revisions(decoy TEXT)")
        with self.assertRaisesRegex(MediaMigrationError, "v4_schema_name_collision"):
            repository.ensure_schema(library)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT schema_version FROM database_meta").fetchone(), (3,))
            self.assertIsNone(connection.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone())
        self.assertFalse((db_path.parent / "migration-backups").exists())

        lookalike_root = self.root / "lookalike"
        lookalike_root.mkdir()
        repository, library, db_path = initialize_fresh(lookalike_root)
        self.addCleanup(repository.close)
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_creative_blueprint_operations_no_update")
            connection.execute(
                """CREATE TRIGGER trg_creative_blueprint_operations_no_update
                   BEFORE UPDATE ON creative_blueprint_operations BEGIN SELECT 1; END"""
            )
        with self.assertRaisesRegex(MediaMigrationError, "v4_physical_schema_mismatch"):
            repository.ensure_schema(library)

    def test_revision_parent_shape_is_enforced_by_sqlite(self) -> None:
        repository, _, db_path = initialize_fresh(self.root)
        self.addCleanup(repository.close)
        project = repository.create_project("Parent shape", {"goal": "test"}, {})
        row_values = (
            project["id"],
            "1",
            str(BLUEPRINT_SCHEMA_SHA256),
            "{}",
            content_sha256({}),
            SHA_A,
            "unverified",
            "operation_missing",
            utc_now_iso(),
        )
        with closing(sqlite3.connect(db_path)) as connection, connection:
            for revision, parent_revision, parent_digest in (
                (1, 1, SHA_A),
                (2, None, None),
                (3, 1, SHA_A),
            ):
                with self.subTest(revision=revision, parent_revision=parent_revision):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """INSERT INTO creative_blueprint_revisions(
                                 project_id,revision,parent_revision,parent_content_sha256,
                                 schema_version,schema_sha256,blueprint_json,content_sha256,
                                 semantic_sha256,authority_state,operation_id,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                row_values[0],
                                revision,
                                parent_revision,
                                parent_digest,
                                *row_values[1:],
                            ),
                        )


class CreatorBindingPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-creator-binding-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, _, self.db_path = initialize_fresh(self.root)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _put(
        self,
        *,
        profile: dict[str, object],
        source: str,
        evidence: list[dict[str, object]] | None = None,
        profile_id: str = "default",
    ) -> dict[str, object]:
        current = self.repository.get_creator_profile(profile_id=profile_id)
        base_revision = int(current["revision"]) if current is not None else 0
        stored, replayed = self.repository.put_creator_profile(
            profile_id=profile_id,
            base_revision=base_revision,
            profile=profile,
            evidence=evidence or [],
            source=source,
            idempotency_scope="test:creator-binding",
            idempotency_key=f"{profile_id}-{base_revision + 1}",
            request_sha256=hashlib.sha256(f"{profile_id}-{base_revision + 1}".encode()).hexdigest(),
        )
        self.assertFalse(replayed)
        return stored

    def _resolve(self, stored: dict[str, object]) -> dict[str, object] | None:
        binding = {
            "profile_id": stored["profile_id"],
            "revision": stored["revision"],
            "content_sha256": stored["content_sha256"],
        }
        with self.repository.transaction(immediate=False) as connection:
            return self.repository.resolve_creator_profile_binding_in_transaction(
                connection,
                binding,
            )

    def _confirmed(self) -> dict[str, object]:
        projects = [
            self.repository.create_project(
                f"Evidence {index}",
                {"tone": "warm", "platform": "short-video"},
                {"source": "test"},
            )
            for index in range(2)
        ]
        evidence = [
            {"project_id": str(project["id"]), "brief_revision": 1}
            for project in projects
        ]
        current = self.repository.get_creator_profile(profile_id="default")
        profile = dict(current["profile"]) if current is not None else {}
        profile["tone"] = "warm"
        return self._put(
            profile=profile,
            evidence=evidence,
            source="confirmed_suggestion",
        )

    def test_exact_binding_accepts_all_three_confirmed_sources(self) -> None:
        user_edit = self._put(
            profile={
                "platform": "short-video",
                "audience": "individual creators",
                "duration_ms": 30_000,
                "aspect_ratio": "9:16",
                "tone": "natural",
                "pace": "measured",
                "narrative_arc": "question to answer",
                "must_include": ["local material"],
                "must_exclude": ["stock footage"],
            },
            source="user_edit",
        )
        confirmed = self._confirmed()
        reset = self._put(profile={}, source="reset")

        for stored in (user_edit, confirmed, reset):
            with self.subTest(source=stored["source"]):
                resolved = self._resolve(stored)
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved, stored)

        wrong_digest = dict(user_edit)
        wrong_digest["content_sha256"] = SHA_A
        self.assertIsNone(self._resolve(wrong_digest))
        missing = dict(user_edit)
        missing["revision"] = 999
        self.assertIsNone(self._resolve(missing))

        non_default = self._put(
            profile_id="creator_non_default",
            profile={"tone": "natural"},
            source="user_edit",
        )
        self.assertIsNone(self._resolve(non_default))

    def test_binding_rejects_unknown_or_source_incoherent_rows(self) -> None:
        stored = self._put(
            profile={"tone": "natural"},
            source="user_edit",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE creator_profile_revisions SET source='model_inference' WHERE profile_id=?",
                (stored["profile_id"],),
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "creator_profile_binding_row_invalid"):
            self._resolve(stored)

        reset = self._put(profile={}, source="reset")
        profile = {"tone": "forged"}
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE creator_profile_revisions
                      SET profile_json=?,content_sha256=? WHERE profile_id=?""",
                (canonical_json(profile), content_sha256(profile), reset["profile_id"]),
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "creator_profile_binding_source_invalid"):
            self._resolve(reset)

    def test_binding_rejects_digest_valid_but_invalid_profile_shapes_and_values(self) -> None:
        stored = self._put(
            profile={"tone": "natural"},
            source="user_edit",
        )
        hostile_profiles = (
            {"unknown": "value"},
            {"tone": " padded "},
            {"duration_ms": 999},
            {"aspect_ratio": "3:2"},
            {"must_include": ["duplicate", "duplicate"]},
        )
        for profile in hostile_profiles:
            with self.subTest(profile=profile):
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        """UPDATE creator_profile_revisions
                              SET profile_json=?,content_sha256=? WHERE profile_id=?""",
                        (canonical_json(profile), content_sha256(profile), stored["profile_id"]),
                    )
                with self.assertRaisesRegex(
                    BlueprintIntegrityError,
                    "creator_profile_binding_profile_invalid",
                ):
                    self._resolve(stored)

    def test_binding_rejects_invalid_evidence_shape_and_missing_brief(self) -> None:
        stored = self._confirmed()
        evidence = stored["evidence"]
        assert isinstance(evidence, list)
        invalid = [evidence[0], evidence[0]]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE creator_profile_revisions SET evidence_json=? WHERE profile_id=?",
                (canonical_json(invalid), stored["profile_id"]),
            )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "creator_profile_binding_evidence_invalid",
        ):
            self._resolve(stored)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE creator_profile_revisions SET evidence_json=? WHERE profile_id=?",
                (canonical_json(evidence), stored["profile_id"]),
            )
            connection.execute(
                "DELETE FROM creative_briefs WHERE project_id=? AND revision=1",
                (evidence[0]["project_id"],),
            )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "creator_profile_evidence_brief_missing",
        ):
            self._resolve(stored)

    def test_binding_rejects_evidence_brief_digest_or_canonical_json_corruption(self) -> None:
        for corruption in ("digest", "canonical"):
            with self.subTest(corruption=corruption):
                child = self.root / corruption
                child.mkdir()
                repository, _, db_path = initialize_fresh(child)
                original_repository = self.repository
                original_db_path = self.db_path
                self.repository = repository
                self.db_path = db_path
                try:
                    stored = self._confirmed()
                    evidence = stored["evidence"]
                    assert isinstance(evidence, list)
                    project_id = evidence[0]["project_id"]
                    with closing(sqlite3.connect(db_path)) as connection, connection:
                        if corruption == "digest":
                            connection.execute(
                                "UPDATE creative_briefs SET content_sha256=? WHERE project_id=?",
                                (SHA_A, project_id),
                            )
                        else:
                            raw = '{"platform": "short-video","tone":"warm"}'
                            connection.execute(
                                """UPDATE creative_briefs SET brief_json=?,content_sha256=?
                                    WHERE project_id=?""",
                                (raw, hashlib.sha256(raw.encode()).hexdigest(), project_id),
                            )
                    code = (
                        "creator_profile_evidence_brief_identity_invalid"
                        if corruption == "digest"
                        else "creator_profile_evidence_brief_json_invalid"
                    )
                    with self.assertRaisesRegex(BlueprintIntegrityError, code):
                        self._resolve(stored)
                finally:
                    self.repository = original_repository
                    self.db_path = original_db_path
                    repository.close()


class BlueprintPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-blueprint-ledger-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = initialize_fresh(self.root)
        self.project = self.repository.create_project(
            "Blueprint",
            {"goal": "compile local footage"},
            {"source": "fixture"},
        )
        self.project_id = str(self.project["id"])
        self.initial_legacy = {
            "revision": int(self.project["brief_revision"]),
            "content_sha256": str(self.project["brief_content_sha256"]),
        }

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    @staticmethod
    def _request_digest(identity: str) -> str:
        return hashlib.sha256(identity.encode()).hexdigest()

    def _response(
        self,
        *,
        command_type: str,
        operation_id: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        return {
            "object": "creative_blueprint.command_result",
            "schema_version": "1",
            "project_id": self.project_id,
            "command_type": command_type,
            "operation_id": operation_id,
            "result": result,
            "authority": {"state": "unverified", "verified": False},
            "ledger": {
                "coverage_scope": "creative_blueprint",
                "complete_project_history": False,
            },
        }

    def _fault_after(self, checkpoint: str):
        original = getattr(self.repository, checkpoint)

        def fail_after_checkpoint(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError(f"{checkpoint}_failure")

        return patch.object(self.repository, checkpoint, side_effect=fail_after_checkpoint)

    def _tamper_with_trigger_restored(
        self,
        trigger_name: str,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> None:
        """Corrupt one immutable row without turning schema loss into the oracle."""

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            assert trigger is not None and isinstance(trigger[0], str)
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(statement, parameters)
            connection.execute(trigger[0])

    def _commit(
        self,
        *,
        semantic: dict[str, object],
        expected_head: dict[str, object] | None,
        key: str,
        operation_id: str,
        restore_from: dict[str, object] | None = None,
    ):
        request_digest = self._request_digest(key)
        command_type = "blueprint.restore_revision" if restore_from is not None else "blueprint.commit_proposal"
        intent_code = "restore_blueprint" if restore_from is not None else "propose_blueprint"
        result_kind = "revision_restored" if restore_from is not None else "revision_created"

        def mutation(connection: sqlite3.Connection):
            current = self.repository.get_blueprint_head_in_transaction(connection, self.project_id)
            if expected_head is None:
                if current is not None:
                    raise BlueprintHeadConflictError(self.repository._head_projection(current))
                revision = 1
                parent = None
            else:
                if current is None or any(current[key] != expected_head[key] for key in ("revision", "content_sha256")):
                    raise BlueprintHeadConflictError(self.repository._head_projection(current) if current else None)
                revision = int(current["revision"]) + 1
                parent = {
                    "revision": int(current["revision"]),
                    "content_sha256": str(current["content_sha256"]),
                }
            document = compile_blueprint_document(
                project_id=self.project_id,
                revision=revision,
                parent=parent,
                created_by_operation_id=operation_id,
                semantic=semantic,
                source_candidate_sha256=SHA_B,
                initial_legacy_brief=self.initial_legacy,
                evidence_manifest=[],
            )
            document_digest = blueprint_content_sha256(document)
            semantic_digest = blueprint_semantic_sha256(semantic)
            result_head = {
                "revision": revision,
                "content_sha256": document_digest,
                "semantic_sha256": semantic_digest,
                "operation_id": operation_id,
            }
            current_semantic = current["blueprint"]["semantic"] if current is not None else None
            changed = diff_blueprint_sections(current_semantic, semantic)
            result = {
                "kind": result_kind,
                "result_head": result_head,
                "changed_sections": [item["section"] for item in changed],
                "restore_from": restore_from,
            }
            precondition = (
                {"expected_head": expected_head, "restore_from": restore_from}
                if restore_from is not None
                else {
                    "expected_head": expected_head,
                    "initial_legacy_brief": self.initial_legacy if current is None else None,
                }
            )
            self.repository.append_blueprint_operation(
                connection,
                operation_id=operation_id,
                project_id=self.project_id,
                command_type=command_type,
                intent_code=intent_code,
                precondition=precondition,
                typed_diff=changed,
                input_sha256=request_digest,
                output_sha256=document_digest,
                result_kind=result_kind,
                result_revision=revision,
                result_content_sha256=document_digest,
                result=result,
            )
            self.repository.append_blueprint_revision(
                connection,
                project_id=self.project_id,
                revision=revision,
                parent_revision=parent["revision"] if parent else None,
                parent_content_sha256=parent["content_sha256"] if parent else None,
                schema_sha256=str(BLUEPRINT_SCHEMA_SHA256),
                blueprint=document,
                content_sha256=document_digest,
                semantic_sha256=semantic_digest,
                source_candidate_sha256=SHA_B,
                operation_id=operation_id,
            )
            self.repository.cas_blueprint_head(
                connection,
                project_id=self.project_id,
                expected_head=expected_head,
                new_head=result_head,
            )
            self.repository.touch_creative_project(connection, self.project_id)
            response = self._response(
                command_type=command_type,
                operation_id=operation_id,
                result=result,
            )
            return response, 201, operation_id, f"{self.project_id}:{revision}"

        return self.repository.execute_blueprint_command(
            authenticated_principal="desktop_app",
            project_id=self.project_id,
            command_type=command_type,
            command_version="1",
            idempotency_key=key,
            request_sha256=request_digest,
            mutation=mutation,
        )

    def _no_change(self, *, key: str, operation_id: str, expected_head: dict[str, object]):
        request_digest = self._request_digest(key)

        def mutation(connection: sqlite3.Connection):
            current = self.repository.get_blueprint_head_in_transaction(connection, self.project_id)
            assert current is not None
            result_head = self.repository._head_projection(current)
            result_head.pop("project_id")
            result = {
                "kind": "no_change",
                "result_head": result_head,
                "changed_sections": [],
                "restore_from": None,
            }
            self.repository.append_blueprint_operation(
                connection,
                operation_id=operation_id,
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                intent_code="propose_blueprint",
                precondition={"expected_head": expected_head, "restore_from": None},
                typed_diff=[],
                input_sha256=request_digest,
                output_sha256=str(current["content_sha256"]),
                result_kind="no_change",
                result_revision=int(current["revision"]),
                result_content_sha256=str(current["content_sha256"]),
                result=result,
            )
            response = self._response(
                command_type="blueprint.commit_proposal",
                operation_id=operation_id,
                result=result,
            )
            return response, 200, operation_id, f"{self.project_id}:{current['revision']}"

        return self.repository.execute_blueprint_command(
            authenticated_principal="desktop_app",
            project_id=self.project_id,
            command_type="blueprint.commit_proposal",
            command_version="1",
            idempotency_key=key,
            request_sha256=request_digest,
            mutation=mutation,
        )

    def test_commit_replay_conflict_and_no_change_are_exact_and_permanent(self) -> None:
        first = self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="commit-one",
            operation_id="operation_001",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        expected = {"revision": head["revision"], "content_sha256": head["content_sha256"]}
        no_change = self._no_change(key="no-change", operation_id="operation_002", expected_head=expected)
        after_no_change = self.repository.get_blueprint_head(self.project_id)
        self.assertEqual(after_no_change["operation_id"], "operation_001")
        self.assertEqual(after_no_change["revision"], 1)
        self.assertEqual(no_change.response["result"]["kind"], "no_change")
        self.assertEqual(
            [item["sequence"] for item in self.repository.list_blueprint_operations(self.project_id)], [2, 1]
        )

        replay = self.repository.execute_blueprint_command(
            authenticated_principal="desktop_app",
            project_id=self.project_id,
            command_type="blueprint.commit_proposal",
            command_version="1",
            idempotency_key="commit-one",
            request_sha256=self._request_digest("commit-one"),
            mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        with self.assertRaises(BlueprintReceiptConflictError):
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="commit-one",
                request_sha256=SHA_A,
                mutation=lambda _connection: ({}, 200, "never", None),
            )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM creative_blueprint_revisions").fetchone(), (1,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM creative_blueprint_operations").fetchone(), (2,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blueprint_command_receipts").fetchone(), (2,))

    def test_replay_survives_head_advance_and_a_new_repository_instance(self) -> None:
        first = self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head={"revision": head["revision"], "content_sha256": head["content_sha256"]},
            key="two",
            operation_id="operation_002",
        )
        restarted = MediaRepository(self.db_path)
        self.addCleanup(restarted.close)
        replay = restarted.execute_blueprint_command(
            authenticated_principal="desktop_app",
            project_id=self.project_id,
            command_type="blueprint.commit_proposal",
            command_version="1",
            idempotency_key="one",
            request_sha256=self._request_digest("one"),
            mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(replay.operation_id, "operation_001")
        self.assertEqual(restarted.get_blueprint_head(self.project_id)["revision"], 2)

    def test_receipt_older_than_24_hours_replays_before_deleted_live_references(self) -> None:
        referenced = self.repository.create_project(
            "Referenced project",
            {"goal": "reference that may later be removed"},
            {"source": "fixture"},
        )
        semantic = semantic_fixture("durable")
        semantic["reference_refs"] = [
            {
                "reference_id": "reference_live_state",
                "kind": "project",
                "locator": referenced["id"],
                "note": None,
            }
        ]
        old_timestamp = "2020-01-01T00:00:00+00:00"
        with patch("core.media_db.utc_now_iso", return_value=old_timestamp):
            first = self._commit(
                semantic=semantic,
                expected_head=None,
                key="durable-old",
                operation_id="operation_old",
            )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    """SELECT created_at FROM blueprint_command_receipts
                        WHERE idempotency_key='durable-old'"""
                ).fetchone(),
                (old_timestamp,),
            )
            connection.execute(
                "DELETE FROM creative_projects WHERE id=?",
                (referenced["id"],),
            )
            connection.execute(
                "DELETE FROM creative_briefs WHERE project_id=?",
                (self.project_id,),
            )

        restarted = MediaRepository(self.db_path)
        self.addCleanup(restarted.close)
        replay = restarted.execute_blueprint_command(
            authenticated_principal="desktop_app",
            project_id=self.project_id,
            command_type="blueprint.commit_proposal",
            command_version="1",
            idempotency_key="durable-old",
            request_sha256=self._request_digest("durable-old"),
            mutation=lambda _connection: (_ for _ in ()).throw(
                AssertionError("durable replay must precede live reference checks")
            ),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)

    def test_restore_creates_new_revision_without_rewriting_history(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        first = self.repository.get_blueprint_revision(self.project_id, 1)
        head = self.repository.get_blueprint_head(self.project_id)
        assert first is not None and head is not None
        expected = {"revision": head["revision"], "content_sha256": head["content_sha256"]}
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head=expected,
            key="two",
            operation_id="operation_002",
        )
        current = self.repository.get_blueprint_head(self.project_id)
        assert current is not None
        expected = {"revision": current["revision"], "content_sha256": current["content_sha256"]}
        self._commit(
            semantic=first["blueprint"]["semantic"],
            expected_head=expected,
            key="restore",
            operation_id="operation_003",
            restore_from={"revision": 1, "content_sha256": first["content_sha256"]},
        )
        restored = self.repository.get_blueprint_head(self.project_id)
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["parent_revision"], 2)
        self.assertEqual(restored["semantic_sha256"], first["semantic_sha256"])
        self.assertNotEqual(restored["content_sha256"], first["content_sha256"])
        self.assertEqual(
            self.repository.get_blueprint_revision(self.project_id, 1)["content_sha256"], first["content_sha256"]
        )

    def test_stale_cas_rolls_back_operation_revision_and_receipt(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        base = self.repository.get_blueprint_head(self.project_id)
        assert base is not None
        expected = {"revision": base["revision"], "content_sha256": base["content_sha256"]}
        self._commit(
            semantic=semantic_fixture("winner"),
            expected_head=expected,
            key="winner",
            operation_id="operation_002",
        )
        before = self._counts()
        with self.assertRaises(BlueprintHeadConflictError):
            self._commit(
                semantic=semantic_fixture("stale"),
                expected_head=expected,
                key="stale",
                operation_id="operation_003",
            )
        self.assertEqual(self._counts(), before)

    def test_each_atomic_checkpoint_failure_rolls_back_every_blueprint_row(self) -> None:
        before_project = self.repository.get_creative_project_in_transaction
        with closing(self.repository._connect()) as connection, connection:
            project_updated_at = before_project(connection, self.project_id)["updated_at"]
        for checkpoint in (
            "append_blueprint_operation",
            "append_blueprint_revision",
            "cas_blueprint_head",
            "touch_creative_project",
            "_store_blueprint_receipt",
        ):
            with self.subTest(checkpoint=checkpoint):
                with (
                    self._fault_after(checkpoint),
                    self.assertRaisesRegex(RuntimeError, f"{checkpoint}_failure"),
                ):
                    self._commit(
                        semantic=semantic_fixture(f"fault-{checkpoint}"),
                        expected_head=None,
                        key=f"fault-{checkpoint}",
                        operation_id=f"operation_fault_{checkpoint}",
                    )
                self.assertEqual(self._counts(), (0, 0, 0, 0))
                with closing(self.repository._connect()) as connection, connection:
                    self.assertEqual(
                        before_project(connection, self.project_id)["updated_at"],
                        project_updated_at,
                    )

    def test_no_change_checkpoint_failures_roll_back_operation_and_receipt_only(self) -> None:
        self._commit(
            semantic=semantic_fixture("baseline"),
            expected_head=None,
            key="baseline",
            operation_id="operation_baseline",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        expected = {"revision": head["revision"], "content_sha256": head["content_sha256"]}
        before_counts = self._counts()
        before_head = self.repository._head_projection(head)
        with closing(self.repository._connect()) as connection, connection:
            before_updated_at = self.repository.get_creative_project_in_transaction(
                connection,
                self.project_id,
            )["updated_at"]

        for checkpoint in ("append_blueprint_operation", "_store_blueprint_receipt"):
            with self.subTest(checkpoint=checkpoint):
                with (
                    self._fault_after(checkpoint),
                    self.assertRaisesRegex(RuntimeError, f"{checkpoint}_failure"),
                ):
                    self._no_change(
                        key=f"no-change-fault-{checkpoint}",
                        operation_id=f"operation_no_change_fault_{checkpoint}",
                        expected_head=expected,
                    )
                self.assertEqual(self._counts(), before_counts)
                self.assertEqual(
                    self.repository._head_projection(
                        self.repository.get_blueprint_head(self.project_id)
                    ),
                    before_head,
                )
                with closing(self.repository._connect()) as connection, connection:
                    self.assertEqual(
                        self.repository.get_creative_project_in_transaction(
                            connection,
                            self.project_id,
                        )["updated_at"],
                        before_updated_at,
                    )

    def test_restore_checkpoint_failures_roll_back_all_project_state(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        first = self.repository.get_blueprint_revision(self.project_id, 1)
        current = self.repository.get_blueprint_head(self.project_id)
        assert first is not None and current is not None
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head={
                "revision": current["revision"],
                "content_sha256": current["content_sha256"],
            },
            key="two",
            operation_id="operation_002",
        )
        current = self.repository.get_blueprint_head(self.project_id)
        assert current is not None
        expected = {
            "revision": current["revision"],
            "content_sha256": current["content_sha256"],
        }
        restore_from = {
            "revision": first["revision"],
            "content_sha256": first["content_sha256"],
        }
        before_counts = self._counts()
        before_head = self.repository._head_projection(current)
        with closing(self.repository._connect()) as connection, connection:
            before_updated_at = self.repository.get_creative_project_in_transaction(
                connection,
                self.project_id,
            )["updated_at"]

        for checkpoint in (
            "append_blueprint_operation",
            "append_blueprint_revision",
            "cas_blueprint_head",
            "touch_creative_project",
            "_store_blueprint_receipt",
        ):
            with self.subTest(checkpoint=checkpoint):
                with (
                    self._fault_after(checkpoint),
                    self.assertRaisesRegex(RuntimeError, f"{checkpoint}_failure"),
                ):
                    self._commit(
                        semantic=first["blueprint"]["semantic"],
                        expected_head=expected,
                        key=f"restore-fault-{checkpoint}",
                        operation_id=f"operation_restore_fault_{checkpoint}",
                        restore_from=restore_from,
                    )
                self.assertEqual(self._counts(), before_counts)
                self.assertEqual(
                    self.repository._head_projection(
                        self.repository.get_blueprint_head(self.project_id)
                    ),
                    before_head,
                )
                with closing(self.repository._connect()) as connection, connection:
                    self.assertEqual(
                        self.repository.get_creative_project_in_transaction(
                            connection,
                            self.project_id,
                        )["updated_at"],
                        before_updated_at,
                    )

    def test_corrupt_or_missing_explicit_head_never_falls_back(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER trg_blueprint_heads_forward_only")
            connection.execute(
                "UPDATE creative_blueprint_heads SET content_sha256=? WHERE project_id=?",
                (SHA_A, self.project_id),
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_head_identity_mismatch"):
            self.repository.get_blueprint_head(self.project_id)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_blueprint_heads_no_delete")
            connection.execute("DELETE FROM creative_blueprint_heads WHERE project_id=?", (self.project_id,))
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_head_missing"):
            self.repository.get_blueprint_head(self.project_id)
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_head_missing"):
            self.repository.list_blueprint_operations(self.project_id, limit=1)

    def test_head_cannot_be_deleted_or_rolled_back_to_an_older_valid_revision(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        first = self.repository.get_blueprint_revision(self.project_id, 1)
        current = self.repository.get_blueprint_head(self.project_id)
        assert first is not None and current is not None
        expected = {"revision": current["revision"], "content_sha256": current["content_sha256"]}
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head=expected,
            key="two",
            operation_id="operation_002",
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                connection.execute(
                    "DELETE FROM creative_blueprint_heads WHERE project_id=?",
                    (self.project_id,),
                )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_blueprint_heads_forward_only")
            connection.execute(
                """UPDATE creative_blueprint_heads
                      SET revision=?,content_sha256=?,semantic_sha256=?,operation_id=?,updated_at=?
                    WHERE project_id=?""",
                (
                    first["revision"],
                    first["content_sha256"],
                    first["semantic_sha256"],
                    first["operation_id"],
                    utc_now_iso(),
                    self.project_id,
                ),
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_head_rollback_detected"):
            self.repository.get_blueprint_head(self.project_id)

    def test_snapshot_marks_current_and_exact_revision_without_a_second_connection(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        first = self.repository.get_blueprint_head(self.project_id)
        assert first is not None
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head={"revision": first["revision"], "content_sha256": first["content_sha256"]},
            key="two",
            operation_id="operation_002",
        )
        current = self.repository.read_blueprint_snapshot(self.project_id)
        exact = self.repository.read_blueprint_snapshot(self.project_id, revision=1)
        self.assertTrue(current["is_current"])
        self.assertEqual(current["revision"]["revision"], 2)
        self.assertEqual(current["current_head"]["revision"], 2)
        self.assertFalse(exact["is_current"])
        self.assertEqual(exact["revision"]["revision"], 1)
        self.assertEqual(exact["current_head"], current["current_head"])

    def test_history_validates_the_full_chain_outside_the_return_window(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        expected = {"revision": head["revision"], "content_sha256": head["content_sha256"]}
        for index in range(2, 5):
            self._no_change(
                key=f"no-change-{index}",
                operation_id=f"operation_00{index}",
                expected_head=expected,
            )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_creative_blueprint_operations_no_update")
            connection.execute(
                """UPDATE creative_blueprint_operations SET parent_operation_id='operation_003'
                    WHERE id='operation_002'"""
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_operation_chain_invalid"):
            self.repository.list_blueprint_operations(self.project_id, limit=1)

    def test_restore_no_change_is_rejected_without_a_ledger_side_effect(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        head_ref = {"revision": head["revision"], "content_sha256": head["content_sha256"]}
        result_head = self.repository._head_projection(head)
        result_head.pop("project_id")
        result = {
            "kind": "no_change",
            "result_head": result_head,
            "changed_sections": [],
            "restore_from": head_ref,
        }
        before = self._counts()
        with self.repository.transaction(immediate=True) as connection:
            with self.assertRaisesRegex(ValueError, "blueprint_command_result_mismatch"):
                self.repository.append_blueprint_operation(
                    connection,
                    operation_id="operation_invalid_restore",
                    project_id=self.project_id,
                    command_type="blueprint.restore_revision",
                    intent_code="restore_blueprint",
                    precondition={"expected_head": head_ref, "restore_from": head_ref},
                    typed_diff=[],
                    input_sha256=self._request_digest("invalid-restore"),
                    output_sha256=str(head["content_sha256"]),
                    result_kind="no_change",
                    result_revision=int(head["revision"]),
                    result_content_sha256=str(head["content_sha256"]),
                    result=result,
                )
        self.assertEqual(self._counts(), before)

    def test_evidence_proof_is_stable_and_uses_no_locator(self) -> None:
        asset_sha = hashlib.sha256(b"asset").hexdigest()
        now = utc_now_iso()
        with self.repository.transaction(immediate=True) as connection:
            root_id = connection.execute(
                "SELECT id FROM library_roots WHERE status='active' LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO assets(id,kind,sha256,mime_type,file_size,codec_json,probe_status,created_at,updated_at)
                   VALUES('asset_proof','video',?,'video/mp4',5,'{}','ready',?,?)""",
                (asset_sha, now, now),
            )
            connection.execute(
                """INSERT INTO asset_sources(
                     id,asset_id,library_root_id,relative_path,display_filename,observed_size,
                     availability,is_preferred,last_verified_at,created_at,updated_at)
                   VALUES('source_proof','asset_proof',?,'proof.mp4','proof.mp4',5,
                          'available',1,?,?,?)""",
                (root_id, now, now, now),
            )
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,analysis_profile_json,
                     input_asset_sha256,status,transcript_status,visual_status,created_at)
                   VALUES('run_proof','asset_proof',1,'initial','profile','{}',?,'succeeded','available','ready',?)""",
                (asset_sha, now),
            )
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,boundary_reason,
                     semantic_json,combined_text,visual_status,transcript_status,created_at)
                   VALUES('span_proof','asset_proof','run_proof',0,100,900,'fixture','{}','',
                          'ready','available',?)""",
                (now,),
            )
            connection.execute(
                "INSERT INTO asset_analysis_heads(asset_id,analysis_run_id,updated_at) VALUES('asset_proof','run_proof',?)",
                (now,),
            )
            manifest = self.repository.resolve_blueprint_evidence_in_transaction(
                connection,
                (
                    "memolens://evidence/asset/asset_proof",
                    "memolens://evidence/span/span_proof",
                    "memolens://evidence/asset/missing",
                ),
            )
        self.assertEqual(manifest[0]["status"], "verified")
        self.assertEqual(
            manifest[0]["proof_sha256"],
            content_sha256({"kind": "asset", "asset_id": "asset_proof", "asset_sha256": asset_sha}),
        )
        self.assertEqual(manifest[1]["status"], "verified")
        self.assertEqual(
            manifest[2],
            {
                "evidence_ref": "memolens://evidence/asset/missing",
                "status": "unresolved",
                "proof_sha256": None,
            },
        )
        self.assertNotIn("path", canonical_json(manifest))

    def test_evidence_requires_current_successful_analysis_and_an_active_available_source(self) -> None:
        asset_sha = hashlib.sha256(b"availability-fixture").hexdigest()
        now = utc_now_iso()
        asset_ref = "memolens://evidence/asset/asset_availability"
        span_ref = "memolens://evidence/span/span_availability"
        with self.repository.transaction(immediate=True) as connection:
            root_id = connection.execute(
                "SELECT id FROM library_roots WHERE status='active' LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO assets(
                     id,kind,sha256,mime_type,file_size,codec_json,probe_status,created_at,updated_at)
                   VALUES('asset_availability','video',?,'video/mp4',5,'{}','ready',?,?)""",
                (asset_sha, now, now),
            )
            connection.execute(
                """INSERT INTO asset_sources(
                     id,asset_id,library_root_id,relative_path,display_filename,observed_size,
                     availability,is_preferred,last_verified_at,created_at,updated_at)
                   VALUES('source_availability','asset_availability',?,'availability.mp4',
                          'availability.mp4',5,'available',1,?,?,?)""",
                (root_id, now, now, now),
            )
            for run_id, revision, run_kind in (
                ("run_current", 1, "initial"),
                ("run_newer", 2, "reanalyze"),
            ):
                connection.execute(
                    """INSERT INTO analysis_runs(
                         id,asset_id,revision,run_kind,analysis_profile_id,analysis_profile_json,
                         input_asset_sha256,status,transcript_status,visual_status,created_at)
                       VALUES(?, 'asset_availability', ?, ?, 'profile', '{}', ?, 'succeeded',
                              'available','ready',?)""",
                    (run_id, revision, run_kind, asset_sha, now),
                )
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,boundary_reason,
                     semantic_json,combined_text,visual_status,transcript_status,created_at)
                   VALUES('span_availability','asset_availability','run_current',0,10,20,
                          'fixture','{}','','ready','available',?)""",
                (now,),
            )
            connection.execute(
                """INSERT INTO asset_analysis_heads(asset_id,analysis_run_id,updated_at)
                   VALUES('asset_availability','run_current',?)""",
                (now,),
            )

            def statuses() -> list[str]:
                return [
                    str(item["status"])
                    for item in self.repository.resolve_blueprint_evidence_in_transaction(
                        connection,
                        (asset_ref, span_ref),
                    )
                ]

            self.assertEqual(statuses(), ["verified", "verified"])
            connection.execute(
                """UPDATE asset_analysis_heads SET analysis_run_id='run_newer'
                    WHERE asset_id='asset_availability'"""
            )
            self.assertEqual(statuses(), ["verified", "unresolved"])
            connection.execute(
                """UPDATE asset_analysis_heads SET analysis_run_id='run_current'
                    WHERE asset_id='asset_availability'"""
            )
            connection.execute("UPDATE analysis_runs SET status='failed' WHERE id='run_current'")
            self.assertEqual(statuses(), ["verified", "unresolved"])
            connection.execute("UPDATE analysis_runs SET status='succeeded' WHERE id='run_current'")
            connection.execute(
                "UPDATE asset_sources SET availability='missing' WHERE id='source_availability'"
            )
            self.assertEqual(statuses(), ["unresolved", "unresolved"])
            connection.execute(
                "UPDATE asset_sources SET availability='available' WHERE id='source_availability'"
            )
            connection.execute(
                "UPDATE library_roots SET status='unavailable' WHERE id=?",
                (root_id,),
            )
            self.assertEqual(statuses(), ["unresolved", "unresolved"])

    def test_receipt_corruption_fails_before_mutation(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_update",
            "UPDATE blueprint_command_receipts SET response_json='{}' WHERE idempotency_key='one'",
        )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_receipt_response_digest_mismatch"):
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="one",
                request_sha256=self._request_digest("one"),
                mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
            )

    def test_receipt_recomputed_tampering_cannot_escalate_authority_or_change_result(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            original = connection.execute(
                "SELECT * FROM blueprint_command_receipts WHERE idempotency_key='one'"
            ).fetchone()
            original_json = str(original["response_json"])
            original_sha = str(original["response_sha256"])
            original_status = int(original["response_status"])
            original_resource = str(original["resource_id"])
            forged = json.loads(original_json)
            forged["authority"] = {"state": "verified", "verified": True}
            forged_json = canonical_json(forged)
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_update",
            """UPDATE blueprint_command_receipts
                  SET response_json=?,response_sha256=? WHERE idempotency_key='one'""",
            (forged_json, hashlib.sha256(forged_json.encode()).hexdigest()),
        )

        def replay() -> None:
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="one",
                request_sha256=self._request_digest("one"),
                mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
            )

        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_desktop_receipt_digest_mismatch",
        ):
            self.repository.get_blueprint_head(self.project_id)
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_desktop_receipt_digest_mismatch"):
            replay()
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_update",
            """UPDATE blueprint_command_receipts
                  SET response_json=?,response_sha256=?,response_status=200
                WHERE idempotency_key='one'""",
            (original_json, original_sha),
        )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_desktop_receipt_digest_mismatch"):
            replay()
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_update",
            """UPDATE blueprint_command_receipts
                  SET response_status=?,resource_id='different:999'
                WHERE idempotency_key='one'""",
            (original_status,),
        )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_desktop_receipt_digest_mismatch"):
            replay()
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_update",
            "UPDATE blueprint_command_receipts SET resource_id=? WHERE idempotency_key='one'",
            (original_resource,),
        )

    def test_oversized_persisted_json_fails_before_blueprint_deserialization(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        oversized = "x" * 1_048_577
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_update",
            """UPDATE blueprint_command_receipts
                  SET response_json=?,response_sha256=? WHERE idempotency_key='one'""",
            (oversized, hashlib.sha256(oversized.encode()).hexdigest()),
        )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_receipt_response_too_large"):
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="one",
                request_sha256=self._request_digest("one"),
                mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
            )
        self._tamper_with_trigger_restored(
            "trg_blueprint_revisions_no_update",
            "UPDATE creative_blueprint_revisions SET blueprint_json=? WHERE revision=1",
            (oversized,),
        )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_revision_json_too_large"):
            self.repository.get_blueprint_head(self.project_id)

    def test_operation_diff_and_revision_parent_are_cross_validated(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        first = self.repository.get_blueprint_head(self.project_id)
        assert first is not None
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head={"revision": first["revision"], "content_sha256": first["content_sha256"]},
            key="two",
            operation_id="operation_002",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            operation = connection.execute(
                "SELECT typed_diff_json,result_json FROM creative_blueprint_operations WHERE id='operation_002'"
            ).fetchone()
            typed_diff = json.loads(operation["typed_diff_json"])
            typed_diff[0]["before_sha256"] = typed_diff[0]["after_sha256"]
            connection.execute("DROP TRIGGER trg_creative_blueprint_operations_no_update")
            connection.execute(
                "UPDATE creative_blueprint_operations SET typed_diff_json=? WHERE id='operation_002'",
                (canonical_json(typed_diff),),
            )
        with closing(self.repository._connect()) as connection, connection:
            with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_operation_diff_invalid"):
                self.repository.get_blueprint_operation_in_transaction(
                    connection,
                    "operation_002",
                    project_id=self.project_id,
                )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            result = json.loads(operation["result_json"])
            result["changed_sections"] = []
            connection.execute(
                """UPDATE creative_blueprint_operations SET typed_diff_json='[]',result_json=?
                    WHERE id='operation_002'""",
                (canonical_json(result),),
            )
        with closing(self.repository._connect()) as connection, connection:
            self.assertIsNotNone(
                self.repository.get_blueprint_operation_in_transaction(
                    connection,
                    "operation_002",
                    project_id=self.project_id,
                )
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_revision_operation_mismatch"):
            self.repository.get_blueprint_head(self.project_id)

    def test_no_change_cannot_claim_a_synchronized_nonempty_diff(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        expected = {"revision": head["revision"], "content_sha256": head["content_sha256"]}
        self._no_change(key="no-change", operation_id="operation_002", expected_head=expected)
        forged_diff = [
            {
                "section": "script",
                "change": "modified",
                "before_sha256": SHA_A,
                "after_sha256": SHA_B,
            }
        ]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            result = json.loads(
                connection.execute(
                    "SELECT result_json FROM creative_blueprint_operations WHERE id='operation_002'"
                ).fetchone()[0]
            )
            result["changed_sections"] = ["script"]
            connection.execute("DROP TRIGGER trg_creative_blueprint_operations_no_update")
            connection.execute(
                """UPDATE creative_blueprint_operations SET typed_diff_json=?,result_json=?
                    WHERE id='operation_002'""",
                (canonical_json(forged_diff), canonical_json(result)),
            )
        with closing(self.repository._connect()) as connection, connection:
            with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_operation_diff_invalid"):
                self.repository.get_blueprint_operation_in_transaction(
                    connection,
                    "operation_002",
                    project_id=self.project_id,
                )

    def test_current_read_validates_every_ancestor_to_revision_one(self) -> None:
        expected_head: dict[str, object] | None = None
        for revision in range(1, 4):
            self._commit(
                semantic=semantic_fixture(f"revision-{revision}"),
                expected_head=expected_head,
                key=f"revision-{revision}",
                operation_id=f"operation_00{revision}",
            )
            head = self.repository.get_blueprint_head(self.project_id)
            assert head is not None
            expected_head = {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            }
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_blueprint_revisions_no_update")
            connection.execute(
                "UPDATE creative_blueprint_revisions SET blueprint_json='{}' WHERE revision=1"
            )
        with self.assertRaises(BlueprintIntegrityError):
            self.repository.get_blueprint_head(self.project_id)

    def test_replay_validates_the_receipt_linked_revision_before_acknowledging(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_blueprint_revisions_no_update")
            connection.execute(
                "UPDATE creative_blueprint_revisions SET blueprint_json='{}' WHERE revision=1"
            )
        with self.assertRaises(BlueprintIntegrityError):
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="one",
                request_sha256=self._request_digest("one"),
                mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
            )

    def test_replay_validates_the_immutable_operation_prefix_before_acknowledging(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        self._commit(
            semantic=semantic_fixture("two"),
            expected_head={
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            },
            key="two",
            operation_id="operation_002",
        )
        self._tamper_with_trigger_restored(
            "trg_creative_blueprint_operations_no_update",
            """UPDATE creative_blueprint_operations
                  SET parent_operation_id='operation_002'
                WHERE id='operation_002'""",
        )
        before = self._counts()
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_operation_chain_invalid"):
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="two",
                request_sha256=self._request_digest("two"),
                mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
            )
        self.assertEqual(self._counts(), before)

    def test_missing_durable_receipt_blocks_retry_instead_of_reexecuting(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        self._tamper_with_trigger_restored(
            "trg_blueprint_receipts_no_delete",
            "DELETE FROM blueprint_command_receipts WHERE idempotency_key='one'",
        )
        before = self._counts()
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_receipt_ledger_incomplete"):
            self.repository.execute_blueprint_command(
                authenticated_principal="desktop_app",
                project_id=self.project_id,
                command_type="blueprint.commit_proposal",
                command_version="1",
                idempotency_key="one",
                request_sha256=self._request_digest("one"),
                mutation=lambda _connection: (_ for _ in ()).throw(AssertionError("must not run")),
            )
        self.assertEqual(self._counts(), before)

    def test_legacy_base_rejects_duplicate_noncanonical_nonfinite_and_deep_json(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            original = connection.execute(
                """SELECT brief_json,content_sha256,provenance_json
                     FROM creative_briefs WHERE project_id=?""",
                (self.project_id,),
            ).fetchone()
        hostile = (
            '{"goal":"one","goal":"two"}',
            '{ "goal":"noncanonical"}',
            '{"goal":NaN}',
            '{"deep":' + "[" * 1100 + "0" + "]" * 1100 + "}",
        )
        for raw in hostile:
            with self.subTest(raw=raw[:24]):
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        """UPDATE creative_briefs SET brief_json=?,content_sha256=?
                            WHERE project_id=?""",
                        (raw, hashlib.sha256(raw.encode()).hexdigest(), self.project_id),
                    )
                with closing(self.repository._connect()) as connection, connection:
                    with self.assertRaisesRegex(BlueprintIntegrityError, "legacy_brief_json_invalid"):
                        self.repository.get_latest_creative_brief_in_transaction(
                            connection,
                            self.project_id,
                        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE creative_briefs
                      SET brief_json=?,content_sha256=?,provenance_json=? WHERE project_id=?""",
                (original[0], original[1], '{"source":"one","source":"two"}', self.project_id),
            )
        with closing(self.repository._connect()) as connection, connection:
            with self.assertRaisesRegex(BlueprintIntegrityError, "legacy_brief_provenance_invalid"):
                self.repository.get_latest_creative_brief_in_transaction(
                    connection,
                    self.project_id,
                )

    def test_blueprint_timestamps_fail_closed_when_recomputed_content_is_unchanged(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_creative_blueprint_operations_no_update")
            connection.execute(
                "UPDATE creative_blueprint_operations SET created_at='not-a-time' WHERE id='operation_001'"
            )
        with self.assertRaisesRegex(BlueprintIntegrityError, "blueprint_operation_row_invalid"):
            self.repository.get_blueprint_head(self.project_id)

    def test_revision_operation_and_receipt_rows_are_physically_immutable(self) -> None:
        self._commit(
            semantic=semantic_fixture("one"),
            expected_head=None,
            key="one",
            operation_id="operation_001",
        )
        attempts = (
            "UPDATE creative_blueprint_revisions SET created_at='changed'",
            "DELETE FROM creative_blueprint_revisions",
            "UPDATE creative_blueprint_operations SET created_at='changed'",
            "DELETE FROM creative_blueprint_operations",
            "UPDATE blueprint_command_receipts SET created_at='changed'",
            "DELETE FROM blueprint_command_receipts",
        )
        for index, statement in enumerate(attempts):
            with self.subTest(statement=statement):
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(statement)
            self.assertEqual(self._counts(), (1, 1, 1, 1), index)

    def test_concurrent_same_base_never_silently_overwrites(self) -> None:
        self._commit(
            semantic=semantic_fixture("initial"),
            expected_head=None,
            key="initial",
            operation_id="operation_initial",
        )
        current = self.repository.get_blueprint_head(self.project_id)
        assert current is not None
        expected = {"revision": current["revision"], "content_sha256": current["content_sha256"]}
        for round_number in range(100):
            def contender(index: int) -> tuple[str, dict[str, object] | None]:
                try:
                    committed = self._commit(
                        semantic=semantic_fixture(f"round-{round_number}-{index}"),
                        expected_head=expected,
                        key=f"round-{round_number}-{index}",
                        operation_id=f"operation_{round_number}_{index}",
                    )
                    return "committed", committed.response["result"]["result_head"]
                except BlueprintHeadConflictError:
                    return "conflict", None

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(contender, (0, 1)))
            self.assertEqual(sorted(item[0] for item in outcomes), ["committed", "conflict"])
            advanced = next(item[1] for item in outcomes if item[0] == "committed")
            assert advanced is not None
            self.assertEqual(advanced["revision"], int(expected["revision"]) + 1)
            expected = {
                "revision": advanced["revision"],
                "content_sha256": advanced["content_sha256"],
            }
        persisted = self.repository.get_blueprint_head(self.project_id)
        self.assertEqual(
            {"revision": persisted["revision"], "content_sha256": persisted["content_sha256"]},
            expected,
        )
        self.assertEqual(self._counts(), (101, 1, 101, 101))

    def _counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "creative_blueprint_revisions",
                    "creative_blueprint_heads",
                    "creative_blueprint_operations",
                    "blueprint_command_receipts",
                )
            )


if __name__ == "__main__":
    unittest.main()
