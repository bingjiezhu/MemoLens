from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS))

from backend.src.media.blueprint import BlueprintService  # noqa: E402
from core.blueprint_contract import (  # noqa: E402
    SEMANTIC_SECTIONS,
    blueprint_content_sha256,
    canonical_json,
    compile_blueprint_document,
    diff_blueprint_sections,
)
from core.db import ImageIndexRepository  # noqa: E402
from core.media_db import MediaRepository  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_core import MemoLensGateway, TRUST_LOCAL_API_ENV  # noqa: E402
from memolens_mcp import TOOLS, call_tool  # noqa: E402
import memolens_persisted_blueprint as persisted_blueprint  # noqa: E402
from memolens_persisted_blueprint import (  # noqa: E402
    PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE,
    PERSISTED_BLUEPRINT_SCHEMA_SHA256,
)


SECRET_SCRIPT = "PRIVATE_SCRIPT_MUST_NOT_APPEAR_IN_HISTORY"
SECRET_REFERENCE = "https://example.com/reference?private_key=never-return-in-history"
SECRET_PATH = "/Users/private/creator/raw.mov"
CLI_PATH = SCRIPTS / "memolens_cli.py"


def semantic() -> dict[str, Any]:
    return {
        "intent": {
            "goal": "把闲置素材剪成短片",
            "stance": "普通生活值得被认真观看",
            "audience": "个人创作者",
            "platform": "short-video",
        },
        "script": {"blocks": [{"block_id": "hook", "text": SECRET_SCRIPT}]},
        "direction": {
            "theme": "重新观看日常",
            "narrative_arc": "提问—证据—立场",
            "emotion": "克制",
            "tone": "真诚",
            "pace": "先快后慢",
        },
        "output": {"duration_target_ms": 30_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [],
        "reference_refs": [
            {
                "reference_id": "reference_web",
                "kind": "external_https",
                "locator": SECRET_REFERENCE,
                "note": "节奏参考",
            }
        ],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [
            {
                "decision_id": "music",
                "question": "是否保留现场声？",
                "scope": "direction",
                "required_before": "render",
            }
        ],
    }


class PersistedBlueprintFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "blueprint-v4.db"
        self.media = self.root / "do-not-open.mov"
        self.media.write_bytes(b"private media")
        self._create()

    def close(self) -> None:
        self.temporary.cleanup()

    def gateway(self) -> MemoLensGateway:
        with mock.patch.dict(
            os.environ,
            {TRUST_LOCAL_API_ENV: "0", "MEMOLENS_PLUGIN_TRUST_LOCAL_API": "0"},
            clear=False,
        ):
            return MemoLensGateway(
                db_path=self.db,
                base_url="http://must-never-resolve.invalid:1",
            )

    @staticmethod
    def _canonical(value: Any) -> str:
        return canonical_json(value)

    def _create(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE creative_projects (
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,status TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE creative_briefs (
                    project_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    brief_json TEXT NOT NULL,content_sha256 TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id,revision)
                );
                CREATE TABLE creative_blueprint_revisions (
                    project_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    parent_revision INTEGER,parent_content_sha256 TEXT,
                    schema_version TEXT NOT NULL,schema_sha256 TEXT NOT NULL,
                    blueprint_json TEXT NOT NULL,content_sha256 TEXT NOT NULL,
                    semantic_sha256 TEXT NOT NULL,source_candidate_sha256 TEXT,
                    authority_state TEXT NOT NULL,operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,PRIMARY KEY(project_id,revision)
                );
                CREATE TABLE creative_blueprint_heads (
                    project_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,semantic_sha256 TEXT NOT NULL,
                    operation_id TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE creative_blueprint_operations (
                    id TEXT PRIMARY KEY,project_id TEXT NOT NULL,sequence INTEGER NOT NULL,
                    parent_operation_id TEXT,schema_version TEXT NOT NULL,
                    coverage_scope TEXT NOT NULL,complete_project_history INTEGER NOT NULL,
                    command_type TEXT NOT NULL,command_version TEXT NOT NULL,
                    effect_class TEXT NOT NULL,actor_json TEXT NOT NULL,
                    origin_json TEXT NOT NULL,intent_code TEXT NOT NULL,
                    precondition_json TEXT NOT NULL,typed_diff_json TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,output_sha256 TEXT NOT NULL,
                    result_kind TEXT NOT NULL,result_revision INTEGER NOT NULL,
                    result_content_sha256 TEXT NOT NULL,result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,UNIQUE(project_id,sequence)
                );
                CREATE TABLE assets (id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha256 TEXT NOT NULL);
                CREATE TABLE asset_sources (
                    id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,relative_path TEXT NOT NULL,
                    display_filename TEXT NOT NULL,availability TEXT NOT NULL
                );
                CREATE TABLE database_meta (database_uuid TEXT NOT NULL,schema_version INTEGER NOT NULL);
                """
            )
            connection.executemany(
                "INSERT INTO creative_projects VALUES (?,?,?,?,?)",
                [
                    ("proj_blueprint", "Canonical", "active", "2026-08-22T00:00:00+00:00", "2026-08-22T00:00:00+00:00"),
                    ("proj_legacy", "Legacy", "draft", "2026-08-22T00:00:00+00:00", "2026-08-22T00:00:00+00:00"),
                ],
            )
            brief = self._canonical({"schema_version": "legacy-1", "goal": "legacy"})
            for project_id in ("proj_blueprint", "proj_legacy"):
                connection.execute(
                    "INSERT INTO creative_briefs VALUES (?,?,?,?,?,?)",
                    (project_id, 1, brief, hashlib.sha256(brief.encode()).hexdigest(), "{}", "2026-08-22T00:00:00+00:00"),
                )
            connection.execute("INSERT INTO assets VALUES ('asset_identity','image',?)", ("a" * 64,))
            connection.execute("INSERT INTO database_meta VALUES ('db_fixture',4)")
            self._insert_revision(connection)
            connection.commit()

    def _insert_revision(self, connection: sqlite3.Connection) -> None:
        operation_id = "op_blueprint_1"
        initial_legacy_brief = {
            "revision": 1,
            "content_sha256": hashlib.sha256(
                self._canonical(
                    {"schema_version": "legacy-1", "goal": "legacy"}
                ).encode()
            ).hexdigest(),
        }
        document = compile_blueprint_document(
            project_id="proj_blueprint",
            revision=1,
            parent=None,
            created_by_operation_id=operation_id,
            semantic=semantic(),
            source_candidate_sha256="c" * 64,
            initial_legacy_brief=initial_legacy_brief,
            evidence_manifest=[],
        )
        content_sha256 = blueprint_content_sha256(document)
        raw = self._canonical(document)
        result = {
            "kind": "revision_created",
            "result_head": {
                "revision": 1,
                "content_sha256": content_sha256,
                "semantic_sha256": document["semantic_sha256"],
                "operation_id": operation_id,
            },
            "changed_sections": list(SEMANTIC_SECTIONS),
            "restore_from": None,
        }
        connection.execute(
            "INSERT INTO creative_blueprint_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,"proj_blueprint",1,None,"1","creative_blueprint",0,
                "blueprint.commit_proposal","1","reversible_project_write",
                self._canonical({"identity_verified": True, "kind": "desktop_app_command", "user_authority_verified": False}),
                self._canonical({"principal": "desktop_app", "surface": "desktop_authenticated_api"}),
                "propose_blueprint",self._canonical({
                    "expected_head": None,
                    "initial_legacy_brief": initial_legacy_brief,
                }),
                self._canonical(diff_blueprint_sections(None, semantic())),"d" * 64,content_sha256,"revision_created",1,
                content_sha256,self._canonical(result),"2026-08-22T00:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO creative_blueprint_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "proj_blueprint",1,None,None,"1",PERSISTED_BLUEPRINT_SCHEMA_SHA256,
                raw,content_sha256,document["semantic_sha256"],"c" * 64,
                "unverified",operation_id,"2026-08-22T00:01:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO creative_blueprint_heads VALUES (?,?,?,?,?,?)",
            (
                "proj_blueprint",1,content_sha256,document["semantic_sha256"],
                operation_id,"2026-08-22T00:01:00+00:00",
            ),
        )

    def insert_no_change(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            head = connection.execute("SELECT * FROM creative_blueprint_heads WHERE project_id='proj_blueprint'").fetchone()
            operation_id = "op_blueprint_2"
            result = {
                "kind": "no_change",
                "result_head": {
                    "revision": head[1],
                    "content_sha256": head[2],
                    "semantic_sha256": head[3],
                    "operation_id": head[4],
                },
                "changed_sections": [],
                "restore_from": None,
            }
            connection.execute(
                "INSERT INTO creative_blueprint_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,"proj_blueprint",2,"op_blueprint_1","1","creative_blueprint",0,
                    "blueprint.commit_proposal","1","reversible_project_write",
                    self._canonical({"identity_verified": True, "kind": "desktop_app_command", "user_authority_verified": False}),
                    self._canonical({"principal": "desktop_app", "surface": "desktop_authenticated_api"}),
                    "propose_blueprint",self._canonical({
                        "expected_head": {
                            "revision": head[1],
                            "content_sha256": head[2],
                        },
                        "restore_from": None,
                    }),"[]","e" * 64,head[2],
                    "no_change",head[1],head[2],self._canonical(result),"2026-08-22T00:02:00+00:00",
                ),
            )
            connection.commit()

    def insert_second_revision(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            head = connection.execute(
                "SELECT revision,content_sha256,semantic_sha256,operation_id "
                "FROM creative_blueprint_heads WHERE project_id='proj_blueprint'"
            ).fetchone()
            assert head is not None
            second_semantic = semantic()
            second_semantic["intent"]["goal"] = "把闲置素材剪成第二版短片"
            operation_id = "op_blueprint_2"
            document = compile_blueprint_document(
                project_id="proj_blueprint",
                revision=2,
                parent={"revision": head[0], "content_sha256": head[1]},
                created_by_operation_id=operation_id,
                semantic=second_semantic,
                source_candidate_sha256="e" * 64,
                initial_legacy_brief={
                    "revision": 1,
                    "content_sha256": hashlib.sha256(
                        self._canonical(
                            {"schema_version": "legacy-1", "goal": "legacy"}
                        ).encode()
                    ).hexdigest(),
                },
                evidence_manifest=[],
            )
            content_sha256 = blueprint_content_sha256(document)
            result = {
                "kind": "revision_created",
                "result_head": {
                    "revision": 2,
                    "content_sha256": content_sha256,
                    "semantic_sha256": document["semantic_sha256"],
                    "operation_id": operation_id,
                },
                "changed_sections": ["intent"],
                "restore_from": None,
            }
            connection.execute(
                "INSERT INTO creative_blueprint_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    "proj_blueprint",
                    2,
                    head[3],
                    "1",
                    "creative_blueprint",
                    0,
                    "blueprint.commit_proposal",
                    "1",
                    "reversible_project_write",
                    self._canonical(
                        {
                            "identity_verified": True,
                            "kind": "desktop_app_command",
                            "user_authority_verified": False,
                        }
                    ),
                    self._canonical(
                        {
                            "principal": "desktop_app",
                            "surface": "desktop_authenticated_api",
                        }
                    ),
                    "propose_blueprint",
                    self._canonical(
                        {
                            "expected_head": {
                                "revision": head[0],
                                "content_sha256": head[1],
                            },
                            "initial_legacy_brief": None,
                        }
                    ),
                    self._canonical(
                        diff_blueprint_sections(semantic(), second_semantic)
                    ),
                    "f" * 64,
                    content_sha256,
                    "revision_created",
                    2,
                    content_sha256,
                    self._canonical(result),
                    "2026-08-22T00:02:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO creative_blueprint_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "proj_blueprint",
                    2,
                    head[0],
                    head[1],
                    "1",
                    PERSISTED_BLUEPRINT_SCHEMA_SHA256,
                    self._canonical(document),
                    content_sha256,
                    document["semantic_sha256"],
                    "e" * 64,
                    "unverified",
                    operation_id,
                    "2026-08-22T00:02:00+00:00",
                ),
            )
            connection.execute(
                "UPDATE creative_blueprint_heads SET revision=2,content_sha256=?,"
                "semantic_sha256=?,operation_id=?,updated_at=? "
                "WHERE project_id='proj_blueprint'",
                (
                    content_sha256,
                    document["semantic_sha256"],
                    operation_id,
                    "2026-08-22T00:02:00+00:00",
                ),
            )
            connection.commit()

    def insert_third_revision(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            head = connection.execute(
                "SELECT revision,content_sha256,semantic_sha256,operation_id "
                "FROM creative_blueprint_heads WHERE project_id='proj_blueprint'"
            ).fetchone()
            assert head is not None and head[0] == 2
            current_raw = connection.execute(
                "SELECT blueprint_json FROM creative_blueprint_revisions "
                "WHERE project_id='proj_blueprint' AND revision=2"
            ).fetchone()
            assert current_raw is not None
            current_semantic = json.loads(current_raw[0])["semantic"]
            third_semantic = semantic()
            third_semantic["intent"]["goal"] = "把闲置素材剪成第三版短片"
            operation_id = "op_blueprint_3"
            created_at = "2026-08-22T00:03:00+00:00"
            document = compile_blueprint_document(
                project_id="proj_blueprint",
                revision=3,
                parent={"revision": head[0], "content_sha256": head[1]},
                created_by_operation_id=operation_id,
                semantic=third_semantic,
                source_candidate_sha256="9" * 64,
                initial_legacy_brief={
                    "revision": 1,
                    "content_sha256": hashlib.sha256(
                        self._canonical(
                            {"schema_version": "legacy-1", "goal": "legacy"}
                        ).encode()
                    ).hexdigest(),
                },
                evidence_manifest=[],
            )
            content_sha256 = blueprint_content_sha256(document)
            result = {
                "kind": "revision_created",
                "result_head": {
                    "revision": 3,
                    "content_sha256": content_sha256,
                    "semantic_sha256": document["semantic_sha256"],
                    "operation_id": operation_id,
                },
                "changed_sections": ["intent"],
                "restore_from": None,
            }
            connection.execute(
                "INSERT INTO creative_blueprint_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    "proj_blueprint",
                    3,
                    head[3],
                    "1",
                    "creative_blueprint",
                    0,
                    "blueprint.commit_proposal",
                    "1",
                    "reversible_project_write",
                    self._canonical(
                        {
                            "identity_verified": True,
                            "kind": "desktop_app_command",
                            "user_authority_verified": False,
                        }
                    ),
                    self._canonical(
                        {
                            "principal": "desktop_app",
                            "surface": "desktop_authenticated_api",
                        }
                    ),
                    "propose_blueprint",
                    self._canonical(
                        {
                            "expected_head": {
                                "revision": head[0],
                                "content_sha256": head[1],
                            },
                            "initial_legacy_brief": None,
                        }
                    ),
                    self._canonical(
                        diff_blueprint_sections(current_semantic, third_semantic)
                    ),
                    "8" * 64,
                    content_sha256,
                    "revision_created",
                    3,
                    content_sha256,
                    self._canonical(result),
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO creative_blueprint_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "proj_blueprint",
                    3,
                    head[0],
                    head[1],
                    "1",
                    PERSISTED_BLUEPRINT_SCHEMA_SHA256,
                    self._canonical(document),
                    content_sha256,
                    document["semantic_sha256"],
                    "9" * 64,
                    "unverified",
                    operation_id,
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE creative_blueprint_heads SET revision=3,content_sha256=?,"
                "semantic_sha256=?,operation_id=?,updated_at=? "
                "WHERE project_id='proj_blueprint'",
                (
                    content_sha256,
                    document["semantic_sha256"],
                    operation_id,
                    created_at,
                ),
            )
            connection.commit()


class PersistedBlueprintReadTests(unittest.TestCase):
    synthetic_peak_retained = 0

    def setUp(self) -> None:
        self.fixture = PersistedBlueprintFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_schema_digest_and_status_capabilities_are_honest(self) -> None:
        self.assertTrue(PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE)
        self.assertEqual(
            PERSISTED_BLUEPRINT_SCHEMA_SHA256,
            hashlib.sha256((PLUGIN_ROOT / "schemas" / "creative-blueprint-v1.schema.json").read_bytes()).hexdigest(),
        )
        status = self.fixture.gateway().status()
        capabilities = status["capabilities"]
        self.assertTrue(capabilities["read_persisted_blueprint"])
        self.assertTrue(capabilities["read_blueprint_history"])
        self.assertTrue(capabilities["read_blueprint_operation_ledger"])
        self.assertFalse(capabilities["complete_project_history"])
        self.assertFalse(capabilities["write_blueprint"])

    def test_agent_responses_never_emit_bare_authority_claims_at_0_1_or_8_units(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-persisted-authority-claims-"
        ) as temporary:
            root = Path(temporary)
            library = root / "library"
            library.mkdir()
            db_path = root / "media.db"
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            self.addCleanup(repository.close)
            repository.ensure_schema(library)
            project = repository.create_project(
                "Persisted authority claims",
                {"goal": "legacy"},
                {"source": "test"},
            )
            project_id = str(project["id"])
            brief = repository.get_brief(project_id, 1)
            assert brief is not None
            proposal = semantic()
            proposal["reference_refs"] = []
            BlueprintService(repository).commit_proposal(
                project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": proposal,
                },
                idempotency_key="persisted-authority-claims-initial",
            )
            gateway = MemoLensGateway(
                db_path=db_path,
                base_url="http://must-never-resolve.invalid:1",
            )

            def assert_no_bare_claims(value: Any) -> None:
                if isinstance(value, dict):
                    self.assertNotIn("authority_verified", value)
                    self.assertNotIn("user_confirmed", value)
                    for child in value.values():
                        assert_no_bare_claims(child)
                elif isinstance(value, list):
                    for child in value:
                        assert_no_bare_claims(child)

            responses: list[tuple[int, dict[str, Any]]] = []
            zero = gateway.blueprint_get(project_id)
            responses.append((0, zero))

            one_unit = repository.build_blueprint_authority_presentation(
                project_id,
                authority_operation="confirm",
                decision_units=["script"],
                runtime_authority_epoch="persisted_claims_epoch",
            )
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="persisted_claims_epoch",
                presentation=one_unit["presentation"],
                presentation_sha256=str(one_unit["presentation_sha256"]),
                native_gesture_nonce="persisted_claims_one",
                idempotency_key="persisted-claims-one",
            )
            one = gateway.blueprint_get(project_id)
            responses.append((1, one))

            remaining_units = [
                "intent_goal",
                "intent_stance",
                "creative_direction",
                "output",
                "material_constraints",
                "references",
                "techniques",
            ]
            all_units = repository.build_blueprint_authority_presentation(
                project_id,
                authority_operation="confirm",
                decision_units=remaining_units,
                runtime_authority_epoch="persisted_claims_epoch",
            )
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="persisted_claims_epoch",
                presentation=all_units["presentation"],
                presentation_sha256=str(all_units["presentation_sha256"]),
                native_gesture_nonce="persisted_claims_all",
                idempotency_key="persisted-claims-all",
            )
            eight = gateway.blueprint_get(project_id)
            responses.append((8, eight))
            # Blueprint commands retain a repository-lifetime verification
            # anchor.  Release it before the temporary database is removed;
            # addCleanup above also covers an earlier assertion failure.
            repository.close()

        for expected_confirmed, response in responses:
            assert_no_bare_claims(response)
            self.assertFalse(
                response["contract"]["creation_authority_verified"]
            )
            self.assertEqual(
                response["blueprint"]["authority_projection"][
                    "confirmed_decision_unit_count"
                ],
                expected_confirmed,
            )

    def test_current_exact_cli_mcp_contract_and_project_resume_precedence(self) -> None:
        gateway = self.fixture.gateway()
        current = gateway.blueprint_get("proj_blueprint")
        exact = gateway.blueprint_get("proj_blueprint", revision=1)
        via_mcp = call_tool(
            "memolens_blueprint_get",
            {"project_id": "proj_blueprint", "revision": 1},
            gateway,
        )
        self.assertEqual(current["blueprint"], exact["blueprint"])
        self.assertEqual(exact, via_mcp)
        self.assertEqual(
            current["blueprint"]["object"],
            "memolens.creative_blueprint_projection",
        )
        self.assertEqual(current["selection"]["kind"], "current")
        self.assertEqual(exact["selection"]["kind"], "exact_revision")
        authority_summary = current["blueprint"]["authority_summary"]
        self.assertFalse(authority_summary["all_decision_units_confirmed"])
        self.assertFalse(authority_summary["any_decision_unit_confirmed"])
        self.assertNotIn("authority_verified", authority_summary)
        self.assertNotIn("user_confirmed", authority_summary)
        self.assertEqual(current["blueprint"]["untrusted_data_fields"], ["semantic"])
        resume = gateway.project_open("proj_blueprint")
        self.assertIsNotNone(resume["creative_blueprint_head"])
        self.assertTrue(resume["creative_blueprint_head"]["canonical_persisted_head"])
        self.assertEqual(
            resume["selection"]["policy"],
            "explicit_blueprint_head_exact_revision_digest_operation_join",
        )
        self.assertTrue(resume["selection"]["authoritative_project_head"])
        self.assertEqual(
            resume["next_actions"][:2],
            ["blueprint-get", "blueprint-history"],
        )
        codes = {item["code"] for item in resume["gaps"]}
        self.assertNotIn("creative_blueprint_unavailable", codes)
        self.assertIn("blueprint_user_authority_unverified", codes)

        tools = {item["name"]: item for item in TOOLS}
        for name in ("memolens_blueprint_get", "memolens_blueprint_history"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            self.assertFalse(tools[name]["annotations"]["openWorldHint"])
        blueprint_created_at = tools["memolens_blueprint_get"]["outputSchema"][
            "properties"
        ]["blueprint"]["anyOf"][0]["properties"]["created_at"]
        self.assertEqual(blueprint_created_at["maxLength"], 64)

        env = os.environ.copy()
        env[TRUST_LOCAL_API_ENV] = "0"
        source_before = self.fixture.db.read_bytes()
        cli = subprocess.run(
            [
                sys.executable,
                str(CLI_PATH),
                "--db",
                str(self.fixture.db),
                "blueprint-get",
                "proj_blueprint",
                "--revision",
                "1",
            ],
            cwd=self.fixture.root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(cli.stderr, "")
        self.assertEqual(json.loads(cli.stdout)["blueprint"], exact["blueprint"])
        self.assertEqual(self.fixture.db.read_bytes(), source_before)

    def test_history_includes_no_change_but_never_semantic_or_private_operation_data(self) -> None:
        self.fixture.insert_no_change()
        history = self.fixture.gateway().blueprint_history("proj_blueprint", limit=50)
        self.assertEqual([item["result_kind"] for item in history["operations"]], ["no_change", "revision_created"])
        self.assertFalse(history["complete_project_history"])
        rendered = json.dumps(history, ensure_ascii=False, sort_keys=True)
        for secret in (SECRET_SCRIPT, SECRET_REFERENCE, SECRET_PATH, str(self.fixture.db)):
            self.assertNotIn(secret, rendered)
        for forbidden in (
            "semantic",
            "locator",
            "actor_json",
            "origin_json",
            "precondition_json",
            "typed_diff_json",
            "input_sha256",
            "result_json",
            "idempotency_key",
        ):
            self.assertNotIn(f'"{forbidden}"', rendered)

    def test_corrupt_current_head_fails_closed_for_all_read_surfaces(self) -> None:
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE creative_blueprint_heads SET content_sha256=? WHERE project_id='proj_blueprint'",
                ("f" * 64,),
            )
            connection.commit()
        gateway = self.fixture.gateway()
        for read in (
            lambda: gateway.blueprint_get("proj_blueprint"),
            lambda: gateway.blueprint_get("proj_blueprint", revision=1),
            lambda: gateway.blueprint_history("proj_blueprint"),
            lambda: gateway.project_open("proj_blueprint"),
        ):
            with self.assertRaises(MemoLensError) as raised:
                read()
            self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_head_cannot_be_rewound_to_hide_a_later_valid_revision(self) -> None:
        self.fixture.insert_second_revision()
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            first = connection.execute(
                "SELECT revision,content_sha256,semantic_sha256,operation_id "
                "FROM creative_blueprint_revisions WHERE project_id='proj_blueprint' "
                "AND revision=1"
            ).fetchone()
            assert first is not None
            connection.execute(
                "UPDATE creative_blueprint_heads SET revision=?,content_sha256=?,"
                "semantic_sha256=?,operation_id=? WHERE project_id='proj_blueprint'",
                first,
            )
            connection.commit()

        gateway = self.fixture.gateway()
        for read in (
            lambda: gateway.blueprint_get("proj_blueprint"),
            lambda: gateway.blueprint_get("proj_blueprint", revision=1),
            lambda: gateway.blueprint_history("proj_blueprint"),
            lambda: gateway.project_open("proj_blueprint"),
        ):
            with self.assertRaises(MemoLensError) as raised:
                read()
            self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_forged_no_change_diff_and_private_timestamp_fail_closed(self) -> None:
        self.fixture.insert_no_change()
        forged_diff = [
            {
                "section": "script",
                "change": "modified",
                "before_sha256": "1" * 64,
                "after_sha256": "2" * 64,
            }
        ]
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            result = json.loads(
                connection.execute(
                    "SELECT result_json FROM creative_blueprint_operations WHERE id='op_blueprint_2'"
                ).fetchone()[0]
            )
            result["changed_sections"] = ["script"]
            connection.execute(
                "UPDATE creative_blueprint_operations SET typed_diff_json=?,result_json=? "
                "WHERE id='op_blueprint_2'",
                (
                    self.fixture._canonical(forged_diff),
                    self.fixture._canonical(result),
                ),
            )
            connection.commit()
        gateway = self.fixture.gateway()
        for read in (
            lambda: gateway.blueprint_get("proj_blueprint"),
            lambda: gateway.blueprint_history("proj_blueprint"),
            lambda: gateway.project_open("proj_blueprint"),
        ):
            with self.assertRaises(MemoLensError) as raised:
                read()
            self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

        self.fixture.close()
        self.fixture = PersistedBlueprintFixture()
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE creative_blueprint_operations SET created_at=? WHERE id='op_blueprint_1'",
                (SECRET_PATH,),
            )
            connection.commit()
        with self.assertRaises(MemoLensError) as raised:
            self.fixture.gateway().blueprint_history("proj_blueprint")
        self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_current_read_validates_every_ancestor_revision(self) -> None:
        self.fixture.insert_second_revision()
        self.fixture.insert_third_revision()
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE creative_blueprint_revisions SET blueprint_json='{}' "
                "WHERE project_id='proj_blueprint' AND revision=1"
            )
            connection.commit()
        gateway = self.fixture.gateway()
        for read in (
            lambda: gateway.blueprint_get("proj_blueprint"),
            lambda: gateway.blueprint_get("proj_blueprint", revision=3),
            lambda: gateway.blueprint_history("proj_blueprint"),
            lambda: gateway.project_open("proj_blueprint"),
        ):
            with self.assertRaises(MemoLensError) as raised:
                read()
            self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_oversized_stored_text_is_bounded_before_python_projection(self) -> None:
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE creative_blueprint_revisions SET blueprint_json=? "
                "WHERE project_id='proj_blueprint' AND revision=1",
                ("x" * (17 * 1024 * 1024),),
            )
            connection.commit()
        with mock.patch.object(
            persisted_blueprint,
            "_decode_document",
            side_effect=AssertionError("document decoder must not run"),
        ):
            with self.assertRaises(MemoLensError) as raised:
                self.fixture.gateway().blueprint_get("proj_blueprint")
        self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_legal_per_value_operation_sizes_have_no_narrower_aggregate_cap(self) -> None:
        padding = "x" * 220_000
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE creative_blueprint_operations SET actor_json=?,origin_json=?,"
                "precondition_json=?,typed_diff_json=?,result_json=? "
                "WHERE id='op_blueprint_1'",
                (
                    self.fixture._canonical({"padding": padding}),
                    self.fixture._canonical({"padding": padding}),
                    self.fixture._canonical({"padding": padding}),
                    self.fixture._canonical([padding]),
                    self.fixture._canonical({"padding": padding}),
                ),
            )
            connection.commit()

        with self.assertRaisesRegex(AssertionError, "operation decoder ran"):
            with mock.patch.object(
                persisted_blueprint,
                "_strict_object",
                side_effect=AssertionError("operation decoder ran"),
            ):
                self.fixture.gateway().blueprint_get("proj_blueprint")

    def test_project_aggregate_budget_accumulates_across_rows_before_decode(self) -> None:
        self.fixture.insert_no_change()
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            row_totals = [
                sum(row)
                for row in connection.execute(
                    "SELECT length(CAST(actor_json AS BLOB)),"
                    "length(CAST(origin_json AS BLOB)),"
                    "length(CAST(precondition_json AS BLOB)),"
                    "length(CAST(typed_diff_json AS BLOB)),"
                    "length(CAST(result_json AS BLOB)) "
                    "FROM creative_blueprint_operations "
                    "WHERE project_id='proj_blueprint' ORDER BY sequence"
                )
            ]
        self.assertEqual(len(row_totals), 2)
        aggregate_budget = max(row_totals)
        self.assertTrue(all(total <= aggregate_budget for total in row_totals))
        self.assertGreater(sum(row_totals), aggregate_budget)

        with (
            mock.patch.object(
                persisted_blueprint,
                "_OPERATION_PROJECT_STORED_JSON_MAX_BYTES",
                aggregate_budget,
            ),
            mock.patch.object(
                persisted_blueprint,
                "_strict_object",
                side_effect=AssertionError("operation decoder must not run"),
            ),
        ):
            with self.assertRaises(MemoLensError) as raised:
                self.fixture.gateway().blueprint_get("proj_blueprint")
        self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_stored_json_preflight_row_and_byte_boundaries(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE payloads (body TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO payloads VALUES (?)",
                [("a" * 8,), ("b" * 8,)],
            )
            reader = self.fixture.gateway()._store.persisted_blueprints
            arguments = {
                "from_clause": "payloads p",
                "where_clause": "1=1",
                "parameters": (),
                "columns": ("p.body",),
                "per_value_max_bytes": 8,
                "order_by": " ORDER BY rowid",
            }
            self.assertEqual(
                reader._assert_stored_json_budget(
                    connection,
                    **arguments,
                    max_rows=2,
                    aggregate_max_bytes=16,
                ),
                2,
            )
            for limits in (
                {"max_rows": 1, "aggregate_max_bytes": 16},
                {"max_rows": 2, "aggregate_max_bytes": 15},
            ):
                with self.assertRaises(MemoLensError) as raised:
                    reader._assert_stored_json_budget(
                        connection,
                        **arguments,
                        **limits,
                    )
                self.assertEqual(raised.exception.code, "blueprint_integrity_failed")

    def test_validated_revision_chain_keeps_only_bounded_decoded_bundles(self) -> None:
        self.fixture.insert_second_revision()
        self.fixture.insert_third_revision()
        reader = self.fixture.gateway()._store.persisted_blueprints
        with closing(reader.database.connection()) as connection:
            current = reader._current_bundle(connection, "proj_blueprint")
            assert current is not None
            revision_chain = current[-1]
            for revision in revision_chain:
                self.assertEqual(revision_chain[revision][0]["revision"], revision)
                self.assertLessEqual(
                    len(revision_chain._cache),
                    persisted_blueprint._REVISION_BUNDLE_CACHE_SIZE,
                )
            self.assertEqual(
                len(revision_chain._cache),
                persisted_blueprint._REVISION_BUNDLE_CACHE_SIZE,
            )

    def test_10k_restore_stream_retains_at_most_64_revision_identities(
        self,
    ) -> None:
        revision_count = 10_000
        project_id = "proj_synthetic_restore_stream"
        stats = {
            "live_rows": 0,
            "live_documents": 0,
            "live_digests": 0,
            "peak_retained": 0,
            "decoded_documents": 0,
            "created_digests": 0,
        }

        def changed(kind: str, amount: int) -> None:
            key = f"live_{kind}"
            stats[key] += amount
            stats["peak_retained"] = max(
                stats["peak_retained"],
                stats["live_rows"]
                + stats["live_documents"]
                + stats["live_digests"],
            )

        class TrackedRow(dict[str, Any]):
            def __init__(self, value: dict[str, Any]) -> None:
                super().__init__(value)
                changed("rows", 1)

            def __del__(self) -> None:
                changed("rows", -1)

        class TrackedDocument(dict[str, Any]):
            def __init__(self, value: dict[str, Any]) -> None:
                super().__init__(value)
                changed("documents", 1)

            def __del__(self) -> None:
                changed("documents", -1)

        class TrackedDigest(str):
            def __new__(cls, value: str) -> "TrackedDigest":
                instance = str.__new__(cls, value)
                stats["created_digests"] += 1
                changed("digests", 1)
                return instance

            def __del__(self) -> None:
                changed("digests", -1)

        def content_sha256(revision: int) -> str:
            return f"{revision:064x}"

        def rows() -> Any:
            for revision in range(1, revision_count + 1):
                restore = revision > 1
                yield TrackedRow(
                    {
                        "r_project_id": project_id,
                        "r_revision": revision,
                        "r_content_sha256": content_sha256(revision),
                        "r_semantic_sha256": "a" * 64,
                        "r_operation_id": f"op_{revision}",
                        "o_result_kind": (
                            "revision_restored" if restore else "revision_created"
                        ),
                        "t_revision": 1 if restore else None,
                        "t_content_sha256": (
                            content_sha256(1) if restore else None
                        ),
                    }
                )

        class SyntheticConnection:
            execute_count = 0

            def execute(self, statement: str, parameters: object) -> Any:
                del parameters
                self.execute_count += 1
                normalized = " ".join(statement.lower().split())
                self_test.assertIn(
                    "left join creative_blueprint_revisions t",
                    normalized,
                )
                self_test.assertIn(
                    "json_extract(o.result_json,'$.restore_from.revision')",
                    normalized,
                )
                return rows()

        def decode_document(row: dict[str, Any]) -> TrackedDocument:
            stats["decoded_documents"] += 1
            revision = int(row["revision"])
            return TrackedDocument(
                {
                    "parent": (
                        None
                        if revision == 1
                        else {
                            "revision": revision - 1,
                            "content_sha256": content_sha256(revision - 1),
                        }
                    ),
                    "semantic": {"script": "same restored material"},
                    "evidence_manifest": [],
                    "lineage": {"source_candidate_sha256": None},
                }
            )

        def validate_operation(
            _operation_row: dict[str, Any],
            revision_row: dict[str, Any],
            *,
            expected_typed_diff: list[dict[str, Any]] | None = None,
        ) -> dict[str, Any]:
            self.assertEqual(expected_typed_diff, [])
            revision = int(revision_row["revision"])
            return {
                "restore_from": (
                    None
                    if revision == 1
                    else {
                        "revision": 1,
                        "content_sha256": content_sha256(1),
                    }
                )
            }

        self_test = self
        connection = SyntheticConnection()
        reader = self.fixture.gateway()._store.persisted_blueprints
        real_row_with_prefix = (
            persisted_blueprint.PersistedBlueprintReader._row_with_prefix
        )

        def tracked_row_with_prefix(
            row: sqlite3.Row,
            prefix: str,
        ) -> TrackedRow:
            return TrackedRow(real_row_with_prefix(row, prefix))

        with (
            mock.patch.object(
                persisted_blueprint.PersistedBlueprintReader,
                "_row_with_prefix",
                new=staticmethod(tracked_row_with_prefix),
            ),
            mock.patch.object(
                persisted_blueprint,
                "_decode_document",
                new=decode_document,
            ),
            mock.patch.object(
                persisted_blueprint,
                "_semantic_diff",
                new=lambda _before, _after: [],
            ),
            mock.patch.object(
                persisted_blueprint,
                "_validate_operation",
                new=validate_operation,
            ),
            mock.patch.object(
                persisted_blueprint,
                "_restore_material_sha256",
                new=lambda _document: TrackedDigest("b" * 64),
            ),
        ):
            revision_chain = reader._validate_revision_chain(
                connection,  # type: ignore[arg-type]
                project_id,
                {
                    "project_id": project_id,
                    "revision": revision_count,
                    "content_sha256": content_sha256(revision_count),
                    "semantic_sha256": "a" * 64,
                    "operation_id": f"op_{revision_count}",
                },
            )

        self.assertEqual(connection.execute_count, 1)
        self.assertEqual(stats["decoded_documents"], 2 * revision_count - 1)
        self.assertEqual(stats["created_digests"], 2 * (revision_count - 1))
        type(self).synthetic_peak_retained = stats["peak_retained"]
        self.assertLessEqual(stats["peak_retained"], 64)
        self.assertLessEqual(
            len(revision_chain._cache),
            persisted_blueprint._REVISION_BUNDLE_CACHE_SIZE,
        )
        self.assertFalse(
            any(
                isinstance(value, list)
                for value in revision_chain.__dict__.values()
            )
        )

    def test_project_without_head_preserves_legacy_gap_and_never_uses_max_revision(self) -> None:
        gateway = self.fixture.gateway()
        result = gateway.blueprint_get("proj_legacy")
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["blueprint"])
        resume = gateway.project_open("proj_legacy")
        self.assertIsNone(resume["creative_blueprint_head"])
        self.assertIn("creative_blueprint_unavailable", {item["code"] for item in resume["gaps"]})


if __name__ == "__main__":
    unittest.main()
