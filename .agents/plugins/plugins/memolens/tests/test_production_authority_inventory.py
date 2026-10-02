from __future__ import annotations

from copy import deepcopy
from http.cookiejar import CookieJar
from pathlib import Path
import json
import sys
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[5]
SCRIPTS = PLUGIN_ROOT / "scripts"
TESTS = PLUGIN_ROOT / "tests"
for search_path in (REPO_ROOT, SCRIPTS, TESTS):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

from core.production_surface_inventory import (  # noqa: E402
    discover_plugin_editor_actions,
)
from memolens_agent_client import AgentApiError  # noqa: E402
from memolens_canonical_editor import PairedCanonicalEditorBackend  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_editor_server import (  # noqa: E402
    CANONICAL_EDITOR_ACTIONS,
    CANONICAL_EDITOR_EDIT_ACTIONS,
    EDITOR_COOKIE,
    LEGACY_EDITOR_ACTIONS,
    EditorServerManager,
)
import memolens_mcp  # noqa: E402
from memolens_mcp import TOOL_ARGUMENT_FIELDS, TOOLS  # noqa: E402
from memolens_timeline import draft_timeline  # noqa: E402
from test_canonical_editor_handoff import (  # noqa: E402
    _Backend as _FixtureBackend,
    _raw_snapshot,
    _snapshot,
)


DATABASE_UUID = "db-11111111-2222-4333-8444-555555555555"


def _legacy_timeline() -> dict:
    return draft_timeline(
        project_id="proj_production_authority",
        items=[
            {
                "kind": "video",
                "asset_id": "asset_video",
                "asset_source_id": "src_video",
                "asset_sha256": "a" * 64,
                "segment_id": "seg_current",
                "analysis_run_id": "analysis_current",
                "analysis_revision": 1,
                "source_in_ms": 1000,
                "source_out_ms": 4000,
                "timeline_duration_ms": 3000,
                "reason": "Authority fixture",
                "match_id": "seg_current",
            }
        ],
        created_at="2026-08-29T00:00:00Z",
    )


def _origin_and_boot(handoff: dict) -> tuple[str, str]:
    parsed = urlsplit(handoff["editor_url"])
    return (
        f"{parsed.scheme}://{parsed.netloc}",
        parse_qs(parsed.fragment)["boot"][0],
    )


def _post_json(opener, url: str, payload: dict, *, origin: str) -> tuple[int, dict]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    with opener.open(request) as response:
        return response.status, json.load(response)


def _post_error(
    test: unittest.TestCase,
    opener,
    url: str,
    payload: dict,
    *,
    origin: str,
    cookie: str | None = None,
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "Origin": origin}
    if cookie is not None:
        headers["Cookie"] = cookie
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        opener.open(request)
    except HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)
    test.fail("Expected the editor request to fail closed.")
    raise AssertionError("unreachable")


class _DispatchReached(RuntimeError):
    pass


class _GatewayTrap:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def reached(*_args, **_kwargs):
            self.calls.append(name)
            raise _DispatchReached(name)

        return reached


class _CanonicalStoreClient:
    """Small authoritative-store fake behind the real paired plugin backend."""

    def __init__(self) -> None:
        self.current_revision = 1
        self.reject_pairing = False
        self.write_attempts = 0
        self.canonical_writes = 0

    def project_workspace(self, project_id: str) -> dict:
        if project_id != "proj_canonical":
            raise AssertionError("project scope changed")
        project, _coverage, _timeline = _raw_snapshot(self.current_revision)
        return deepcopy(project)

    def coverage_workspace(self, project_id: str) -> dict:
        if project_id != "proj_canonical":
            raise AssertionError("project scope changed")
        _project, coverage, _timeline = _raw_snapshot(self.current_revision)
        return deepcopy(coverage)

    def timeline_workspace(
        self,
        project_id: str,
        revision: int | None = None,
    ) -> dict:
        if project_id != "proj_canonical":
            raise AssertionError("project scope changed")
        selected_revision = self.current_revision if revision is None else revision
        _project, _coverage, timeline = _raw_snapshot(selected_revision)
        if revision is not None:
            _head_project, _head_coverage, current = _raw_snapshot(
                self.current_revision
            )
            timeline["head"] = deepcopy(current["head"])
            timeline["selected_revision"]["is_head"] = False
        return deepcopy(timeline)

    def timeline_write(
        self,
        *,
        command: str,
        project_id: str,
        body: dict,
        idempotency_key: str,
        credential: dict,
    ) -> dict:
        del idempotency_key, credential
        if command != "edit" or project_id != "proj_canonical":
            raise AssertionError("write scope changed")
        self.write_attempts += 1
        if self.reject_pairing:
            raise AgentApiError(
                "The paired Agent capability was revoked.",
                code="agent_pairing_required",
                status=403,
            )
        current_head = _snapshot(self.current_revision).expected_timeline_head
        if body.get("expected_timeline_head") != current_head:
            raise AgentApiError(
                "Canonical Timeline changed.",
                code="timeline_head_conflict",
                status=409,
            )
        self.canonical_writes += 1
        raise AssertionError("negative oracle must never admit a canonical write")


class ProductionAuthorityInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )

    def tearDown(self) -> None:
        self.manager.close()

    def _bootstrap(self, handoff: dict):
        origin, boot = _origin_and_boot(handoff)
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, state = _post_json(
            opener,
            f"{origin}/api/sessions/{handoff['session_id']}/bootstrap",
            {"token": boot},
            origin=origin,
        )
        self.assertEqual(status, 200)
        return origin, opener, state

    def test_all_editor_actions_require_bootstrapped_cookie_before_dispatch(self) -> None:
        legacy_payloads = {
            "plugin_editor_action.legacy.apply": {
                "type": "apply",
                "operations": [],
                "created_at": "2026-08-29T00:00:01Z",
            },
            "plugin_editor_action.legacy.undo": {"type": "undo"},
            "plugin_editor_action.legacy.redo": {"type": "redo"},
        }
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
                "edit": {"op": "move_clip", "clip_id": "clip_1", "to_index": 0},
            },
            "plugin_editor_action.canonical.edit.trim_clip": {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": "clip_1",
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            "plugin_editor_action.canonical.edit.set_clip_duration": {
                "type": "stage",
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": "clip_1",
                    "duration_ms": 1000,
                },
            },
            "plugin_editor_action.canonical.edit.replace_clip": {
                "type": "stage",
                "edit": {
                    "op": "replace_clip",
                    "clip_id": "clip_1",
                    "assignment_id": "assign_image",
                },
            },
            "plugin_editor_action.canonical.select_revision": {
                "type": "select_revision",
                "revision": 1,
            },
            "plugin_editor_action.canonical.stage_restore": {
                "type": "stage_restore"
            },
            "plugin_editor_action.canonical.discard": {"type": "discard"},
            "plugin_editor_action.canonical.refresh": {"type": "refresh"},
            "plugin_editor_action.canonical.save": {"type": "save"},
        }
        expected = {
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
        discovered = discover_plugin_editor_actions(
            editor_server_path=SCRIPTS / "memolens_editor_server.py",
            canonical_editor_path=SCRIPTS / "memolens_canonical_editor.py",
        )
        self.assertEqual(set(legacy_payloads) | set(canonical_payloads), expected)
        self.assertEqual(discovered, frozenset(expected))

        legacy = self.manager.create_handoff(timeline=_legacy_timeline())
        legacy_origin, _legacy_boot = _origin_and_boot(legacy)
        legacy_endpoint = (
            f"{legacy_origin}/api/sessions/{legacy['session_id']}/actions"
        )
        with patch.object(
            self.manager,
            "_apply_legacy_action",
            side_effect=AssertionError("legacy dispatch was reached"),
        ) as dispatch:
            for action_id, payload in legacy_payloads.items():
                with self.subTest(action_id=action_id):
                    status, body = _post_error(
                        self,
                        build_opener(),
                        legacy_endpoint,
                        payload,
                        origin=legacy_origin,
                    )
                    self.assertEqual(status, 401)
                    self.assertEqual(
                        body["error"]["code"], "editor_session_unauthorized"
                    )
            dispatch.assert_not_called()

        backend = _FixtureBackend()
        canonical = self.manager.create_canonical_handoff(backend=backend)
        canonical_origin, _canonical_boot = _origin_and_boot(canonical)
        canonical_endpoint = (
            f"{canonical_origin}/api/sessions/{canonical['session_id']}/actions"
        )
        with patch.object(
            self.manager,
            "_apply_canonical_action",
            side_effect=AssertionError("canonical dispatch was reached"),
        ) as dispatch:
            for action_id, payload in canonical_payloads.items():
                with self.subTest(action_id=action_id):
                    status, body = _post_error(
                        self,
                        build_opener(),
                        canonical_endpoint,
                        payload,
                        origin=canonical_origin,
                    )
                    self.assertEqual(status, 401)
                    self.assertEqual(
                        body["error"]["code"], "editor_session_unauthorized"
                    )
            dispatch.assert_not_called()
        self.assertEqual(backend.saved_edits, [])
        self.assertEqual(backend.saved_requests, [])

    def test_bootstrap_requires_live_single_use_session_bound_boot_token_not_cookie(
        self,
    ) -> None:
        first = self.manager.create_handoff(timeline=_legacy_timeline())
        first_origin, first_boot = _origin_and_boot(first)
        first_url = (
            f"{first_origin}/api/sessions/{first['session_id']}/bootstrap"
        )
        first_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        status, _state = _post_json(
            first_opener,
            first_url,
            {"token": first_boot},
            origin=first_origin,
        )
        self.assertEqual(status, 200)

        replay_status, replay = _post_error(
            self,
            first_opener,
            first_url,
            {"token": first_boot},
            origin=first_origin,
        )
        self.assertEqual(replay_status, 401)
        self.assertEqual(replay["error"]["code"], "editor_boot_token_invalid")

        second = self.manager.create_handoff(timeline=_legacy_timeline())
        second_origin, second_boot = _origin_and_boot(second)
        second_url = (
            f"{second_origin}/api/sessions/{second['session_id']}/bootstrap"
        )
        forged_status, forged = _post_error(
            self,
            build_opener(),
            second_url,
            {"token": "forged-editor-boot-token"},
            origin=second_origin,
            cookie=f"{EDITOR_COOKIE}=forged-editor-cookie",
        )
        self.assertEqual(forged_status, 401)
        self.assertEqual(forged["error"]["code"], "editor_boot_token_invalid")
        valid_status, _second_state = _post_json(
            build_opener(),
            second_url,
            {"token": second_boot},
            origin=second_origin,
        )
        self.assertEqual(valid_status, 200)

        scoped = self.manager.create_handoff(timeline=_legacy_timeline())
        target = self.manager.create_handoff(timeline=_legacy_timeline())
        scoped_origin, scoped_boot = _origin_and_boot(scoped)
        target_origin, _target_boot = _origin_and_boot(target)
        wrong_session_status, wrong_session = _post_error(
            self,
            build_opener(),
            f"{target_origin}/api/sessions/{target['session_id']}/bootstrap",
            {"token": scoped_boot},
            origin=target_origin,
        )
        self.assertEqual(wrong_session_status, 401)
        self.assertEqual(
            wrong_session["error"]["code"], "editor_boot_token_invalid"
        )
        scoped_status, _scoped_state = _post_json(
            build_opener(),
            f"{scoped_origin}/api/sessions/{scoped['session_id']}/bootstrap",
            {"token": scoped_boot},
            origin=scoped_origin,
        )
        self.assertEqual(scoped_status, 200)

        expired = self.manager.create_handoff(timeline=_legacy_timeline())
        expired_origin, expired_boot = _origin_and_boot(expired)
        with self.manager._lock:
            session = self.manager._sessions[expired["session_id"]]
            session.boot_expires_at = time.monotonic() - 1
        expired_status, expired_body = _post_error(
            self,
            build_opener(),
            f"{expired_origin}/api/sessions/{expired['session_id']}/bootstrap",
            {"token": expired_boot},
            origin=expired_origin,
        )
        self.assertEqual(expired_status, 401)
        self.assertEqual(
            expired_body["error"]["code"], "editor_boot_token_invalid"
        )

    def test_mcp_advertised_allowed_and_actual_dispatchers_are_closed(self) -> None:
        advertised = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(advertised), len(set(advertised)))
        self.assertEqual(set(advertised), set(TOOL_ARGUMENT_FIELDS))
        for tool in TOOLS:
            self.assertEqual(
                set(tool["inputSchema"]["properties"]),
                set(TOOL_ARGUMENT_FIELDS[tool["name"]]),
                tool["name"],
            )

        argument_values = {
            "asset_id": "asset_1",
            "candidate": {},
            "created_at": "2026-08-29T00:00:00Z",
            "evidence_id": "evidence_1",
            "items": [],
            "operations": [],
            "page_id": "page_1",
            "project_id": "proj_1",
            "query": "coastline",
            "request_id": "lb_" + "a" * 64,
            "request_idempotency_key": "production-inventory-key",
            "timeline": {},
            "timeline_id": "timeline_1",
        }
        gateway = _GatewayTrap()
        reached: dict[str, str] = {}
        expected_dispatch = {
            name: name.removeprefix("memolens_") for name in advertised
        }
        expected_dispatch.update(
            {
                "memolens_canonical_editor_handoff": (
                    "create_canonical_editor_handoff"
                ),
                "memolens_editor_handoff": "create_editor_handoff",
                "memolens_library_bootstrap_start": "start_library_bootstrap",
                "memolens_library_bootstrap_status": "library_bootstrap_status",
            }
        )
        for name in advertised:
            arguments = {
                field: argument_values[field]
                for field in TOOL_ARGUMENT_FIELDS[name]
                if field in argument_values
            }
            try:
                if name == "memolens_canonical_editor_handoff":
                    with patch.object(
                        memolens_mcp,
                        "create_canonical_editor_handoff",
                        side_effect=_DispatchReached(
                            "create_canonical_editor_handoff"
                        ),
                    ):
                        memolens_mcp.call_tool(name, arguments, gateway)
                elif name == "memolens_editor_handoff":
                    with patch.object(
                        memolens_mcp,
                        "create_editor_handoff",
                        side_effect=_DispatchReached("create_editor_handoff"),
                    ):
                        memolens_mcp.call_tool(name, arguments, gateway)
                elif name == "memolens_library_bootstrap_start":
                    with patch.object(
                        memolens_mcp,
                        "start_library_bootstrap",
                        side_effect=_DispatchReached("start_library_bootstrap"),
                    ):
                        memolens_mcp.call_tool(name, arguments, gateway)
                elif name == "memolens_library_bootstrap_status":
                    with patch.object(
                        memolens_mcp,
                        "library_bootstrap_status",
                        side_effect=_DispatchReached("library_bootstrap_status"),
                    ):
                        memolens_mcp.call_tool(name, arguments, gateway)
                else:
                    memolens_mcp.call_tool(name, arguments, gateway)
            except _DispatchReached as exc:
                reached[name] = str(exc)
            else:
                self.fail(f"Advertised MCP tool did not reach a dispatcher: {name}")
        self.assertEqual(reached, expected_dispatch)

        calls_before_unknown = list(gateway.calls)
        with self.assertRaises(MemoLensError) as unknown:
            memolens_mcp.call_tool("memolens_future_magic", {}, gateway)
        self.assertEqual(unknown.exception.code, "unknown_tool")
        self.assertEqual(gateway.calls, calls_before_unknown)

    def test_canonical_stage_and_restore_require_matching_agent_pairing(self) -> None:
        client = _CanonicalStoreClient()
        no_actions = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={"database_uuid": DATABASE_UUID, "actions": []},
            client=client,
        )
        unpaired = self.manager.create_canonical_handoff(backend=no_actions)
        unpaired_origin, unpaired_opener, _state = self._bootstrap(unpaired)
        unpaired_endpoint = (
            f"{unpaired_origin}/api/sessions/{unpaired['session_id']}/actions"
        )
        for edit in (
            {"op": "move_clip", "clip_id": "clip_1", "to_index": 0},
            {
                "op": "trim_clip",
                "clip_id": "clip_1",
                "source_in_ms": 1200,
                "source_out_ms": 2000,
            },
            {
                "op": "set_clip_duration",
                "clip_id": "clip_1",
                "duration_ms": 1000,
            },
            {
                "op": "replace_clip",
                "clip_id": "clip_1",
                "assignment_id": "assign_image",
            },
        ):
            with self.subTest(edit=edit["op"]):
                status, body = _post_error(
                    self,
                    unpaired_opener,
                    unpaired_endpoint,
                    {"type": "stage", "edit": edit},
                    origin=unpaired_origin,
                )
                self.assertEqual(status, 403)
                self.assertEqual(
                    body["error"]["code"], "agent_pairing_scope_denied"
                )

        edit_only = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={
                "database_uuid": DATABASE_UUID,
                "actions": ["timeline.apply_edit"],
            },
            client=client,
        )
        restore_denied = self.manager.create_canonical_handoff(backend=edit_only)
        restore_origin, restore_opener, _state = self._bootstrap(restore_denied)
        restore_status, restore_body = _post_error(
            self,
            restore_opener,
            f"{restore_origin}/api/sessions/{restore_denied['session_id']}/actions",
            {"type": "stage_restore"},
            origin=restore_origin,
        )
        self.assertEqual(restore_status, 403)
        self.assertEqual(
            restore_body["error"]["code"], "agent_pairing_scope_denied"
        )
        self.assertEqual(client.write_attempts, 0)
        self.assertEqual(client.canonical_writes, 0)

    def test_canonical_save_pairing_and_revision_cas_denials_write_zero_rows(
        self,
    ) -> None:
        client = _CanonicalStoreClient()
        backend = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={
                "database_uuid": DATABASE_UUID,
                "actions": ["timeline.apply_edit"],
            },
            client=client,
        )
        handoff = self.manager.create_canonical_handoff(backend=backend)
        origin, opener, state = self._bootstrap(handoff)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]
        _status, staged = _post_json(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            origin=origin,
        )
        self.assertIsNotNone(staged["pending"])

        client.reject_pairing = True
        pairing_status, pairing_denied = _post_error(
            self,
            opener,
            endpoint,
            {"type": "save"},
            origin=origin,
        )
        self.assertEqual(pairing_status, 403)
        self.assertEqual(
            pairing_denied["error"]["code"], "agent_pairing_required"
        )
        self.assertEqual(client.write_attempts, 1)
        self.assertEqual(client.canonical_writes, 0)

        client.reject_pairing = False
        client.current_revision = 2
        cas_status, cas_denied = _post_error(
            self,
            opener,
            endpoint,
            {"type": "save"},
            origin=origin,
        )
        self.assertEqual(cas_status, 409)
        self.assertEqual(cas_denied["error"]["code"], "timeline_head_conflict")
        self.assertEqual(
            cas_denied["error"]["details"]["current"]["projection"][
                "timeline_head"
            ]["revision"],
            2,
        )
        self.assertEqual(client.write_attempts, 2)
        self.assertEqual(client.canonical_writes, 0)

        with opener.open(
            f"{origin}/api/sessions/{handoff['session_id']}"
        ) as response:
            reconciled = json.load(response)
        self.assertEqual(reconciled["projection"]["timeline_head"]["revision"], 2)
        self.assertIsNone(reconciled["pending"])
        self.assertIsNone(reconciled["last_save"])


if __name__ == "__main__":
    unittest.main()
