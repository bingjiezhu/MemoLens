from __future__ import annotations

from copy import deepcopy
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
TESTS = PLUGIN_ROOT / "tests"
for search_path in (SCRIPTS, TESTS):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

import memolens_canonical_editor  # noqa: E402
import memolens_editor_server  # noqa: E402
import memolens_mcp  # noqa: E402
from memolens_agent_client import canonical_sha256  # noqa: E402
from memolens_agent_credentials import save_agent_credential  # noqa: E402
from memolens_canonical_editor import (  # noqa: E402
    CanonicalEditorSnapshot,
    canonical_editor_snapshot,
)
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_editor_server import (  # noqa: E402
    CANONICAL_EDITOR_ACTIONS,
    CANONICAL_EDITOR_EDIT_ACTIONS,
    EDITOR_COOKIE,
    LEGACY_EDITOR_ACTIONS,
    EditorServerManager,
)
from memolens_timeline import draft_timeline  # noqa: E402
from test_canonical_editor_handoff import _raw_snapshot  # noqa: E402


DATABASE_UUID = "db-11111111-2222-4333-8444-555555555555"


def _legacy_timeline(project_id: str) -> dict:
    return draft_timeline(
        project_id=project_id,
        items=[
            {
                "kind": "video",
                "asset_id": f"asset_{project_id}",
                "asset_source_id": f"src_{project_id}",
                "asset_sha256": "a" * 64,
                "segment_id": f"seg_{project_id}",
                "analysis_run_id": f"analysis_{project_id}",
                "analysis_revision": 1,
                "source_in_ms": 1000,
                "source_out_ms": 4000,
                "timeline_duration_ms": 3000,
                "reason": "Session scope fixture",
                "match_id": f"match_{project_id}",
            }
        ],
        created_at="2026-08-29T00:00:00Z",
    )


def _canonical_snapshot_for(project_id: str) -> CanonicalEditorSnapshot:
    project, coverage, timeline_workspace = _raw_snapshot(1)
    project_row = project["project"]
    coverage_plan = coverage["coverage_plan"]
    timeline = timeline_workspace["timeline"]

    project_row["id"] = project_id
    project_row["project_id"] = project_id
    coverage["project_id"] = project_id
    coverage_plan["project_id"] = project_id
    timeline_workspace["project_id"] = project_id
    timeline["project_id"] = project_id

    timeline_id = f"timeline_{project_id}"
    timeline["timeline_id"] = timeline_id
    for head in (
        project_row["timeline"]["head"],
        timeline_workspace["head"],
        timeline_workspace["selected_revision"],
    ):
        head["timeline_id"] = timeline_id

    coverage_content = canonical_sha256(coverage_plan)
    for selected in (
        coverage["head"],
        coverage["selected_revision"],
        project_row["coverage"]["head"],
    ):
        selected["content_sha256"] = coverage_content
    coverage_binding = timeline["coverage_binding"]
    coverage_binding["content_sha256"] = coverage_content
    for head in (
        project_row["timeline"]["head"],
        timeline_workspace["head"],
        timeline_workspace["selected_revision"],
    ):
        head["coverage_binding"]["content_sha256"] = coverage_content

    timeline_content = canonical_sha256(timeline)
    for head in (
        project_row["timeline"]["head"],
        timeline_workspace["head"],
        timeline_workspace["selected_revision"],
    ):
        head["timeline_content_sha256"] = timeline_content

    return canonical_editor_snapshot(
        project_id=project_id,
        credential_database_uuid=DATABASE_UUID,
        project_response=project,
        coverage_response=coverage,
        timeline_response=timeline_workspace,
    )


class _ScopedBackend:
    can_edit = True
    can_restore = True

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.snapshot = _canonical_snapshot_for(project_id)
        self.read_calls = 0
        self.write_calls = 0

    def read(self) -> CanonicalEditorSnapshot:
        self.read_calls += 1
        return self.snapshot

    def read_revision(self, _revision: int, *, current: CanonicalEditorSnapshot):
        del current
        raise AssertionError("cross-session request reached canonical history")

    def save(self, *, snapshot: CanonicalEditorSnapshot, edit: dict):
        del snapshot, edit
        self.write_calls += 1
        raise AssertionError("cross-session request reached canonical save")

    def save_restore(self, *, snapshot: CanonicalEditorSnapshot, historical):
        del snapshot, historical
        self.write_calls += 1
        raise AssertionError("cross-session request reached canonical restore")


def _handoff_origin_and_boot(handoff: dict) -> tuple[str, str]:
    parsed = urlsplit(handoff["editor_url"])
    return (
        f"{parsed.scheme}://{parsed.netloc}",
        parse_qs(parsed.fragment)["boot"][0],
    )


def _post_json(opener, url: str, payload: dict, *, origin: str) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    with opener.open(request) as response:
        if response.status != 200:
            raise AssertionError(f"unexpected HTTP status: {response.status}")
        return json.load(response)


def _bootstrap(handoff: dict) -> tuple[str, str, dict]:
    origin, boot = _handoff_origin_and_boot(handoff)
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    state = _post_json(
        opener,
        f"{origin}/api/sessions/{handoff['session_id']}/bootstrap",
        {"token": boot},
        origin=origin,
    )
    cookies = [cookie for cookie in jar if cookie.name == EDITOR_COOKIE]
    if len(cookies) != 1:
        raise AssertionError("bootstrap did not issue exactly one editor cookie")
    return origin, cookies[0].value, state


def _post_denied(
    url: str,
    payload: dict,
    *,
    origin: str,
    cookie_token: str | None,
) -> tuple[int, dict]:
    headers = {
        "Content-Type": "application/json",
        "Origin": origin,
    }
    if cookie_token is not None:
        headers["Cookie"] = f"{EDITOR_COOKIE}={cookie_token}"
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        build_opener().open(request)
    except HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)
    raise AssertionError("cross-session editor request was unexpectedly admitted")


class ProductionScopeInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )

    def tearDown(self) -> None:
        self.manager.close()

    def test_cross_session_cookie_denies_every_editor_action_before_dispatch_and_zero_mutation(
        self,
    ) -> None:
        legacy_source = self.manager.create_handoff(
            timeline=_legacy_timeline("proj_legacy_source")
        )
        legacy_target = self.manager.create_handoff(
            timeline=_legacy_timeline("proj_legacy_target")
        )
        origin, source_cookie, _source_state = _bootstrap(legacy_source)
        target_origin, target_cookie, _target_state = _bootstrap(legacy_target)
        self.assertEqual(origin, target_origin)
        self.assertNotEqual(source_cookie, target_cookie)

        legacy_source_session = self.manager._sessions[legacy_source["session_id"]]
        legacy_target_session = self.manager._sessions[legacy_target["session_id"]]
        legacy_before = deepcopy(
            {
                "source": legacy_source_session.public(),
                "target": legacy_target_session.public(),
            }
        )
        legacy_payloads = {
            "plugin_editor_action.legacy.apply": {
                "type": "apply",
                "operations": [],
                "created_at": "2026-08-29T00:00:01Z",
            },
            "plugin_editor_action.legacy.undo": {"type": "undo"},
            "plugin_editor_action.legacy.redo": {"type": "redo"},
        }
        with patch.object(
            self.manager,
            "_apply_legacy_action",
            side_effect=AssertionError("cross-session legacy dispatch was reached"),
        ) as legacy_dispatch:
            missing_status, missing_body = _post_denied(
                f"{origin}/api/sessions/{legacy_target['session_id']}/actions",
                legacy_payloads["plugin_editor_action.legacy.apply"],
                origin=origin,
                cookie_token=None,
            )
            self.assertEqual(missing_status, 401)
            self.assertEqual(
                missing_body["error"]["code"], "editor_session_unauthorized"
            )
            for action_id, payload in legacy_payloads.items():
                with self.subTest(action_id=action_id):
                    status, body = _post_denied(
                        f"{origin}/api/sessions/{legacy_target['session_id']}/actions",
                        payload,
                        origin=origin,
                        cookie_token=source_cookie,
                    )
                    self.assertEqual(status, 401)
                    self.assertEqual(
                        body["error"]["code"], "editor_session_unauthorized"
                    )
            legacy_dispatch.assert_not_called()
        self.assertEqual(
            {
                "source": legacy_source_session.public(),
                "target": legacy_target_session.public(),
            },
            legacy_before,
        )

        source_backend = _ScopedBackend("proj_canonical_source")
        target_backend = _ScopedBackend("proj_canonical_target")
        canonical_source = self.manager.create_canonical_handoff(
            backend=source_backend
        )
        canonical_target = self.manager.create_canonical_handoff(
            backend=target_backend
        )
        canonical_origin, canonical_source_cookie, source_state = _bootstrap(
            canonical_source
        )
        canonical_target_origin, canonical_target_cookie, target_state = _bootstrap(
            canonical_target
        )
        self.assertEqual(canonical_origin, canonical_target_origin)
        self.assertNotEqual(canonical_source_cookie, canonical_target_cookie)
        self.assertEqual(source_state["projection"]["project_id"], "proj_canonical_source")
        self.assertEqual(target_state["projection"]["project_id"], "proj_canonical_target")
        clip_id = target_state["projection"]["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]
        canonical_payloads = {
            "plugin_editor_action.canonical.stage": {
                "type": "stage",
                "edit": {"op": "unsupported"},
            },
            "plugin_editor_action.canonical.stage_structural": {
                "type": "stage_structural",
                "structural_edit": {"op": "unsupported"},
            },
            "plugin_editor_action.canonical.edit.move_clip": {
                "type": "stage",
                "edit": {"op": "move_clip", "clip_id": clip_id, "to_index": 0},
            },
            "plugin_editor_action.canonical.edit.trim_clip": {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            "plugin_editor_action.canonical.edit.set_clip_duration": {
                "type": "stage",
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip_id,
                    "duration_ms": 1200,
                },
            },
            "plugin_editor_action.canonical.edit.replace_clip": {
                "type": "stage",
                "edit": {
                    "op": "replace_clip",
                    "clip_id": clip_id,
                    "assignment_id": "assign_image",
                },
            },
            "plugin_editor_action.canonical.select_revision": {
                "type": "select_revision",
                "revision": 1,
            },
            "plugin_editor_action.canonical.stage_restore": {"type": "stage_restore"},
            "plugin_editor_action.canonical.discard": {"type": "discard"},
            "plugin_editor_action.canonical.refresh": {"type": "refresh"},
            "plugin_editor_action.canonical.save": {"type": "save"},
        }
        expected_actions = {
            *(f"plugin_editor_action.legacy.{name}" for name in LEGACY_EDITOR_ACTIONS),
            *(
                f"plugin_editor_action.canonical.{name}"
                for name in CANONICAL_EDITOR_ACTIONS
            ),
            *(
                f"plugin_editor_action.canonical.edit.{name}"
                for name in CANONICAL_EDITOR_EDIT_ACTIONS
            ),
        }
        self.assertEqual(
            set(legacy_payloads) | set(canonical_payloads),
            expected_actions,
        )
        canonical_sessions_before = deepcopy(
            {
                "source": self.manager._sessions[
                    canonical_source["session_id"]
                ].public(),
                "target": self.manager._sessions[
                    canonical_target["session_id"]
                ].public(),
            }
        )
        read_calls_before = (source_backend.read_calls, target_backend.read_calls)
        with patch.object(
            self.manager,
            "_apply_canonical_action",
            side_effect=AssertionError("cross-session canonical dispatch was reached"),
        ) as canonical_dispatch:
            for action_id, payload in canonical_payloads.items():
                with self.subTest(action_id=action_id):
                    status, body = _post_denied(
                        f"{canonical_origin}/api/sessions/{canonical_target['session_id']}/actions",
                        payload,
                        origin=canonical_origin,
                        cookie_token=canonical_source_cookie,
                    )
                    self.assertEqual(status, 401)
                    self.assertEqual(
                        body["error"]["code"], "editor_session_unauthorized"
                    )
            canonical_dispatch.assert_not_called()
        self.assertEqual(
            {
                "source": self.manager._sessions[
                    canonical_source["session_id"]
                ].public(),
                "target": self.manager._sessions[
                    canonical_target["session_id"]
                ].public(),
            },
            canonical_sessions_before,
        )
        self.assertEqual(
            (source_backend.read_calls, target_backend.read_calls),
            read_calls_before,
        )
        self.assertEqual(source_backend.write_calls, 0)
        self.assertEqual(target_backend.write_calls, 0)

    def test_canonical_mcp_handoff_denies_missing_and_cross_project_pairing_before_session_creation(
        self,
    ) -> None:
        paired_project = "proj_paired"
        requested_project = "proj_requested"
        with TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {"MEMOLENS_APP_STATE_DIR": temporary},
                clear=False,
            ):
                with (
                    patch.object(
                        memolens_canonical_editor,
                        "AgentPairingWorkflow",
                        side_effect=AssertionError(
                            "pairing transport ran before project-bound credential admission"
                        ),
                    ) as workflow,
                    patch.object(
                        memolens_editor_server,
                        "get_editor_server_manager",
                        side_effect=AssertionError(
                            "editor session was created without project-bound pairing"
                        ),
                    ) as manager,
                ):
                    with self.assertRaises(MemoLensError) as missing:
                        memolens_mcp.call_tool(
                            "memolens_canonical_editor_handoff",
                            {"project_id": requested_project},
                            object(),
                        )
                    self.assertEqual(missing.exception.code, "agent_pairing_required")

                    save_agent_credential(
                        {
                            "object": "memolens.agent_project_credential",
                            "schema_version": "1",
                            "base_url": "http://127.0.0.1:65535",
                            "project_id": paired_project,
                            "pairing_id": "pair_scope_oracle",
                            "capability_id": "cap_scope_oracle",
                            "subject_id": "subject_scope_oracle",
                            "claimed_client_label": "Scope oracle",
                            "proof_secret": "f" * 64,
                            "database_uuid": DATABASE_UUID,
                            "actions": ["timeline.apply_edit"],
                            "created_at": "2026-08-29T00:00:00Z",
                            "expires_at": "2026-08-29T01:00:00Z",
                            "status": "active",
                        }
                    )
                    credential_directory = (
                        Path(temporary) / "agent-credentials"
                    )
                    source = next(credential_directory.glob("project-*.json"))
                    requested_digest = hashlib.sha256(
                        requested_project.encode("utf-8")
                    ).hexdigest()[:32]
                    forged_target = credential_directory / (
                        f"project-{requested_digest}.json"
                    )
                    shutil.copyfile(source, forged_target)
                    forged_target.chmod(0o600)

                    with self.assertRaises(MemoLensError) as cross_project:
                        memolens_mcp.call_tool(
                            "memolens_canonical_editor_handoff",
                            {"project_id": requested_project},
                            object(),
                        )
                    self.assertEqual(
                        cross_project.exception.code,
                        "agent_credential_invalid",
                    )
                    workflow.assert_not_called()
                    manager.assert_not_called()

    def test_legacy_mcp_handoff_accepts_only_closed_input_and_creates_a_new_process_draft(
        self,
    ) -> None:
        timeline = _legacy_timeline("proj_caller_value")
        with patch.object(
            memolens_editor_server,
            "get_editor_server_manager",
            return_value=self.manager,
        ):
            result = memolens_mcp.call_tool(
                "memolens_editor_handoff",
                {"timeline": timeline},
                object(),
            )
            self.assertEqual(result["persistence"], "not_saved")
            self.assertTrue(result["safety"]["timeline_persisted"] is False)
            session = self.manager._sessions[result["session_id"]]
            self.assertEqual(session.timeline, timeline)
            self.assertIsNot(session.timeline, timeline)
            self.assertEqual(len(self.manager._sessions), 1)

            for redirected in (
                {
                    "timeline": timeline,
                    "session_id": result["session_id"],
                },
                {
                    "timeline": timeline,
                    "project_id": "proj_existing_target",
                },
            ):
                with self.subTest(fields=sorted(redirected)):
                    with self.assertRaises(MemoLensError) as denied:
                        memolens_mcp.call_tool(
                            "memolens_editor_handoff",
                            redirected,
                            object(),
                        )
                    self.assertEqual(denied.exception.code, "invalid_argument")
                    self.assertEqual(len(self.manager._sessions), 1)
                    self.assertEqual(session.timeline, timeline)


if __name__ == "__main__":
    unittest.main()
