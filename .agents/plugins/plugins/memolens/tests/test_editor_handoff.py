from __future__ import annotations

from contextlib import closing
from http.cookiejar import CookieJar
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_editor_server import EditorServerError, EditorServerManager  # noqa: E402
from memolens_mcp import TOOLS  # noqa: E402
from memolens_timeline import draft_timeline  # noqa: E402


def _timeline() -> dict:
    return draft_timeline(
        project_id="proj_browser_editor",
        items=[
            {
                "kind": "video",
                "asset_id": "asset_video",
                "asset_source_id": "src_video",
                "asset_sha256": "a" * 64,
                "segment_id": "seg_current",
                "analysis_run_id": "arun_current",
                "analysis_revision": 1,
                "source_in_ms": 1000,
                "source_out_ms": 4000,
                "timeline_duration_ms": 3000,
                "reason": "Open on the coastline",
                "match_id": "seg_current",
            }
        ],
        created_at="2026-08-23T00:00:00Z",
    )


def _all_kind_timeline() -> dict:
    return draft_timeline(
        project_id="proj_browser_editor_all_kinds",
        items=[
            {
                "kind": "video",
                "asset_id": "asset_video",
                "asset_source_id": "src_video",
                "asset_sha256": "a" * 64,
                "segment_id": "seg_current",
                "analysis_run_id": "arun_current",
                "analysis_revision": 1,
                "source_in_ms": 1000,
                "source_out_ms": 4000,
                "timeline_duration_ms": 3000,
                "reason": "Open on the coastline",
                "match_id": "seg_current",
            },
            {
                "kind": "image",
                "asset_id": "asset_image",
                "asset_source_id": "src_image",
                "asset_sha256": "b" * 64,
                "timeline_duration_ms": 1500,
                "reason": "Hold on one detail",
                "match_id": "asset_image",
            },
            {
                "kind": "audio",
                "asset_id": "asset_audio",
                "asset_source_id": "src_audio",
                "asset_sha256": "c" * 64,
                "source_in_ms": 0,
                "source_out_ms": 4500,
                "timeline_duration_ms": 4500,
                "reason": "Keep the restrained score",
                "match_id": "asset_audio",
            },
        ],
        created_at="2026-08-23T00:00:00Z",
    )


def _video_candidate() -> dict:
    return {
        "kind": "video",
        "label": "Second shot",
        "asset_id": "asset_second",
        "asset_source_id": "src_second",
        "asset_sha256": "D" * 64,
        "segment_id": "seg_second",
        "analysis_run_id": "arun_second",
        "analysis_revision": 2,
        "source_in_ms": 5000,
        "source_out_ms": 8000,
        "reason": "  Continue   with motion ",
        "match_id": "match_second",
    }


def _image_candidate() -> dict:
    return {
        "kind": "image",
        "label": "Still detail",
        "asset_id": "asset_still",
        "asset_source_id": "src_still",
        "asset_sha256": "e" * 64,
    }


def _audio_candidate() -> dict:
    return {
        "kind": "audio",
        "label": "Alternate score",
        "asset_id": "asset_score",
        "asset_source_id": "src_score",
        "asset_sha256": "f" * 64,
        "source_in_ms": 1200,
        "source_out_ms": 5700,
    }


class EditorHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )

    def tearDown(self) -> None:
        self.manager.close()

    @staticmethod
    def _request_json(opener, url: str, payload: dict, *, origin: str):
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": origin},
            method="POST",
        )
        with opener.open(request) as response:
            return response, json.load(response)

    def test_handoff_is_loopback_scoped_with_narrow_path_safety_claim(self) -> None:
        result = self.manager.create_handoff(
            timeline=_timeline(),
            replacement_candidates=[_video_candidate()],
        )
        parsed = urlsplit(result["editor_url"])
        self.assertEqual(parsed.scheme, "http")
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertTrue(parse_qs(parsed.fragment)["boot"][0])
        self.assertEqual(result["surface"], "loopback-browser")
        self.assertEqual(result["uiHandoff"]["type"], "browser")
        self.assertEqual(result["uiHandoff"]["url"], result["editor_url"])
        self.assertTrue(result["uiHandoff"]["requiresUserGesture"])
        self.assertEqual(
            result["browserHandoff"]["preferredMode"],
            "codex-internal-browser",
        )
        self.assertNotIn("Codex", result["next_step"])
        self.assertEqual(result["persistence"], "not_saved")
        self.assertFalse(result["safety"]["source_paths_loaded"])
        self.assertFalse(result["safety"]["storage_paths_introduced"])
        self.assertNotIn("absolute_paths_exposed", result["safety"])

    def test_replacement_contract_is_discriminated_for_all_three_kinds(self) -> None:
        result = self.manager.create_handoff(
            timeline=_all_kind_timeline(),
            replacement_candidates=[
                _video_candidate(),
                _image_candidate(),
                _audio_candidate(),
            ],
        )
        boot = parse_qs(urlsplit(result["editor_url"]).fragment)["boot"][0]
        _, state = self.manager.bootstrap(result["session_id"], boot)
        candidates = {item["kind"]: item for item in state["replacement_candidates"]}

        self.assertEqual(set(candidates), {"video", "image", "audio"})
        self.assertEqual(candidates["video"]["asset_sha256"], "d" * 64)
        self.assertEqual(candidates["video"]["reason"], "Continue with motion")
        self.assertEqual(candidates["video"]["analysis_revision"], 2)
        self.assertNotIn("source_in_ms", candidates["image"])
        self.assertEqual(candidates["audio"]["source_out_ms"], 5700)

    def test_incomplete_or_cross_kind_candidate_fields_are_rejected_upfront(self) -> None:
        incomplete_video = {
            "kind": "video",
            "asset_id": "asset_second",
            "asset_source_id": "src_second",
            "asset_sha256": "d" * 64,
        }
        with self.assertRaisesRegex(EditorServerError, "required video fields"):
            self.manager.create_handoff(
                timeline=_timeline(),
                replacement_candidates=[incomplete_video],
            )

        image_with_range = {
            **_image_candidate(),
            "source_in_ms": 0,
            "source_out_ms": 1000,
        }
        with self.assertRaisesRegex(EditorServerError, "unsupported fields"):
            self.manager.create_handoff(
                timeline=_timeline(),
                replacement_candidates=[image_with_range],
            )

    def test_replacement_candidates_reject_paths_in_ids_and_display_text(self) -> None:
        cases = [
            ("asset_id", "file:private"),
            ("asset_source_id", "data:private"),
            ("label", "/Users/person/private.mov"),
            ("reason", "Use ~/private.mov"),
            ("match_id", "file:/tmp/private.mov"),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                candidate = _video_candidate()
                candidate[field] = value
                with self.assertRaisesRegex(EditorServerError, field):
                    self.manager.create_handoff(
                        timeline=_timeline(),
                        replacement_candidates=[candidate],
                    )

        with self.assertRaisesRegex(EditorServerError, "unsupported fields"):
            self.manager.create_handoff(
                timeline=_timeline(),
                replacement_candidates=[
                    {**_video_candidate(), "absolute_path": "/private/source.mov"}
                ],
            )

    def test_browser_boot_action_undo_redo_and_security_headers(self) -> None:
        result = self.manager.create_handoff(timeline=_timeline())
        parsed = urlsplit(result["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        session_id = result["session_id"]
        boot = parse_qs(parsed.fragment)["boot"][0]
        opener = build_opener(HTTPCookieProcessor(CookieJar()))

        with opener.open(f"{origin}{parsed.path}") as response:
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("MemoLens Unsaved Draft Lab", html)
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertIn(
                "frame-ancestors 'none'",
                response.headers["Content-Security-Policy"],
            )
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

        _, state = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/bootstrap",
            {"token": boot},
            origin=origin,
        )
        self.assertEqual(state["persistence"], "not_saved")
        self.assertEqual(state["experience"], "unsaved_draft_lab")
        self.assertEqual(state["title"], "MemoLens Unsaved Draft Lab")
        self.assertFalse(state["capabilities"]["persist_timeline"])
        self.assertFalse(state["capabilities"]["media_playback"])
        self.assertFalse(state["safety"]["source_paths_loaded"])
        self.assertFalse(state["safety"]["storage_paths_introduced"])
        clip_id = state["timeline"]["tracks"][0]["clips"][0]["id"]

        with self.assertRaises(HTTPError) as reused:
            self._request_json(
                opener,
                f"{origin}/api/sessions/{session_id}/bootstrap",
                {"token": boot},
                origin=origin,
            )
        self.assertEqual(reused.exception.code, 401)
        reused.exception.close()

        _, state = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/actions",
            {
                "type": "apply",
                "operations": [
                    {"op": "split_clip", "clip_id": clip_id, "offset_ms": 1000}
                ],
                "created_at": "2026-08-23T00:01:00Z",
            },
            origin=origin,
        )
        self.assertEqual(len(state["timeline"]["tracks"][0]["clips"]), 2)
        self.assertTrue(state["history"]["undo_available"])

        _, state = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/actions",
            {"type": "undo"},
            origin=origin,
        )
        self.assertEqual(len(state["timeline"]["tracks"][0]["clips"]), 1)
        self.assertTrue(state["history"]["redo_available"])

        _, state = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/actions",
            {"type": "redo"},
            origin=origin,
        )
        self.assertEqual(len(state["timeline"]["tracks"][0]["clips"]), 2)

        with opener.open(f"{origin}/api/sessions/{session_id}") as response:
            reloaded = json.load(response)
        self.assertEqual(reloaded["timeline"]["revision"], 2)

        bad_origin = Request(
            f"{origin}/api/sessions/{session_id}/actions",
            data=b'{"type":"undo"}',
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.invalid",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as denied:
            opener.open(bad_origin)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

    def test_bootstrapped_session_expiry_does_not_slide_past_cookie_lifetime(self) -> None:
        result = self.manager.create_handoff(timeline=_timeline())
        boot = parse_qs(urlsplit(result["editor_url"]).fragment)["boot"][0]
        auth_token, state = self.manager.bootstrap(result["session_id"], boot)
        fixed_expiry = self.manager._sessions[result["session_id"]].expires_at

        self.manager.authenticate(result["session_id"], auth_token)
        clip_id = state["timeline"]["tracks"][0]["clips"][0]["id"]
        self.manager.apply_action(
            result["session_id"],
            auth_token,
            {
                "type": "apply",
                "operations": [
                    {"op": "split_clip", "clip_id": clip_id, "offset_ms": 1000}
                ],
                "created_at": "2026-08-23T00:01:00Z",
            },
        )
        self.assertEqual(
            self.manager._sessions[result["session_id"]].expires_at,
            fixed_expiry,
        )

    def test_complete_video_candidate_replaces_without_422(self) -> None:
        result = self.manager.create_handoff(
            timeline=_timeline(),
            replacement_candidates=[_video_candidate()],
        )
        parsed = urlsplit(result["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        session_id = result["session_id"]
        boot = parse_qs(parsed.fragment)["boot"][0]
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        _, state = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/bootstrap",
            {"token": boot},
            origin=origin,
        )
        clip_id = state["timeline"]["tracks"][0]["clips"][0]["id"]
        candidate = state["replacement_candidates"][0]
        operation = {
            "op": "replace_clip",
            "clip_id": clip_id,
            "ripple": True,
            **{
                key: value
                for key, value in candidate.items()
                if key not in {"kind", "label"}
            },
        }

        _, revised = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/actions",
            {
                "type": "apply",
                "operations": [operation],
                "created_at": "2026-08-23T00:02:00Z",
            },
            origin=origin,
        )
        clip = revised["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(revised["timeline"]["revision"], 2)
        self.assertEqual(clip["asset_id"], "asset_second")
        self.assertEqual((clip["source_in_ms"], clip["source_out_ms"]), (5000, 8000))
        self.assertEqual(clip["provenance"]["reason"], "Continue with motion")

    def test_cross_kind_replacement_action_is_rejected(self) -> None:
        result = self.manager.create_handoff(
            timeline=_timeline(),
            replacement_candidates=[_image_candidate()],
        )
        parsed = urlsplit(result["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        session_id = result["session_id"]
        boot = parse_qs(parsed.fragment)["boot"][0]
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        _, state = self._request_json(
            opener,
            f"{origin}/api/sessions/{session_id}/bootstrap",
            {"token": boot},
            origin=origin,
        )
        self.assertFalse(state["capabilities"]["replace"])
        clip_id = state["timeline"]["tracks"][0]["clips"][0]["id"]
        candidate = state["replacement_candidates"][0]
        with self.assertRaises(HTTPError) as denied:
            self._request_json(
                opener,
                f"{origin}/api/sessions/{session_id}/actions",
                {
                    "type": "apply",
                    "operations": [
                        {
                            "op": "replace_clip",
                            "clip_id": clip_id,
                            "ripple": True,
                            **{
                                key: value
                                for key, value in candidate.items()
                                if key not in {"kind", "label"}
                            },
                        }
                    ],
                    "created_at": "2026-08-23T00:03:00Z",
                },
                origin=origin,
            )
        self.assertEqual(denied.exception.code, 422)
        denied.exception.close()

    def test_mcp_schema_and_ui_discriminate_replacement_kind(self) -> None:
        tool = next(item for item in TOOLS if item["name"] == "memolens_editor_handoff")
        candidate_schema = tool["inputSchema"]["properties"]["replacement_candidates"]["items"]
        branches = {
            branch["properties"]["kind"]["const"]: branch
            for branch in candidate_schema["oneOf"]
        }
        self.assertEqual(set(branches), {"video", "image", "audio"})
        self.assertTrue(
            {
                "kind",
                "segment_id",
                "analysis_run_id",
                "analysis_revision",
                "source_in_ms",
                "source_out_ms",
            }.issubset(branches["video"]["required"])
        )
        self.assertNotIn("source_in_ms", branches["image"]["properties"])
        self.assertTrue(
            {"kind", "source_in_ms", "source_out_ms"}.issubset(
                branches["audio"]["required"]
            )
        )

        html = (PLUGIN_ROOT / "ui" / "editor.html").read_text(encoding="utf-8")
        self.assertIn('candidate.kind === row.track.type', html)
        self.assertIn('candidate.kind !== row.track.type', html)

    def test_api_requires_bootstrapped_cookie(self) -> None:
        result = self.manager.create_handoff(timeline=_timeline())
        parsed = urlsplit(result["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self.assertRaises(HTTPError) as denied:
            build_opener().open(
                f"{origin}/api/sessions/{result['session_id']}"
            )
        self.assertEqual(denied.exception.code, 401)
        denied.exception.close()

    def test_draft_lab_process_restart_discards_state_and_never_mutates_canonical_db(
        self,
    ) -> None:
        """The legacy editor stays process memory even beside a canonical DB."""

        with tempfile.TemporaryDirectory(prefix="memolens-draft-lab-restart-") as root:
            database = Path(root) / "canonical.db"
            table_names = (
                "canonical_timeline_revisions",
                "canonical_timeline_operations",
                "canonical_timeline_heads",
                "canonical_timeline_receipts",
                "agent_project_command_receipts",
                "agent_project_capability_events",
            )
            with closing(sqlite3.connect(database)) as connection:
                for index, table in enumerate(table_names, start=1):
                    connection.execute(
                        f"CREATE TABLE {table}(id TEXT PRIMARY KEY, digest TEXT NOT NULL)"
                    )
                    connection.execute(
                        f"INSERT INTO {table}(id,digest) VALUES(?,?)",
                        (f"sentinel-{index}", f"{index:064x}"),
                    )
                connection.commit()

            def census() -> tuple[bytes, tuple[tuple[str, tuple[tuple[str, str], ...]], ...]]:
                with closing(sqlite3.connect(database)) as connection:
                    rows = tuple(
                        (
                            table,
                            tuple(
                                connection.execute(
                                    f"SELECT id,digest FROM {table} ORDER BY id"
                                )
                            ),
                        )
                        for table in table_names
                    )
                return database.read_bytes(), rows

            before = census()
            program = f"""
import json
import sys
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, {str(SCRIPTS)!r})
from memolens_editor_server import EditorServerManager

payload = json.load(sys.stdin)
manager = EditorServerManager(session_ttl_seconds=60, boot_ttl_seconds=30)
try:
    handoff = manager.create_handoff(timeline=payload["timeline"])
    boot = parse_qs(urlsplit(handoff["editor_url"]).fragment)["boot"][0]
    auth, state = manager.bootstrap(handoff["session_id"], boot)
    if payload["mode"] == "mutate":
        clip_id = state["timeline"]["tracks"][0]["clips"][0]["id"]
        state = manager.apply_action(
            handoff["session_id"],
            auth,
            {{
                "type": "apply",
                "operations": [
                    {{"op": "split_clip", "clip_id": clip_id, "offset_ms": 1000}}
                ],
                "created_at": "2026-08-30T00:00:00Z",
            }},
        )
        right_clip_id = state["timeline"]["tracks"][0]["clips"][1]["id"]
        state = manager.apply_action(
            handoff["session_id"],
            auth,
            {{
                "type": "apply",
                "operations": [
                    {{"op": "delete_clip", "clip_id": right_clip_id}}
                ],
                "created_at": "2026-08-30T00:00:01Z",
            }},
        )
    first_clip = state["timeline"]["tracks"][0]["clips"][0]
    print(json.dumps({{
        "clip_count": len(state["timeline"]["tracks"][0]["clips"]),
        "revision": state["timeline"]["revision"],
        "source_out_ms": first_clip["source_out_ms"],
    }}))
finally:
    manager.close()
"""
            environment = os.environ.copy()
            environment["MEMOLENS_DB_PATH"] = str(database)
            environment["SQLITE_DB_PATH"] = str(database)

            def run_process(mode: str) -> dict[str, object]:
                completed = subprocess.run(
                    [sys.executable, "-c", program],
                    input=json.dumps({"mode": mode, "timeline": _timeline()}),
                    text=True,
                    capture_output=True,
                    check=True,
                    cwd=root,
                    env=environment,
                )
                return json.loads(completed.stdout)

            mutated = run_process("mutate")
            restarted = run_process("read")

            self.assertEqual(
                mutated,
                {"clip_count": 1, "revision": 3, "source_out_ms": 2000},
            )
            self.assertEqual(
                restarted,
                {"clip_count": 1, "revision": 1, "source_out_ms": 4000},
            )
            self.assertEqual(census(), before)


if __name__ == "__main__":
    unittest.main()
