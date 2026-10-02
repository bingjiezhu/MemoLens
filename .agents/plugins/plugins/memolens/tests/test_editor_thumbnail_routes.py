from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import test_canonical_editor_handoff as fixtures
from memolens_editor_server import EditorServerManager


class _StableClipThumbnailBackend(fixtures._ThumbnailBackend):
    def __init__(self):
        super().__init__()
        project, coverage, timeline = fixtures._raw_snapshot(2)
        timeline["timeline"]["tracks"][0]["clips"][0]["clip_id"] = "clip_1"
        timeline["source_bindings"][0]["clip_id"] = "clip_1"
        fixtures._reseal_snapshot(project, coverage, timeline)
        self.snapshots[1] = fixtures.canonical_editor_snapshot(
            project_id="proj_canonical",
            credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
            project_response=project,
            coverage_response=coverage,
            timeline_response=timeline,
        )


class EditorThumbnailRouteTests(unittest.TestCase):
    _post = staticmethod(fixtures.CanonicalEditorHandoffTests._post)
    _open = fixtures.CanonicalEditorHandoffTests._open

    def setUp(self):
        self.manager = EditorServerManager(session_ttl_seconds=60, boot_ttl_seconds=30)

    def tearDown(self):
        self.manager.close()

    def test_thumbnail_routes_reject_query_authority_and_cache_busters(self):
        backend = fixtures._ThumbnailBackend()
        handoff, origin, opener, _state = self._open(backend)
        base = f"{origin}/api/sessions/{handoff['session_id']}"
        for route in (
            "previews/clip_1", "previews/clip_1/1",
            "replacement-previews/clip_1/assign_image",
            "replacement-previews/clip_1/assign_image/1",
        ):
            for query in ("?revision=1", "?path=%2Fprivate", "?v=2", "?"):
                with self.subTest(route=route, query=query):
                    with self.assertRaises(HTTPError) as denied:
                        opener.open(Request(f"{base}/{route}{query}", headers={"Origin": origin}))
                    self.assertEqual(denied.exception.code, 404)
                    self.assertEqual(json.load(denied.exception)["error"]["code"], "editor_route_not_found")
                    denied.exception.close()
        self.assertEqual(backend.thumbnail_requests, [])
        self.assertEqual(backend.replacement_thumbnail_requests, [])
        self.assertEqual(backend.saved_edits, [])

    def test_versioned_routes_are_current_snapshot_only_and_reject_old_versions(self):
        backend = _StableClipThumbnailBackend()
        handoff, origin, opener, _state = self._open(backend)
        base = f"{origin}/api/sessions/{handoff['session_id']}"
        routes = ("previews/clip_1", "replacement-previews/clip_1/assign_image")
        for revision in (1, 2):
            backend.index = revision - 1
            self._post(opener, f"{base}/actions", {"type": "refresh"}, origin)
            for route in routes:
                with opener.open(Request(f"{base}/{route}/{revision}", headers={"Origin": origin})) as response:
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                    response.read()
            self.assertIs(backend.asserted_thumbnail_snapshot, backend.snapshots[revision - 1])
            self.assertIs(backend.asserted_replacement_thumbnail_snapshot, backend.snapshots[revision - 1])
        for route in routes:
            for revision in (1, 3):
                with self.assertRaises(HTTPError) as denied:
                    opener.open(Request(f"{base}/{route}/{revision}", headers={"Origin": origin}))
                self.assertEqual(denied.exception.code, 409)
                self.assertEqual(json.load(denied.exception)["error"]["code"], "agent_preview_head_changed")
                denied.exception.close()
        self.assertEqual(len(backend.thumbnail_requests), 2)
        self.assertEqual(len(backend.replacement_thumbnail_requests), 2)
        self.assertEqual(backend.saved_edits, [])

    def test_versioned_routes_preserve_cookie_origin_and_closed_revision_validation(self):
        backend = fixtures._ThumbnailBackend()
        handoff, origin, opener, _state = self._open(backend)
        base = f"{origin}/api/sessions/{handoff['session_id']}"
        for route in ("previews/clip_1", "replacement-previews/clip_1/assign_image"):
            with self.assertRaises(HTTPError) as no_cookie:
                build_opener().open(Request(f"{base}/{route}/1", headers={"Origin": origin}))
            self.assertEqual(no_cookie.exception.code, 401)
            no_cookie.exception.close()
            with self.assertRaises(HTTPError) as wrong_origin:
                opener.open(Request(f"{base}/{route}/1", headers={"Origin": "https://attacker.invalid"}))
            self.assertEqual(wrong_origin.exception.code, 403)
            wrong_origin.exception.close()
            for revision in ("0", "01", "-1", "%31", "1/extra", "x", "999999999999999999999999"):
                with self.subTest(route=route, revision=revision):
                    with self.assertRaises(HTTPError) as invalid:
                        opener.open(Request(f"{base}/{route}/{revision}", headers={"Origin": origin}))
                    self.assertEqual(invalid.exception.code, 404)
                    invalid.exception.close()
        self.assertEqual(backend.thumbnail_requests, [])
        self.assertEqual(backend.replacement_thumbnail_requests, [])

    def test_same_route_reads_current_snapshot_after_refresh_without_writes(self):
        backend = fixtures._ThumbnailBackend()
        handoff, origin, opener, _state = self._open(backend)
        base = f"{origin}/api/sessions/{handoff['session_id']}"
        url = f"{base}/previews/clip_1"
        for revision in (0, 1):
            backend.index = revision
            self._post(opener, f"{base}/actions", {"type": "refresh"}, origin)
            with opener.open(Request(url, headers={"Origin": origin})) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                self.assertEqual(response.read(), b"\xff\xd8verified-thumbnail\xff\xd9")
            self.assertIs(backend.asserted_thumbnail_snapshot, backend.snapshots[revision])
        self.assertEqual(backend.saved_edits, [])


if __name__ == "__main__":
    unittest.main()
