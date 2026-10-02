from __future__ import annotations

import copy
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.src import create_app, shutdown_runtime_extensions
from backend.src.media.render_plan import build_render_plan
from backend.src.media.video import MediaCapabilityError, MediaJobRunner
from core.config import Settings
from core.production_surface_inventory import (
    INVENTORY_PATH,
    ProductionSurfaceInventoryError,
    UnknownProductionActionError,
    assert_surface_set_equality,
    canonical_inventory_sha256,
    declared_action_ids,
    discover_flask_actions,
    discover_mcp_actions,
    discover_plugin_editor_actions,
    discover_plugin_editor_http_actions,
    discover_python_worker_actions,
    high_impact_oracle_gaps,
    load_production_surface_inventory,
    negative_oracle_action_claims,
    parse_inventory_json,
    require_declared_action,
    validate_inventory,
)
from scripts.run_production_oracles import (
    OracleExecutionError,
    _assert_python_result,
    _exact_node_pattern,
    _parse_tap_summary,
    _run_node_oracle,
    _select_unique_python_test,
)


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = ROOT / ".agents" / "plugins" / "plugins" / "memolens" / "scripts"
PLUGIN_TESTS = PLUGIN_SCRIPTS.parent / "tests"
SCHEMA_PATH = ROOT / "core" / "schemas" / "production-surface-inventory-v1.schema.json"


class ProductionSurfaceInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="memolens-surface-inventory-")
        root = Path(cls.temporary.name).resolve()
        library = root / "library"
        library.mkdir()
        cls.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                "IMAGE_LIBRARY_DIR": str(library),
                "SQLITE_DB_PATH": str(root / "state" / "inventory.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": "inventory-desktop-token",
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": "inventory-main-token",
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        cls.environment.start()
        cls.app = create_app(Settings.from_env())

    @classmethod
    def tearDownClass(cls) -> None:
        shutdown_runtime_extensions(cls.app.extensions)
        cls.environment.stop()
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.inventory = load_production_surface_inventory()

    def test_frozen_inventory_is_closed_sorted_unique_and_hash_bound(self) -> None:
        self.assertEqual(
            self.inventory["inventory_sha256"],
            canonical_inventory_sha256(self.inventory),
        )
        self.assertEqual(
            [row["id"] for row in self.inventory["actions"]],
            sorted(row["id"] for row in self.inventory["actions"]),
        )
        for row in self.inventory["actions"]:
            for field in ("authority", "effects", "negative_tests", "scope"):
                self.assertEqual(row[field], sorted(set(row[field])))

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["negativeOracle"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["actionClaims"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["action"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["network"]["additionalProperties"])
        self.assertFalse(
            schema["$defs"]["surfaceVerification"]["additionalProperties"]
        )
        self.assertEqual(
            set(schema["$defs"]["effect"]["enum"]),
            {
                "egress",
                "filesystem-read",
                "filesystem-write",
                "mutation",
                "process-start",
                "read",
            },
        )

    def test_negative_oracles_are_hash_bound_resolvable_and_fully_referenced(self) -> None:
        oracle_ids = [row["id"] for row in self.inventory["negative_oracles"]]
        self.assertEqual(oracle_ids, sorted(set(oracle_ids)))
        referenced = {
            oracle_id
            for action in self.inventory["actions"]
            for oracle_id in action["negative_tests"]
        }
        self.assertEqual(set(oracle_ids), referenced)
        action_references = {
            oracle_id: {
                action["id"]
                for action in self.inventory["actions"]
                if oracle_id in action["negative_tests"]
            }
            for oracle_id in oracle_ids
        }
        for row in self.inventory["negative_oracles"]:
            with self.subTest(oracle=row["id"]):
                self.assertNotIn("pending", row["id"].lower())
                path = ROOT / row["path"]
                self.assertTrue(path.is_file(), f"negative oracle file is missing: {path}")
                self.assertIn(
                    row["selector"],
                    path.read_text(encoding="utf-8"),
                    f"negative oracle selector is missing: {row['id']}",
                )
                claimed_actions = [
                    action_claim["action_id"]
                    for action_claim in row["action_claims"]
                ]
                self.assertEqual(claimed_actions, sorted(set(claimed_actions)))
                self.assertEqual(set(claimed_actions), action_references[row["id"]])
                for action_claim in row["action_claims"]:
                    self.assertEqual(
                        action_claim["claims"],
                        sorted(set(action_claim["claims"])),
                    )
                    self.assertEqual(
                        action_claim["authority_denials"],
                        sorted(set(action_claim["authority_denials"])),
                    )
                    self.assertEqual(
                        action_claim["scope_denials"],
                        sorted(set(action_claim["scope_denials"])),
                    )

    def test_action_claims_are_bidirectional_and_cannot_inherit_global_proof(self) -> None:
        claims = negative_oracle_action_claims(self.inventory)
        for action in self.inventory["actions"]:
            for oracle_id in action["negative_tests"]:
                with self.subTest(action=action["id"], oracle=oracle_id):
                    self.assertIn(action["id"], claims[oracle_id])

        target = next(
            action
            for action in self.inventory["actions"]
            if "mutation" in action["effects"] and action["authority"] != ["none"]
        )
        spoofed = copy.deepcopy(self.inventory)
        denied_authority = target["authority"][0]
        for oracle in spoofed["negative_oracles"]:
            for action_claim in oracle["action_claims"]:
                if action_claim["action_id"] != target["id"]:
                    continue
                action_claim["authority_denials"] = [
                    authority
                    for authority in action_claim["authority_denials"]
                    if authority != denied_authority
                ]
                if not action_claim["authority_denials"]:
                    remaining = [
                        claim
                        for claim in action_claim["claims"]
                        if claim != "authority-deny"
                    ]
                    action_claim["claims"] = remaining or ["surface-equality"]
        self.assertIn(
            f"authority-deny:{denied_authority}",
            high_impact_oracle_gaps(spoofed)[target["id"]],
        )

        scope_spoofed = copy.deepcopy(self.inventory)
        denied_scope = target["scope"][0]
        for oracle in scope_spoofed["negative_oracles"]:
            for action_claim in oracle["action_claims"]:
                if action_claim["action_id"] != target["id"]:
                    continue
                action_claim["scope_denials"] = [
                    scope
                    for scope in action_claim["scope_denials"]
                    if scope != denied_scope
                ]
                if not action_claim["scope_denials"]:
                    remaining = [
                        claim
                        for claim in action_claim["claims"]
                        if claim != "scope-deny"
                    ]
                    action_claim["claims"] = remaining or ["surface-equality"]
        self.assertIn(
            f"scope-deny:{denied_scope}",
            high_impact_oracle_gaps(scope_spoofed)[target["id"]],
        )

    def test_every_high_impact_action_has_authority_scope_and_offline_oracles(self) -> None:
        self.assertEqual(high_impact_oracle_gaps(self.inventory), {})

    def test_filesystem_writes_are_not_collapsed_into_generic_mutation(self) -> None:
        by_id = {row["id"]: row for row in self.inventory["actions"]}
        for action_id in (
            "electron_ipc.memolens:save-settings",
            "flask_http.POST:/v1/assets/import",
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/edit",
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/structural-edit",
            "photon_bot.discord.send_attachments",
            "plugin_editor_action.canonical.save",
            "python_worker.canonical_export.package",
            "python_worker.media.video_index",
            "render_profile.export-1080p",
            "render_profile.preview-low",
        ):
            with self.subTest(action_id=action_id):
                self.assertIn("filesystem-write", by_id[action_id]["effects"])

        for process_scoped_action in (
            "plugin_editor_action.canonical.stage",
            "plugin_editor_action.canonical.stage_structural",
            "plugin_editor_action.legacy.apply",
            "plugin_editor_http.POST:/api/sessions/<session_id>/bootstrap",
        ):
            with self.subTest(action_id=process_scoped_action):
                self.assertNotIn(
                    "filesystem-write", by_id[process_scoped_action]["effects"]
                )

    def test_parser_rejects_duplicate_keys_non_finite_and_unknown_fields(self) -> None:
        with self.assertRaises(ProductionSurfaceInventoryError):
            parse_inventory_json('{"schema_version":"a","schema_version":"b"}')
        with self.assertRaises(ProductionSurfaceInventoryError):
            parse_inventory_json('{"value":NaN}')

        inflated = dict(self.inventory)
        inflated["undeclared_default"] = True
        inflated["inventory_sha256"] = canonical_inventory_sha256(inflated)
        with self.assertRaises(ProductionSurfaceInventoryError):
            validate_inventory(inflated)

    def test_hash_and_sorted_set_tampering_fail_closed(self) -> None:
        tampered = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        tampered["actions"][0]["scope"].append(tampered["actions"][0]["scope"][0])
        tampered["inventory_sha256"] = canonical_inventory_sha256(tampered)
        with self.assertRaises(ProductionSurfaceInventoryError):
            validate_inventory(tampered)

        hash_mismatch = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        hash_mismatch["actions"][0]["contract_version"] = "future"
        with self.assertRaisesRegex(ProductionSurfaceInventoryError, "SHA-256"):
            validate_inventory(hash_mismatch)

    def test_resource_scope_cannot_repeat_transport_or_action_identity(self) -> None:
        for fake_scope in (
            "GET:/v1/future",
            "POST:/v1/future",
            "ipc:memolens:future",
            "tool:memolens_future",
            "bot:discord.future",
            "render-profile:future",
            "media-job:future",
        ):
            with self.subTest(scope=fake_scope):
                tampered = copy.deepcopy(self.inventory)
                tampered["actions"][0]["scope"] = [fake_scope]
                tampered["inventory_sha256"] = canonical_inventory_sha256(
                    tampered
                )
                with self.assertRaisesRegex(
                    ProductionSurfaceInventoryError,
                    "action.scope",
                ):
                    validate_inventory(tampered)

    def test_action_claim_backlink_and_unknown_action_tampering_fail_closed(self) -> None:
        missing = copy.deepcopy(self.inventory)
        oracle = next(
            row
            for row in missing["negative_oracles"]
            if len(row["action_claims"]) > 1
        )
        oracle["action_claims"].pop()
        missing["inventory_sha256"] = canonical_inventory_sha256(missing)
        with self.assertRaisesRegex(
            ProductionSurfaceInventoryError, "action coverage drifted"
        ):
            validate_inventory(missing)

        unknown = copy.deepcopy(self.inventory)
        unknown["negative_oracles"][0]["action_claims"].append(
            {
                "action_id": "future_surface.undeclared_action",
                "authority_denials": [],
                "claims": ["scope-deny"],
                "scope_denials": ["future-scope"],
            }
        )
        unknown["negative_oracles"][0]["action_claims"].sort(
            key=lambda row: row["action_id"]
        )
        unknown["inventory_sha256"] = canonical_inventory_sha256(unknown)
        with self.assertRaisesRegex(
            ProductionSurfaceInventoryError, "undeclared production actions"
        ):
            validate_inventory(unknown)

        unbound_authority = copy.deepcopy(self.inventory)
        proof = unbound_authority["negative_oracles"][0]["action_claims"][0]
        proof["authority_denials"] = []
        if "authority-deny" not in proof["claims"]:
            proof["claims"].append("authority-deny")
            proof["claims"].sort()
        unbound_authority["inventory_sha256"] = canonical_inventory_sha256(
            unbound_authority
        )
        with self.assertRaisesRegex(
            ProductionSurfaceInventoryError,
            "bind authority-deny",
        ):
            validate_inventory(unbound_authority)

    def test_exact_oracle_runner_rejects_zero_ambiguous_and_skipped_python_tests(
        self,
    ) -> None:
        class Fixture(unittest.TestCase):
            def test_exact_oracle(self) -> None:
                return

            @unittest.skip("must fail the production oracle gate")
            def test_skipped_oracle(self) -> None:
                return

        with self.assertRaisesRegex(OracleExecutionError, "matches=0"):
            _select_unique_python_test(
                unittest.TestSuite([Fixture("test_exact_oracle")]),
                "test_exact",
            )
        with self.assertRaisesRegex(OracleExecutionError, "matches=2"):
            _select_unique_python_test(
                unittest.TestSuite(
                    [Fixture("test_exact_oracle"), Fixture("test_exact_oracle")]
                ),
                "test_exact_oracle",
            )

        skipped = unittest.TextTestRunner(stream=StringIO()).run(
            unittest.TestSuite([Fixture("test_skipped_oracle")])
        )
        with self.assertRaisesRegex(OracleExecutionError, "skipped.*1"):
            _assert_python_result(skipped)

    def test_exact_node_oracle_runner_rejects_generalized_zero_match_and_skip(
        self,
    ) -> None:
        self.assertEqual(
            _exact_node_pattern("literal [selector]"),
            r"^(?:literal \[selector\])$",
        )
        with self.assertRaises(OracleExecutionError):
            _parse_tap_summary(
                "\n".join(
                    (
                        "# tests 0",
                        "# pass 0",
                        "# fail 0",
                        "# cancelled 0",
                        "# skipped 0",
                        "# todo 0",
                    )
                ),
                selector="authority denial",
            )

        with tempfile.TemporaryDirectory(prefix="memolens-exact-node-oracle-") as raw:
            root = Path(raw)
            test_path = root / "fixture.test.mjs"
            test_path.write_text(
                "import test from 'node:test';\n"
                "test('specific authority denial', () => {});\n"
                "test.skip('explicitly skipped denial', () => {});\n",
                encoding="utf-8",
            )
            with self.assertRaises(OracleExecutionError):
                _run_node_oracle(
                    {
                        "path": test_path.name,
                        "runner": "node-test",
                        "selector": "authority denial",
                    },
                    repo_root=root,
                    echo_output=False,
                )
            with self.assertRaises(OracleExecutionError):
                _run_node_oracle(
                    {
                        "path": test_path.name,
                        "runner": "node-test",
                        "selector": "explicitly skipped denial",
                    },
                    repo_root=root,
                    echo_output=False,
                )

    def test_create_app_flask_rules_match_manifest_in_both_directions(self) -> None:
        discovered = discover_flask_actions(self.app)
        self.assertNotIn("flask_http.HEAD:/healthz", discovered)
        self.assertNotIn("flask_http.OPTIONS:/healthz", discovered)
        self.assertNotIn("flask_http.GET:/static/<path:filename>", discovered)
        self.assertIn(
            "flask_http.POST:/v1/agent/creative/projects/<project_id>/timeline/preview-leases",
            discovered,
        )
        self.assertIn(
            "flask_http.GET:/v1/agent/preview-leases/<lease_id>/clips/<clip_id>/media",
            discovered,
        )
        self.assertNotIn(
            "flask_http.HEAD:/v1/agent/preview-leases/<lease_id>/clips/<clip_id>/media",
            discovered,
        )
        assert_surface_set_equality(
            self.inventory,
            surface="flask_http",
            discovered=discovered,
        )

    def test_mcp_tools_match_manifest_in_both_directions(self) -> None:
        sys.path.insert(0, str(PLUGIN_SCRIPTS))
        try:
            import memolens_mcp

            discovered = discover_mcp_actions(memolens_mcp.TOOLS)
        finally:
            if sys.path[0] == str(PLUGIN_SCRIPTS):
                sys.path.pop(0)
        assert_surface_set_equality(
            self.inventory,
            surface="mcp_tool",
            discovered=discovered,
        )

    def test_mcp_unknown_tool_fails_closed(self) -> None:
        sys.path.insert(0, str(PLUGIN_SCRIPTS))
        try:
            import memolens_mcp

            with self.assertRaises(memolens_mcp.MemoLensError) as captured:
                memolens_mcp.call_tool("memolens_future_magic", {}, object())
        finally:
            if sys.path[0] == str(PLUGIN_SCRIPTS):
                sys.path.pop(0)
        self.assertEqual(captured.exception.code, "unknown_tool")

    def test_plugin_editor_http_and_inner_actions_match_manifest(self) -> None:
        editor_server = PLUGIN_SCRIPTS / "memolens_editor_server.py"
        canonical_editor = PLUGIN_SCRIPTS / "memolens_canonical_editor.py"
        discovered_http = discover_plugin_editor_http_actions(editor_server)
        self.assertIn(
            "plugin_editor_http.GET:/api/sessions/<session_id>/clip-media/<clip_id>",
            discovered_http,
        )
        self.assertIn(
            "plugin_editor_http.HEAD:/api/sessions/<session_id>/clip-media/<clip_id>",
            discovered_http,
        )
        assert_surface_set_equality(
            self.inventory,
            surface="plugin_editor_http",
            discovered=discovered_http,
        )
        assert_surface_set_equality(
            self.inventory,
            surface="plugin_editor_action",
            discovered=discover_plugin_editor_actions(
                editor_server_path=editor_server,
                canonical_editor_path=canonical_editor,
            ),
        )

    def test_plugin_structural_stage_requires_matching_pairing_without_mutation(
        self,
    ) -> None:
        inserted: list[str] = []
        for search_path in (str(PLUGIN_SCRIPTS), str(PLUGIN_TESTS)):
            if search_path not in sys.path:
                sys.path.insert(0, search_path)
                inserted.append(search_path)
        try:
            from memolens_canonical_editor import PairedCanonicalEditorBackend
            from memolens_editor_server import EditorServerError, EditorServerManager
            from test_canonical_editor_handoff import _raw_snapshot

            project, coverage, timeline = _raw_snapshot(1)

            class Client:
                def __init__(self) -> None:
                    self.write_attempts = 0

                def project_workspace(self, project_id: str) -> dict[str, object]:
                    if project_id != "proj_canonical":
                        raise AssertionError("project scope changed")
                    return copy.deepcopy(project)

                def coverage_workspace(self, project_id: str) -> dict[str, object]:
                    if project_id != "proj_canonical":
                        raise AssertionError("project scope changed")
                    return copy.deepcopy(coverage)

                def timeline_workspace(
                    self,
                    project_id: str,
                    revision: int | None = None,
                ) -> dict[str, object]:
                    if project_id != "proj_canonical" or revision is not None:
                        raise AssertionError("Timeline scope changed")
                    return copy.deepcopy(timeline)

                def timeline_write(self, **_kwargs: object) -> dict[str, object]:
                    self.write_attempts += 1
                    raise AssertionError("unpaired structural stage reached a write")

            client = Client()
            backend = PairedCanonicalEditorBackend(
                project_id="proj_canonical",
                credential={
                    "database_uuid": "db-11111111-2222-4333-8444-555555555555",
                    "actions": ["timeline.apply_edit"],
                },
                client=client,
            )
            manager = EditorServerManager(
                session_ttl_seconds=60,
                boot_ttl_seconds=30,
            )
            try:
                handoff = manager.create_canonical_handoff(backend=backend)
                session = manager._sessions[handoff["session_id"]]
                before = copy.deepcopy(session.public())
                clip_id = str(
                    before["projection"]["timeline"]["tracks"][0]["clips"][0][
                        "clip_id"
                    ]
                )
                with self.assertRaises(EditorServerError) as captured:
                    manager._apply_canonical_action(
                        session,
                        {
                            "type": "stage_structural",
                            "structural_edit": {
                                "op": "delete_clip",
                                "clip_id": clip_id,
                            },
                        },
                    )
                self.assertEqual(captured.exception.code, "agent_pairing_scope_denied")
                self.assertEqual(captured.exception.status, 403)
                self.assertEqual(session.public(), before)
                self.assertEqual(client.write_attempts, 0)
            finally:
                manager.close()
        finally:
            for search_path in inserted:
                if search_path in sys.path:
                    sys.path.remove(search_path)

    def test_python_worker_and_render_registries_match_manifest(self) -> None:
        discovered = discover_python_worker_actions()
        for surface, actions in discovered.items():
            assert_surface_set_equality(
                self.inventory,
                surface=surface,
                discovered=actions,
            )

    def test_injected_unknown_action_and_set_drift_fail_closed(self) -> None:
        with self.assertRaises(UnknownProductionActionError):
            require_declared_action(self.inventory, "mcp_tool.memolens_future_magic")
        with self.assertRaisesRegex(
            ProductionSurfaceInventoryError, "undeclared_runtime"
        ):
            assert_surface_set_equality(
                self.inventory,
                surface="mcp_tool",
                discovered=[
                    *declared_action_ids(self.inventory, surface="mcp_tool"),
                    "mcp_tool.memolens_future_magic",
                ],
            )

    def test_render_profile_unknown_is_rejected_before_defaulting_to_export(self) -> None:
        with self.assertRaises(MediaCapabilityError) as captured:
            build_render_plan({}, "future-huge-export")
        self.assertEqual(captured.exception.code, "unknown_render_profile")

    def test_media_job_runner_marks_unknown_kind_failed(self) -> None:
        class Repository:
            def __init__(self) -> None:
                self.database_uuid = "inventory-unknown-kind-db"
                self.update: dict[str, object] | None = None

            def get_media_job(self, job_id: str) -> dict[str, object]:
                return {
                    "id": job_id,
                    "database_uuid": self.database_uuid,
                    "kind": "future_media_job",
                    "status": "queued",
                    "cancel_requested": False,
                }

            def update_media_job(self, job_id: str, **fields: object) -> None:
                self.update = {"job_id": job_id, **fields}

        repository = Repository()
        runner = object.__new__(MediaJobRunner)
        runner.repository = repository
        runner._run("job_unknown")
        self.assertIsNotNone(repository.update)
        assert repository.update is not None
        self.assertEqual(repository.update["status"], "failed")
        self.assertEqual(
            repository.update["error"],
            {
                "code": "unknown_media_job_kind",
                "message": "The media job kind is not registered for production dispatch.",
            },
        )

    def test_electron_and_photon_rows_use_automatic_external_gates(self) -> None:
        verification = {
            row["surface"]: row for row in self.inventory["surface_verification"]
        }
        for surface in ("electron_ipc", "photon_bot"):
            self.assertEqual(verification[surface]["mode"], "automatic")
            self.assertEqual(verification[surface]["status"], "enforced")
            self.assertTrue(declared_action_ids(self.inventory, surface=surface))


if __name__ == "__main__":
    unittest.main()
