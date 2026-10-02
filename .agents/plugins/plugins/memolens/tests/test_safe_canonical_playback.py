from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
TESTS = Path(__file__).resolve().parent
REPO_ROOT = PLUGIN_ROOT.parents[3]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO_ROOT))

from backend.src.agent_preview import (  # noqa: E402
    agent_preview_mint_proof,
    agent_preview_read_proof,
)
from memolens_agent_client import (  # noqa: E402
    AgentApiClient,
    AgentPreviewMediaResponse,
    TIMELINE_PREVIEW_ACTION,
    canonical_sha256,
    timeline_preview_mint_proof,
    timeline_preview_read_proof,
)
from memolens_canonical_editor import (  # noqa: E402
    CanonicalEditorError,
    PairedCanonicalEditorBackend,
)
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_editor_server import (  # noqa: E402
    EDITOR_COOKIE,
    EDITOR_MEDIA_STREAM_CHUNK_BYTES,
    EditorServerError,
    EditorServerManager,
)
from test_canonical_editor_handoff import (  # noqa: E402
    _Backend,
    _historical_snapshot,
)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _RawResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "http://127.0.0.1:8000/media",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = BytesIO(body)
        self._url = url
        self.closed = False
        self.read_sizes: list[int] = []
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True
        self._body.close()

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _RawResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _media_headers(
    length: int,
    *,
    content_range: str | None = None,
) -> dict[str, str]:
    result = {
        "Content-Type": "video/mp4",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "ETag": '"opaque-preview-etag"',
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if content_range is not None:
        result["Content-Range"] = content_range
    return result


class _OneResponseOpener:
    def __init__(self, response: _RawResponse) -> None:
        self.response = response
        self.requests: list[Request] = []

    def open(self, request: Request, *, timeout: float):
        self.requests.append(request)
        return self.response


class _PreviewBackend(_Backend):
    can_preview = True
    can_edit = True
    can_restore = False

    def __init__(self, *, body: bytes = b"0123456789ab") -> None:
        super().__init__()
        self.body = body
        self.mint_count = 0
        self.media_calls: list[dict[str, object]] = []
        self.raws: list[_RawResponse] = []
        self.dropped: list[str | None] = []
        self.early_eof_next = False
        self.expire_during_read_once = False
        self.reject_generic_lease = False

    def mint_preview_lease(self, *, snapshot):
        self.mint_count += 1
        now = datetime.now(timezone.utc)
        clips = [
            {
                "clip_id": clip["clip_id"],
                "timeline_start_ms": clip["start_ms"],
                "timeline_end_ms": clip["end_ms"],
                "source_in_ms": clip["source_in_ms"],
                "source_out_ms": clip["source_out_ms"],
                "status": "playable",
                "reason_code": None,
            }
            for clip in snapshot.public["timeline"]["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        ]
        return {
            "object": "memolens.timeline_preview_lease",
            "schema_version": "1",
            "lease_id": f"preview_{self.mint_count:032x}",
            "lease_secret": f"{self.mint_count:064x}",
            "project_id": snapshot.public["project_id"],
            "observed_head": {
                "revision": snapshot.expected_timeline_head["revision"],
                "content_sha256": snapshot.expected_timeline_head[
                    "timeline_content_sha256"
                ],
            },
            "issued_at": _utc(now),
            "expires_at": _utc(now + timedelta(seconds=90)),
            "budgets": {
                "max_requests": 64,
                "max_response_bytes": 8 * 1024 * 1024,
                "max_aggregate_bytes": 64 * 1024 * 1024,
                "max_concurrency": 2,
            },
            "clips": clips,
        }

    def drop_preview_lease(self, lease):
        self.dropped.append(
            lease.get("lease_id") if isinstance(lease, dict) else None
        )
        if isinstance(lease, dict):
            lease.clear()

    def preview_media(
        self,
        *,
        snapshot,
        lease,
        clip_id,
        method,
        range_header,
    ):
        self.media_calls.append(
            {
                "snapshot": snapshot,
                "lease_id": lease["lease_id"],
                "clip_id": clip_id,
                "method": method,
                "range": range_header,
            }
        )
        if self.expire_during_read_once:
            self.expire_during_read_once = False
            lease["expires_at"] = _utc(
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
            raise CanonicalEditorError(
                "agent_preview_lease_expired",
                "The local TTL elapsed during admission.",
                status=403,
            )
        if self.reject_generic_lease:
            raise CanonicalEditorError(
                "agent_preview_lease_expired",
                "The lease budget or runtime authority is unavailable.",
                status=403,
            )
        if range_header in {"bytes=-", "bytes=bad", "bytes=0-1,3-4"}:
            status = 416
            body = b""
            headers = _media_headers(0, content_range=f"bytes */{len(self.body)}")
        elif range_header == "bytes=2-5":
            status = 206
            body = self.body[2:6]
            headers = _media_headers(
                len(body),
                content_range=f"bytes 2-5/{len(self.body)}",
            )
        else:
            status = 200
            body = self.body
            headers = _media_headers(len(body))
        if method == "HEAD":
            body = b""
        if self.early_eof_next:
            self.early_eof_next = False
            body = b""
            headers = _media_headers(max(1, len(self.body)))
        raw = _RawResponse(body, status=status, headers=headers)
        self.raws.append(raw)
        return AgentPreviewMediaResponse(
            raw,
            status=status,
            headers=headers,
            method=method,
        )

    def read_revision(self, revision, *, current):
        if revision != 1 or current.expected_timeline_head["revision"] != 2:
            raise AssertionError("history fake exposes only K=1 from N=2")
        return _historical_snapshot()


class _PreviewOnlyBackend(_PreviewBackend):
    can_edit = False
    can_restore = False


def _direct_session(manager: EditorServerManager, backend):
    handoff = manager.create_canonical_handoff(backend=backend)
    boot = parse_qs(urlsplit(handoff["editor_url"]).fragment)["boot"][0]
    token, state = manager.bootstrap(handoff["session_id"], boot)
    return handoff, token, state


class SafeCanonicalPlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )

    def tearDown(self) -> None:
        self.manager.close()

    def test_preview_proof_domains_match_core_and_preview_has_no_write_nonce(self):
        secret = "11" * 32
        body = {
            "object": "memolens.timeline_preview_lease_request",
            "schema_version": "1",
            "project_id": "proj_canonical",
            "observed_head": {"revision": 7, "content_sha256": "a" * 64},
            "request_nonce": "preview-request-nonce",
        }
        mint_fields = {
            "capability_id": "cap_preview",
            "method": "POST",
            "canonical_path": (
                "/v1/agent/creative/projects/proj_canonical/"
                "timeline/preview-leases"
            ),
            "database_uuid": "db-test",
        }
        self.assertEqual(
            timeline_preview_mint_proof(
                proof_secret=secret,
                body=body,
                **mint_fields,
            ),
            agent_preview_mint_proof(
                secret,
                body_sha256=canonical_sha256(body),
                **mint_fields,
            ),
        )
        read_fields = {
            "lease_id": "preview_" + "2" * 32,
            "request_nonce": "read-nonce",
            "method": "GET",
            "canonical_path": (
                "/v1/agent/preview-leases/preview_"
                + "2" * 32
                + "/clips/clip_7/media"
            ),
            "range_header": "bytes=9-",
        }
        self.assertEqual(
            timeline_preview_read_proof(lease_secret=secret, **read_fields),
            agent_preview_read_proof(secret, **read_fields),
        )
        client = AgentApiClient("http://127.0.0.1:8000")
        client._identity_checked = True
        with self.assertRaises(MemoLensError) as raised:
            client.request_nonce(
                "cap_preview",
                action=TIMELINE_PREVIEW_ACTION,
                proof_secret=secret,
            )
        self.assertEqual(raised.exception.code, "invalid_argument")

    def test_client_streams_and_accepts_large_range_less_head_without_read(self):
        lease = {
            "lease_id": "preview_" + "3" * 32,
            "lease_secret": "44" * 32,
        }
        url = (
            "http://127.0.0.1:8000/v1/agent/preview-leases/"
            + lease["lease_id"]
            + "/clips/clip_1/media"
        )
        large = 12 * 1024 * 1024
        raw = _RawResponse(
            b"not-read",
            status=200,
            url=url,
            headers=_media_headers(large),
        )
        opener = _OneResponseOpener(raw)
        client = AgentApiClient(
            "http://127.0.0.1:8000",
            opener=opener,
        )
        client._identity_checked = True
        response = client.timeline_preview_media(
            lease=lease,
            clip_id="clip_1",
            method="HEAD",
            range_header=None,
        )
        self.assertEqual(response.content_length, large)
        self.assertEqual(response.read(), b"")
        self.assertEqual(raw.read_sizes, [])
        response.close()

    def test_truncated_agent_stream_fails_and_closes(self):
        raw = _RawResponse(b"ab")
        response = AgentPreviewMediaResponse(
            raw,
            status=200,
            headers=_media_headers(5),
            method="GET",
        )
        self.assertEqual(response.read(2), b"ab")
        with self.assertRaises(MemoLensError) as raised:
            response.read(2)
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertTrue(raw.closed)

        class _GreedyRaw(_RawResponse):
            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return b"overflow"

        greedy = _GreedyRaw(b"")
        bounded = AgentPreviewMediaResponse(
            greedy,
            status=200,
            headers=_media_headers(3),
            method="GET",
        )
        with self.assertRaises(MemoLensError):
            bounded.read(1)
        self.assertTrue(greedy.closed)

    def test_client_rejects_redirect_and_closes_upstream(self):
        lease = {
            "lease_id": "preview_" + "5" * 32,
            "lease_secret": "66" * 32,
        }
        raw = _RawResponse(
            b"abcd",
            status=206,
            url="http://evil.invalid/redirected.mp4",
            headers=_media_headers(4, content_range="bytes 0-3/12"),
        )
        client = AgentApiClient(
            "http://127.0.0.1:8000",
            opener=_OneResponseOpener(raw),
        )
        client._identity_checked = True
        with self.assertRaises(MemoLensError) as raised:
            client.timeline_preview_media(
                lease=lease,
                clip_id="clip_1",
                method="GET",
                range_header="bytes=0-3",
            )
        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertTrue(raw.closed)

    def test_preview_only_session_is_read_only_but_playable_and_redacted(self):
        backend = _PreviewOnlyBackend()
        handoff, token, state = _direct_session(self.manager, backend)
        self.assertFalse(state["capabilities"]["save"])
        self.assertFalse(state["capabilities"]["move_clip"])
        self.assertFalse(state["capabilities"]["restore_revision"])
        self.assertTrue(state["capabilities"]["media_playback"])
        self.assertEqual(state["authority"]["actions"], [TIMELINE_PREVIEW_ACTION])
        self.assertEqual(
            set(state["playback"]),
            {
                "object",
                "schema_version",
                "mode",
                "output_muted",
                "audio_playback_enabled",
                "transport_may_include_audio",
                "audio_stream_attested",
                "final_fidelity",
                "clips",
                "available",
                "reason_code",
            },
        )
        self.assertEqual(state["playback"]["schema_version"], "2")
        self.assertIs(state["playback"]["output_muted"], True)
        self.assertIs(state["playback"]["audio_playback_enabled"], False)
        self.assertIs(state["playback"]["transport_may_include_audio"], True)
        self.assertIs(state["playback"]["audio_stream_attested"], False)
        self.assertIs(state["playback"]["final_fidelity"], False)
        self.assertNotIn("muted", state["playback"])
        self.assertNotIn("source_audio_included", state["playback"])
        self.assertIs(state["safety"]["source_preview_output_muted"], True)
        self.assertIs(
            state["safety"]["source_preview_audio_playback_enabled"],
            False,
        )
        self.assertIs(
            state["safety"]["source_preview_transport_may_include_audio"],
            True,
        )
        self.assertIs(
            state["safety"]["source_preview_audio_stream_attested"],
            False,
        )
        rendered = json.dumps({"handoff": handoff, "state": state})
        for forbidden in (
            "lease_id",
            "lease_secret",
            "issued_at",
            "expires_at",
            "observed_head",
            backend.snapshots[0].public["database_uuid"],
            "http://127.0.0.1:8000",
        ):
            self.assertNotIn(forbidden, rendered)
        with self.assertRaises(EditorServerError) as blocked:
            self.manager.apply_action(
                handoff["session_id"],
                token,
                {
                    "type": "stage",
                    "edit": {
                        "op": "trim_clip",
                        "clip_id": "clip_1",
                        "source_in_ms": 1200,
                        "source_out_ms": 2000,
                    },
                },
            )
        self.assertEqual(blocked.exception.status, 403)

    def test_stage_replace_history_refresh_save_and_close_stop_active_streams(self):
        backend = _PreviewBackend()
        handoff, token, _state = _direct_session(self.manager, backend)
        session_id = handoff["session_id"]
        stream = self.manager.clip_media(
            session_id,
            token,
            "clip_1",
            method="GET",
            range_header=None,
        )
        raw = backend.raws[-1]
        staged_trim = self.manager.apply_action(
            session_id,
            token,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": "clip_1",
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
        )
        self.assertTrue(staged_trim["capabilities"]["media_playback"])
        self.assertFalse(raw.closed)
        saved = self.manager.apply_action(
            session_id,
            token,
            {"type": "save"},
        )
        self.assertEqual(saved["last_save"]["status"], "saved")
        self.assertEqual(saved["projection"]["timeline_head"]["revision"], 2)
        self.assertTrue(raw.closed)
        with self.assertRaises(MemoLensError):
            stream.read()

        # Refresh remints current N and closes its registered stream.
        refreshed_stream = self.manager.clip_media(
            session_id,
            token,
            "clip_2",
            method="GET",
            range_header=None,
        )
        refreshed_raw = backend.raws[-1]
        self.manager.apply_action(session_id, token, {"type": "refresh"})
        self.assertTrue(refreshed_raw.closed)
        self.assertTrue(refreshed_stream.closed)

        # Historical K never inherits N's lease or stream.
        history_stream = self.manager.clip_media(
            session_id,
            token,
            "clip_2",
            method="GET",
            range_header=None,
        )
        history_raw = backend.raws[-1]
        selected = self.manager.apply_action(
            session_id,
            token,
            {"type": "select_revision", "revision": 1},
        )
        self.assertTrue(history_raw.closed)
        self.assertFalse(selected["capabilities"]["media_playback"])
        self.assertIsNone(
            self.manager._sessions[session_id].preview_lease  # noqa: SLF001
        )
        self.assertTrue(history_stream.closed)

        # A separate current session proves stage-replace and manager close.
        other = EditorServerManager(session_ttl_seconds=60, boot_ttl_seconds=30)
        other_backend = _PreviewBackend()
        try:
            other_handoff, other_token, _ = _direct_session(other, other_backend)
            other_id = other_handoff["session_id"]
            retained_id = other._sessions[other_id].preview_lease[  # noqa: SLF001
                "lease_id"
            ]
            other.apply_action(
                other_id,
                other_token,
                {
                    "type": "stage",
                    "edit": {
                        "op": "trim_clip",
                        "clip_id": "clip_1",
                        "source_in_ms": 1200,
                        "source_out_ms": 2000,
                    },
                },
            )
            other.apply_action(
                other_id,
                other_token,
                {"type": "discard"},
            )
            self.assertEqual(
                other._sessions[other_id].preview_lease["lease_id"],  # noqa: SLF001
                retained_id,
            )
            replace_stream = other.clip_media(
                other_id,
                other_token,
                "clip_1",
                method="GET",
                range_header=None,
            )
            replaced = other.apply_action(
                other_id,
                other_token,
                {
                    "type": "stage",
                    "edit": {
                        "op": "replace_clip",
                        "clip_id": "clip_1",
                        "assignment_id": "assign_image",
                    },
                },
            )
            self.assertTrue(replace_stream.closed)
            self.assertFalse(replaced["capabilities"]["media_playback"])
            discarded = other.apply_action(
                other_id,
                other_token,
                {"type": "discard"},
            )
            self.assertTrue(discarded["capabilities"]["media_playback"])
            self.assertNotEqual(
                other._sessions[other_id].preview_lease["lease_id"],  # noqa: SLF001
                retained_id,
            )
            close_stream = other.clip_media(
                other_id,
                other_token,
                "clip_1",
                method="GET",
                range_header=None,
            )
            other.close()
            self.assertTrue(close_stream.closed)
        finally:
            other.close()

    def test_http_get_head_range_closed_416_and_early_eof(self):
        backend = _PreviewBackend(
            body=b"x" * (EDITOR_MEDIA_STREAM_CHUNK_BYTES * 3 + 7)
        )
        handoff, token, _state = _direct_session(self.manager, backend)
        parsed = urlsplit(handoff["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        media_url = (
            f"{origin}/api/sessions/{handoff['session_id']}/clip-media/clip_1"
        )
        base_headers = {
            "Origin": origin,
            "Cookie": f"{EDITOR_COOKIE}={token}",
        }

        with build_opener().open(
            Request(media_url, headers=base_headers)
        ) as response:
            self.assertEqual(
                len(response.read()),
                EDITOR_MEDIA_STREAM_CHUNK_BYTES * 3 + 7,
            )
            self.assertEqual(
                response.headers.get_all("X-Content-Type-Options"),
                ["nosniff"],
            )
        self.assertGreaterEqual(len(backend.raws[-1].read_sizes), 4)
        self.assertLessEqual(
            max(backend.raws[-1].read_sizes),
            EDITOR_MEDIA_STREAM_CHUNK_BYTES,
        )

        with build_opener().open(
            Request(
                media_url,
                headers={**base_headers, "Range": "bytes=2-5"},
            )
        ) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/196615")
            self.assertEqual(response.read(), b"xxxx")

        with build_opener().open(
            Request(media_url, headers=base_headers, method="HEAD")
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        self.assertEqual(backend.raws[-1].read_sizes, [])

        for method in ("GET", "HEAD"):
            # Safe malformed syntax is signed and forwarded to Core, whose
            # closed 416 (including total and opaque ETag) is preserved.
            with self.subTest(method=method, range_case="core-closed"):
                with self.assertRaises(HTTPError) as rejected:
                    build_opener().open(
                        Request(
                            media_url,
                            headers={**base_headers, "Range": "bytes=-"},
                            method=method,
                        )
                    )
                with rejected.exception as response:
                    self.assertEqual(response.code, 416)
                    self.assertEqual(response.read(), b"")
                    self.assertEqual(response.headers["Content-Type"], "video/mp4")
                    self.assertEqual(response.headers["Content-Length"], "0")
                    self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                    self.assertEqual(
                        response.headers["Content-Range"],
                        "bytes */196615",
                    )
                    self.assertEqual(
                        response.headers["ETag"],
                        '"opaque-preview-etag"',
                    )

            # Oversized digits cannot reach int() or Core and still receive a
            # closed, empty media rejection rather than a JSON error body.
            with self.subTest(method=method, range_case="local-closed"):
                with self.assertRaises(HTTPError) as local_rejection:
                    build_opener().open(
                        Request(
                            media_url,
                            headers={
                                **base_headers,
                                "Range": "bytes=" + "9" * 30 + "-",
                            },
                            method=method,
                        )
                    )
                with local_rejection.exception as response:
                    self.assertEqual(response.code, 416)
                    self.assertEqual(response.read(), b"")
                    self.assertEqual(response.headers["Content-Type"], "video/mp4")
                    self.assertEqual(response.headers["Content-Length"], "0")
                    self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                    self.assertEqual(
                        response.headers["Cache-Control"],
                        "private, no-store",
                    )
                    self.assertEqual(response.headers["Pragma"], "no-cache")

        backend.early_eof_next = True
        with self.assertRaises(HTTPError) as truncated:
            build_opener().open(Request(media_url, headers=base_headers))
        with truncated.exception as response:
            self.assertEqual(response.code, 503)
            self.assertEqual(json.load(response)["error"]["code"], "invalid_response")
        self.assertTrue(backend.raws[-1].closed)

    def test_http_media_requires_origin_cookie_session_and_current_clip(self):
        backend = _PreviewBackend()
        handoff, token, _state = _direct_session(self.manager, backend)
        other_backend = _PreviewBackend()
        other_handoff, other_token, _ = _direct_session(
            self.manager,
            other_backend,
        )
        parsed = urlsplit(handoff["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        media_url = (
            f"{origin}/api/sessions/{handoff['session_id']}/clip-media/clip_1"
        )

        for method in ("GET", "HEAD"):
            for headers, expected in (
                ({"Origin": origin}, 401),
                (
                    {
                        "Origin": "http://evil.invalid",
                        "Cookie": f"{EDITOR_COOKIE}={token}",
                    },
                    403,
                ),
                (
                    {
                        "Origin": origin,
                        "Cookie": f"{EDITOR_COOKIE}={other_token}",
                    },
                    401,
                ),
            ):
                with (
                    self.subTest(method=method, expected=expected),
                    self.assertRaises(HTTPError) as denied,
                ):
                    build_opener().open(
                        Request(media_url, headers=headers, method=method)
                    )
                denied.exception.close()
                self.assertEqual(denied.exception.code, expected)

        unknown = (
            f"{origin}/api/sessions/{handoff['session_id']}/"
            "clip-media/clip_unknown"
        )
        for method in ("GET", "HEAD"):
            with (
                self.subTest(method=method, scope_case="unknown-clip"),
                self.assertRaises(HTTPError) as missing,
            ):
                build_opener().open(
                    Request(
                        unknown,
                        headers={
                            "Origin": origin,
                            "Cookie": f"{EDITOR_COOKIE}={token}",
                        },
                        method=method,
                    )
                )
            self.assertEqual(missing.exception.code, 404)
            missing.exception.close()
        self.assertNotEqual(handoff["session_id"], other_handoff["session_id"])
        self.assertEqual(backend.media_calls, [])
        self.assertEqual(other_backend.media_calls, [])

    def test_only_locally_elapsed_ttl_remints_once(self):
        backend = _PreviewBackend()
        handoff, token, _ = _direct_session(self.manager, backend)
        backend.expire_during_read_once = True
        stream = self.manager.clip_media(
            handoff["session_id"],
            token,
            "clip_1",
            method="GET",
            range_header=None,
        )
        self.assertEqual(backend.mint_count, 2)
        self.assertEqual(len(backend.media_calls), 2)
        stream.close()
        self.manager.release_preview_stream(handoff["session_id"], stream)

        other = EditorServerManager(session_ttl_seconds=60, boot_ttl_seconds=30)
        rejected_backend = _PreviewBackend()
        try:
            rejected_handoff, rejected_token, _ = _direct_session(
                other,
                rejected_backend,
            )
            rejected_backend.reject_generic_lease = True
            for method in ("GET", "HEAD"):
                with (
                    self.subTest(method=method, lease_case="generic-rejection"),
                    self.assertRaises(EditorServerError),
                ):
                    other.clip_media(
                        rejected_handoff["session_id"],
                        rejected_token,
                        "clip_1",
                        method=method,
                        range_header=None,
                    )
                self.assertEqual(rejected_backend.mint_count, 1)
                self.assertEqual(len(rejected_backend.media_calls), 1)
        finally:
            other.close()

    def test_ui_waits_for_metadata_then_seeks_source_in_before_play(self):
        html = (PLUGIN_ROOT / "ui" / "canonical-editor.html").read_text()
        self.assertIn('<link rel="icon" href="data:,">', html)
        self.assertEqual(html.count("<video"), 1)
        self.assertIn('muted playsinline preload="metadata"', html)
        self.assertIn(
            "Raw MP4 preview transport may contain audio. This page disables "
            "audio playback and keeps output muted. Audio-stream presence, "
            "content, and mix are not attested; this is not final-fidelity proof.",
            html,
        )
        sync = html[html.index("function syncActiveViewer") :]
        sync = sync[: sync.index("function pixelsPerMs")]
        self.assertLess(sync.index("readyState < 1"), sync.index(".play()"))
        self.assertLess(sync.index("currentTime = sourceTime"), sync.index(".play()"))
        self.assertLess(sync.index("enforceMutedPreview()"), sync.index(".play()"))
        self.assertIn("clip.source_in_ms + localMs", sync)
        self.assertIn("ui.sourceVideo.defaultMuted = true", html)
        self.assertIn("ui.sourceVideo.muted = true", html)
        self.assertIn("ui.sourceVideo.volume = 0", html)
        self.assertIn(
            "ui.sourceVideo.addEventListener('volumechange', enforceMutedPreview)",
            html,
        )
        self.assertIn("playback?.schema_version === '2'", html)
        self.assertIn("playback?.audio_playback_enabled === false", html)
        self.assertNotIn("source_audio_included", html)
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        checked = subprocess.run(
            ["node", "--check"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_preview_capability_is_independent_from_edit_and_restore(self):
        backend = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={"actions": [TIMELINE_PREVIEW_ACTION]},
            client=object(),  # type: ignore[arg-type]
        )
        self.assertTrue(backend.can_preview)
        self.assertFalse(backend.can_edit)
        self.assertFalse(backend.can_restore)


if __name__ == "__main__":
    unittest.main()
