"""Controlled shared-Browser vertical oracle for B2B4C structural edits.

This module drives the real Core/Flask ledger, production paired Agent client,
``PairedCanonicalEditorBackend``, ``EditorServerManager`` loopback server, and
the one canonical editor page shared by the Codex and DeepSeek plugin wiring.
It is controlled local evidence only: no real model, Codex host, DeepSeek host,
or human native-gesture journey is claimed.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from http.cookiejar import CookieJar
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from werkzeug.serving import WSGIRequestHandler, make_server

from core.media_db import canonical_json
from tests import test_timeline_lowering_integration as timeline_integration_fixture
from tests import test_timeline_structural_edit_backend as structural_fixture
from tests.test_coverage_persistence import semantic


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / ".agents" / "plugins" / "plugins" / "memolens"
PLUGIN_SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

from memolens_agent_client import AgentApiClient  # noqa: E402
from memolens_agent_receipts import validate_agent_receipt_ledger  # noqa: E402
from memolens_canonical_editor import PairedCanonicalEditorBackend  # noqa: E402
from memolens_editor_server import (  # noqa: E402
    CANONICAL_EDITOR_HTML_PATH,
    EditorServerManager,
)


STRUCTURAL_ACTION = "timeline.apply_structural_edit"
TIMELINE_HEAD_FIELDS = (
    "revision",
    "revision_sha256",
    "timeline_id",
    "timeline_content_sha256",
    "blueprint_binding",
    "coverage_binding",
    "operation_id",
)


class _SilentRequestHandler(WSGIRequestHandler):
    def log(self, type: str, message: str, *args: object) -> None:  # noqa: A002
        del type, message, args


def _timeline_head(value: dict[str, object]) -> dict[str, object]:
    return {field: deepcopy(value[field]) for field in TIMELINE_HEAD_FIELDS}


class B2B4CSharedEditorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = structural_fixture.TimelineStructuralEditBackendTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

        (
            self.project_id,
            self.materialize_payload,
            self.initial_workspace,
        ) = self._build_video_project()
        self.initial_head = _timeline_head(self.initial_workspace["head"])
        self.fixture.project_id = self.project_id
        self.capability_id = self.fixture._pair(
            actions=[STRUCTURAL_ACTION],
            max_operations=2,
        )

        self.authority_server = make_server(
            "127.0.0.1",
            0,
            self.fixture.app,
            threaded=True,
            request_handler=_SilentRequestHandler,
        )
        self.authority_thread = threading.Thread(
            target=self.authority_server.serve_forever,
            name="b2b4c-shared-editor-authority",
            daemon=True,
        )
        self.authority_thread.start()
        self.addCleanup(self._close_authority_server)

        self.authority_origin = (
            f"http://127.0.0.1:{self.authority_server.server_port}"
        )
        self.agent_client = AgentApiClient(self.authority_origin, timeout=5.0)
        health = self.agent_client.health()
        self.assertEqual(health["service"], "memolens-backend")
        self.credential = {
            "base_url": self.authority_origin,
            "database_uuid": self.fixture.repository.database_uuid,
            "project_id": self.project_id,
            "capability_id": self.capability_id,
            "proof_secret": structural_fixture.SECRET,
            "actions": [STRUCTURAL_ACTION],
            "status": "active",
        }
        self.backend = PairedCanonicalEditorBackend(
            project_id=self.project_id,
            credential=self.credential,
            client=self.agent_client,
        )
        self.manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )
        self.addCleanup(self.manager.close)

    def _close_authority_server(self) -> None:
        self.authority_server.shutdown()
        self.authority_server.server_close()
        self.authority_thread.join(timeout=5.0)
        self.assertFalse(self.authority_thread.is_alive())

    def _build_video_project(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        fixture = self.fixture
        span_ref = (
            timeline_integration_fixture.TimelineLoweringIntegrationTests
            ._register_video_span(fixture)
        )
        ending = fixture._register_image(
            "b2b4c-shared-editor-ending.jpg",
            b"b2b4c-shared-editor-ending",
        )
        proposed = semantic("b2b4c-shared-editor", evidence_ref=span_ref)
        proposed["material_hints"][0] = {
            "hint_id": "opening_video",
            "evidence_ref": span_ref,
            "script_block_ids": ["opening"],
            "reason": "Use the exact verified video span for the opening.",
        }
        proposed["material_hints"].append(
            {
                "hint_id": "ending_image",
                "evidence_ref": f"memolens://evidence/asset/{ending['id']}",
                "script_block_ids": ["ending"],
                "reason": "Use the exact verified image for the ending.",
            }
        )
        project = fixture.repository.create_project(
            "B2B4C shared Browser structural editor",
            {
                "goal": "controlled shared editor integration",
                "duration_ms": 8_000,
                "aspect_ratio": "9:16",
                "candidate_refs": [],
            },
            {"created_by": "b2b4c-shared-editor-test"},
        )
        project_id = str(project["id"])
        brief = fixture.repository.get_brief(project_id, 1)
        assert brief is not None
        fixture.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key=f"b2b4c-shared-blueprint-{project_id}",
        )
        blueprint = fixture.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        expected_blueprint = {
            field: blueprint[field]
            for field in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        fixture.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    field: expected_blueprint[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key=f"b2b4c-shared-coverage-{project_id}",
            expected_database_uuid=fixture.repository.database_uuid,
        )
        coverage = fixture.repository.get_coverage_head(project_id)
        assert coverage is not None
        payload = {
            "expected_blueprint": expected_blueprint,
            "expected_coverage": {
                field: coverage[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": None,
        }
        materialized = fixture.timelines.materialize_first_cut(
            project_id,
            payload,
            idempotency_key=f"b2b4c-shared-timeline-{project_id}",
            expected_database_uuid=fixture.repository.database_uuid,
        )
        self.assertEqual(materialized.response_status, 201)
        workspace = fixture.timelines.read(project_id)
        self.assertEqual(workspace["head"]["revision"], 1)
        self.assertEqual(workspace["timeline"]["schema_version"], "1")
        self.assertEqual(
            [
                clip["media_kind"]
                for clip in workspace["timeline"]["tracks"][0]["clips"]
            ],
            ["video", "image"],
        )
        return project_id, payload, workspace

    @staticmethod
    def _post_json(opener, url: str, payload: dict[str, object], origin: str):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": origin},
            method="POST",
        )
        with opener.open(request) as response:
            return response.status, json.load(response)

    def _open_editor(self):
        handoff = self.manager.create_canonical_handoff(backend=self.backend)
        parsed = urlsplit(handoff["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        page_url = parsed._replace(fragment="").geturl()
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        with opener.open(page_url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertEqual(response.read(), CANONICAL_EDITOR_HTML_PATH.read_bytes())
        boot = parse_qs(parsed.fragment)["boot"][0]
        status, state = self._post_json(
            opener,
            f"{origin}/api/sessions/{handoff['session_id']}/bootstrap",
            {"token": boot},
            origin,
        )
        self.assertEqual(status, 200)
        return handoff, origin, opener, state

    def _ledger_cardinality(self) -> dict[str, object]:
        with closing(self.fixture.repository._connect()) as connection:
            counts = {
                "operations": connection.execute(
                    "SELECT COUNT(*) FROM canonical_timeline_operations WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0],
                "revisions": connection.execute(
                    "SELECT COUNT(*) FROM canonical_timeline_revisions WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0],
                "desktop_receipts": connection.execute(
                    "SELECT COUNT(*) FROM canonical_timeline_receipts WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0],
                "paired_receipts": connection.execute(
                    "SELECT COUNT(*) FROM agent_project_command_receipts WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0],
                "capability_events": connection.execute(
                    "SELECT COUNT(*) FROM agent_project_capability_events WHERE capability_id=?",
                    (self.capability_id,),
                ).fetchone()[0],
                "used_events": connection.execute(
                    """SELECT COUNT(*) FROM agent_project_capability_events
                        WHERE capability_id=? AND event_type='used'""",
                    (self.capability_id,),
                ).fetchone()[0],
            }
        capability = self.fixture.repository.get_agent_project_capability(
            self.capability_id,
            runtime_authority_epoch=self.fixture.epoch,
        )
        assert capability is not None
        counts["operations_used"] = capability["operations_used"]
        counts["capability_status"] = capability["status"]
        return counts

    def _asset_source_rows(self) -> tuple[tuple[object, ...], ...]:
        with closing(self.fixture.repository._connect()) as connection:
            return tuple(
                tuple(row)
                for row in connection.execute("SELECT * FROM asset_sources ORDER BY id")
            )

    def _assert_canonical_reread(
        self,
        *,
        expected_head: dict[str, object],
        expected_revision: int,
        session_id: str,
    ) -> dict[str, object]:
        agent_workspace = self.agent_client.timeline_workspace(self.project_id)
        core_workspace = self.fixture.timelines.read(self.project_id)
        self.assertEqual(agent_workspace, core_workspace)
        self.assertEqual(_timeline_head(agent_workspace["head"]), expected_head)
        self.assertEqual(agent_workspace["timeline"]["revision"], expected_revision)
        session = self.manager._sessions[session_id]
        self.assertEqual(session.snapshot.expected_timeline_head, expected_head)
        self.assertEqual(session.snapshot.public["timeline"], agent_workspace["timeline"])
        return agent_workspace

    def _assert_shared_host_wiring(self) -> None:
        codex_mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text())
        self.assertEqual(
            codex_mcp["mcpServers"]["memolens"]["args"],
            ["./scripts/memolens_mcp.py"],
        )
        deepseek_bundle = (PLUGIN_ROOT / "index.js").read_text()
        self.assertIn(
            "mcpScript: join(root, 'scripts', 'memolens_mcp.py')",
            deepseek_bundle,
        )
        self.assertEqual(
            list(PLUGIN_ROOT.rglob("canonical-editor.html")),
            [CANONICAL_EDITOR_HTML_PATH],
        )

    def test_shared_browser_split_then_delete_commits_exact_paired_chain(self) -> None:
        self._assert_shared_host_wiring()
        initial_counts = {
            "operations": 1,
            "revisions": 1,
            "desktop_receipts": 1,
            "paired_receipts": 0,
            "capability_events": 1,
            "used_events": 0,
            "operations_used": 0,
            "capability_status": "active",
        }
        self.assertEqual(self._ledger_cardinality(), initial_counts)
        original_sources = self._asset_source_rows()
        original_video = (self.fixture.library / "ending.mp4").read_bytes()

        handoff, origin, opener, state = self._open_editor()
        session_id = str(handoff["session_id"])
        endpoint = f"{origin}/api/sessions/{session_id}/actions"
        self.assertEqual(handoff["authority"]["actions"], [STRUCTURAL_ACTION])
        self.assertEqual(state["authority"]["actions"], [STRUCTURAL_ACTION])
        self.assertTrue(state["capabilities"]["split_clip"])
        self.assertTrue(state["capabilities"]["delete_clip"])
        self.assertEqual(
            state["projection"]["timeline_head"],
            {
                "revision": self.initial_head["revision"],
                "timeline_id": self.initial_head["timeline_id"],
            },
        )

        initial_clips = state["projection"]["timeline"]["tracks"][0]["clips"]
        initial_clip_ids = {str(clip["clip_id"]) for clip in initial_clips}
        video = next(clip for clip in initial_clips if clip["media_kind"] == "video")
        split_edit = {
            "op": "split_clip",
            "clip_id": video["clip_id"],
            "source_split_ms": (
                int(video["source_in_ms"])
                + (int(video["source_out_ms"]) - int(video["source_in_ms"])) // 2
            ),
        }
        split_stage_status, split_staged = self._post_json(
            opener,
            endpoint,
            {"type": "stage_structural", "structural_edit": split_edit},
            origin,
        )
        self.assertEqual(split_stage_status, 200)
        self.assertEqual(split_staged["pending"]["structural_edit"], split_edit)
        self.assertIs(split_staged["pending"]["canonical"], False)
        self.assertIs(split_staged["pending"]["persisted"], False)
        self.assertEqual(self._ledger_cardinality(), initial_counts)
        self.assertEqual(
            _timeline_head(self.agent_client.timeline_workspace(self.project_id)["head"]),
            self.initial_head,
        )

        split_save_status, split_saved = self._post_json(
            opener,
            endpoint,
            {"type": "save"},
            origin,
        )
        self.assertEqual(split_save_status, 200)
        self.assertEqual(
            split_saved["last_save"],
            {
                "status": "saved",
                "command_type": STRUCTURAL_ACTION,
                "revision": 2,
            },
        )
        self.assertIsNone(split_saved["pending"])
        first_internal_save = deepcopy(self.manager._sessions[session_id].last_save)
        assert first_internal_save is not None
        first_head = _timeline_head(first_internal_save["result_head"])
        self.assertEqual(first_internal_save["operation_id"], first_head["operation_id"])
        first_workspace = self._assert_canonical_reread(
            expected_head=first_head,
            expected_revision=2,
            session_id=session_id,
        )
        self.assertEqual(first_workspace["timeline"]["schema_version"], "2")
        first_clips = first_workspace["timeline"]["tracks"][0]["clips"]
        child_clips = [
            clip for clip in first_clips if str(clip["clip_id"]) not in initial_clip_ids
        ]
        self.assertEqual(len(child_clips), 2)
        self.assertEqual(child_clips[0]["source_out_ms"], split_edit["source_split_ms"])
        self.assertEqual(child_clips[1]["source_in_ms"], split_edit["source_split_ms"])
        after_split_counts = {
            **initial_counts,
            "operations": 2,
            "revisions": 2,
            "paired_receipts": 1,
            "capability_events": 2,
            "used_events": 1,
            "operations_used": 1,
        }
        self.assertEqual(self._ledger_cardinality(), after_split_counts)

        delete_edit = {
            "op": "delete_clip",
            "clip_id": child_clips[0]["clip_id"],
        }
        delete_stage_status, delete_staged = self._post_json(
            opener,
            endpoint,
            {"type": "stage_structural", "structural_edit": delete_edit},
            origin,
        )
        self.assertEqual(delete_stage_status, 200)
        self.assertEqual(delete_staged["pending"]["structural_edit"], delete_edit)
        self.assertIs(delete_staged["pending"]["canonical"], False)
        self.assertIs(delete_staged["pending"]["persisted"], False)
        self.assertEqual(self._ledger_cardinality(), after_split_counts)
        self.assertEqual(
            _timeline_head(self.agent_client.timeline_workspace(self.project_id)["head"]),
            first_head,
        )

        delete_save_status, delete_saved = self._post_json(
            opener,
            endpoint,
            {"type": "save"},
            origin,
        )
        self.assertEqual(delete_save_status, 200)
        self.assertEqual(
            delete_saved["last_save"],
            {
                "status": "saved",
                "command_type": STRUCTURAL_ACTION,
                "revision": 3,
            },
        )
        self.assertIsNone(delete_saved["pending"])
        second_internal_save = deepcopy(self.manager._sessions[session_id].last_save)
        assert second_internal_save is not None
        second_head = _timeline_head(second_internal_save["result_head"])
        self.assertEqual(second_internal_save["operation_id"], second_head["operation_id"])
        final_workspace = self._assert_canonical_reread(
            expected_head=second_head,
            expected_revision=3,
            session_id=session_id,
        )
        self.assertEqual(final_workspace["timeline"]["schema_version"], "2")
        final_clip_ids = [
            str(clip["clip_id"])
            for clip in final_workspace["timeline"]["tracks"][0]["clips"]
        ]
        self.assertNotIn(str(video["clip_id"]), final_clip_ids)
        self.assertNotIn(str(delete_edit["clip_id"]), final_clip_ids)
        self.assertIn(str(child_clips[1]["clip_id"]), final_clip_ids)
        self.assertEqual(len(final_clip_ids), len(initial_clip_ids))

        final_counts = {
            **after_split_counts,
            "operations": 3,
            "revisions": 3,
            "paired_receipts": 2,
            "capability_events": 3,
            "used_events": 2,
            "operations_used": 2,
            "capability_status": "exhausted",
        }
        self.assertEqual(self._ledger_cardinality(), final_counts)
        self.assertEqual(self._asset_source_rows(), original_sources)
        self.assertEqual((self.fixture.library / "ending.mp4").read_bytes(), original_video)

        with closing(self.fixture.repository._connect()) as connection:
            operations = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM canonical_timeline_operations
                        WHERE project_id=? ORDER BY sequence""",
                    (self.project_id,),
                )
            ]
            revisions = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM canonical_timeline_revisions
                        WHERE project_id=? ORDER BY revision""",
                    (self.project_id,),
                )
            ]
            paired_receipts = [
                dict(row)
                for row in connection.execute(
                    """SELECT receipt.* FROM agent_project_command_receipts receipt
                        JOIN canonical_timeline_operations operation
                          ON operation.id=receipt.operation_id
                       WHERE receipt.project_id=? ORDER BY operation.sequence""",
                    (self.project_id,),
                )
            ]
            events = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM agent_project_capability_events
                        WHERE capability_id=? ORDER BY sequence""",
                    (self.capability_id,),
                )
            ]
            validation = validate_agent_receipt_ledger(
                connection,
                project_id=self.project_id,
            )
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()

        operation_ids = [
            str(self.initial_head["operation_id"]),
            str(first_head["operation_id"]),
            str(second_head["operation_id"]),
        ]
        self.assertEqual([row["sequence"] for row in operations], [1, 2, 3])
        self.assertEqual([row["id"] for row in operations], operation_ids)
        self.assertEqual(
            [row["parent_operation_id"] for row in operations],
            [None, operation_ids[0], operation_ids[1]],
        )
        self.assertEqual(
            [row["command_type"] for row in operations],
            ["timeline.materialize_first_cut", STRUCTURAL_ACTION, STRUCTURAL_ACTION],
        )
        self.assertEqual([row["result_revision"] for row in operations], [1, 2, 3])
        self.assertEqual(
            [json.loads(row["expected_timeline_head_json"]) for row in operations],
            [None, self.initial_head, first_head],
        )
        self.assertEqual([row["revision"] for row in revisions], [1, 2, 3])
        self.assertEqual([row["operation_id"] for row in revisions], operation_ids)
        self.assertEqual([row["parent_revision"] for row in revisions], [None, 1, 2])
        self.assertEqual(
            [row["parent_revision_sha256"] for row in revisions],
            [None, revisions[0]["revision_sha256"], revisions[1]["revision_sha256"]],
        )
        self.assertEqual(
            [row["schema_version"] for row in revisions],
            ["1", "2", "2"],
        )
        self.assertEqual(
            [json.loads(row["result_json"])["structural_edit"] for row in operations[1:]],
            [split_edit, delete_edit],
        )
        self.assertEqual(
            _timeline_head(
                self.fixture.repository.get_canonical_timeline_head(self.project_id)
            ),
            second_head,
        )

        expected_requests = (
            (self.initial_head, split_edit),
            (first_head, delete_edit),
        )
        for receipt, (expected_head, structural_edit), operation_id in zip(
            paired_receipts,
            expected_requests,
            operation_ids[1:],
            strict=True,
        ):
            request_value = json.loads(receipt["request_json"])
            response_value = json.loads(receipt["response_json"])
            self.assertEqual(
                request_value,
                {
                    "object": "memolens.timeline_apply_structural_edit_command",
                    "schema_version": "1",
                    "project_id": self.project_id,
                    "expected_database_uuid": self.fixture.repository.database_uuid,
                    "expected_blueprint": self.materialize_payload[
                        "expected_blueprint"
                    ],
                    "expected_coverage": self.materialize_payload[
                        "expected_coverage"
                    ],
                    "expected_timeline_head": expected_head,
                    "structural_edit": structural_edit,
                },
            )
            self.assertEqual(receipt["operation_id"], operation_id)
            self.assertEqual(receipt["command_type"], STRUCTURAL_ACTION)
            self.assertEqual(receipt["authenticated_principal"], "paired_agent")
            self.assertEqual(receipt["capability_id"], self.capability_id)
            self.assertEqual(receipt["paired_subject_id"], "structural_0")
            self.assertEqual(
                receipt["claimed_client_label"],
                "Codex or DeepSeek canonical editor",
            )
            self.assertEqual(receipt["receipt_schema_version"], "2")
            self.assertEqual(receipt["request_capture"], "exact_canonical_json")
            self.assertEqual(receipt["resource_type"], "canonical_timeline")
            self.assertEqual(receipt["response_status"], 201)
            self.assertEqual(response_value["operation_id"], operation_id)
            self.assertEqual(response_value["command_type"], STRUCTURAL_ACTION)
            self.assertEqual(response_value["result"]["structural_edit"], structural_edit)
            self.assertEqual(canonical_json(request_value), receipt["request_json"])
            self.assertEqual(
                hashlib.sha256(receipt["request_json"].encode()).hexdigest(),
                receipt["request_sha256"],
            )
            self.assertEqual(
                hashlib.sha256(receipt["response_json"].encode()).hexdigest(),
                receipt["response_sha256"],
            )

        self.assertEqual([row["sequence"] for row in events], [1, 2, 3])
        self.assertEqual(
            [row["event_type"] for row in events],
            ["issued", "used", "used"],
        )
        self.assertEqual(
            [row["action"] for row in events],
            [None, STRUCTURAL_ACTION, STRUCTURAL_ACTION],
        )
        self.assertEqual(
            [row["operation_id"] for row in events],
            [None, operation_ids[1], operation_ids[2]],
        )
        self.assertEqual(
            [row["agent_project_command_receipt_id"] for row in events[1:]],
            [row["id"] for row in paired_receipts],
        )
        self.assertEqual(
            [row["parent_event_id"] for row in events],
            [None, events[0]["id"], events[1]["id"]],
        )
        self.assertEqual(
            [row["parent_event_sha256"] for row in events],
            [None, events[0]["event_sha256"], events[1]["event_sha256"]],
        )
        self.assertTrue(validation["validated"])
        self.assertEqual(foreign_keys, [])


if __name__ == "__main__":
    unittest.main()
