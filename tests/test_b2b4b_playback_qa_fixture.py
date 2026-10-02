from __future__ import annotations

from http.cookiejar import CookieJar
import hashlib
import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = (PROJECT_ROOT / "scripts/run_b2b4b_playback_qa_fixture.py").resolve()
READY_KEYS = {
    "object",
    "schema_version",
    "status",
    "fixture_mode",
    "real_user_approval_claimed",
    "editor_url",
    "project_id",
    "clip_count",
    "codec",
    "approved_actions",
    "stdin_commands",
    "fixture_retained_on_shutdown",
}
SNAPSHOT_KEYS = {
    "object",
    "schema_version",
    "status",
    "fixture_mode",
    "real_user_approval_claimed",
    "project_id",
    "head",
    "clip_count",
    "editor_session",
    "canonical_ledgers",
}
COMMAND_OBJECT = "memolens.b2b4b_playback_qa_command"
FIXTURE_MODE = "automated_qa_native_approval_fixture_not_real_user_approval"
LEDGER_GROUPS = {"timeline", "usage", "export", "capability_write"}
FORBIDDEN_OUTPUT_FRAGMENTS = (
    "proof_secret",
    "capability_id",
    "pairing_id",
    "database_uuid",
    "asset_source",
    "source_path",
    "sqlite_db_path",
    "main_authority_token",
    "desktop_session_token",
)


def _video_tools_available() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        return False
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    return (
        completed.returncode == 0
        and b"libx264" in completed.stdout
        and b" aac " in completed.stdout
    )


def _read_json_line(process: subprocess.Popen[str], *, timeout: float) -> dict[str, object]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError("QA fixture did not produce a control response")
        line = process.stdout.readline()
    finally:
        selector.close()
    if not line:
        raise RuntimeError("QA fixture closed stdout before a control response")
    value = json.loads(line)
    if type(value) is not dict:
        raise AssertionError("QA fixture control response is not a JSON object")
    return value


def _command(process: subprocess.Popen[str], command: str) -> dict[str, object]:
    assert process.stdin is not None
    process.stdin.write(
        json.dumps(
            {
                "object": COMMAND_OBJECT,
                "schema_version": "1",
                "command": command,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    process.stdin.flush()
    return _read_json_line(process, timeout=20)


def _json_request(
    opener: object,
    url: str,
    *,
    method: str,
    origin: str,
    body: dict[str, object] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, object, dict[str, str]]:
    encoded = None
    headers = {"Origin": origin, "Accept": "application/json"}
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    request = Request(url, data=encoded, method=method, headers=headers)
    response = opener.open(request, timeout=10)  # type: ignore[attr-defined]
    with response:
        raw = response.read()
        status = int(response.status)
        response_headers = {key.casefold(): value for key, value in response.headers.items()}
    content_type = response_headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return status, json.loads(raw), response_headers
    return status, raw, response_headers


class B2B4BPlaybackQAFixtureTests(unittest.TestCase):
    maxDiff = None

    @unittest.skipUnless(
        _video_tools_available(),
        "local ffmpeg/ffprobe with libx264 are required",
    )
    def test_production_browser_playback_is_preview_only_and_read_pure(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-b2b4b-playback-test-parent-"
        ) as parent:
            fixture_root = (
                Path(parent) / "memolens-b2b4b-playback-integration"
            ).resolve()
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONWARNINGS": "error",
                    "MINIMAX_KEY": "",
                    "OPENAI_API_KEY": "",
                    "DASHSCOPE_API_KEY": "",
                    "VERTEX_ACCESS_TOKEN": "",
                    "GOOGLE_OAUTH_ACCESS_TOKEN": "",
                }
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LAUNCHER),
                    "--fixture-dir",
                    str(fixture_root),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
            control_responses: list[dict[str, object]] = []
            editor_page_url = ""
            try:
                ready = _read_json_line(process, timeout=90)
                control_responses.append(ready)
                self.assertEqual(set(ready), READY_KEYS)
                self.assertEqual(ready["object"], "memolens.b2b4b_playback_qa_ready")
                self.assertEqual(ready["schema_version"], "1")
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(ready["fixture_mode"], FIXTURE_MODE)
                self.assertIs(ready["real_user_approval_claimed"], False)
                self.assertEqual(ready["approved_actions"], ["timeline.preview_media"])
                self.assertEqual(ready["stdin_commands"], ["snapshot", "shutdown"])
                self.assertIs(ready["fixture_retained_on_shutdown"], False)
                self.assertEqual(ready["clip_count"], 2)
                self.assertEqual(
                    ready["codec"],
                    {
                        "container": "video/mp4",
                        "video_codec": "h264",
                        "pixel_format": "yuv420p",
                        "audio": True,
                        "audio_codec": "aac",
                        "audio_sample_rate_hz": 48_000,
                        "audio_channels": 2,
                        "faststart": True,
                    },
                )

                assert process.stdin is not None
                process.stdin.write(
                    json.dumps(
                        {
                            "object": COMMAND_OBJECT,
                            "schema_version": "1",
                            "command": "snapshot",
                            "extra": True,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                process.stdin.flush()
                rejected = _read_json_line(process, timeout=10)
                control_responses.append(rejected)
                self.assertEqual(
                    rejected,
                    {
                        "object": "memolens.b2b4b_playback_qa_error",
                        "schema_version": "1",
                        "status": "error",
                        "error": {
                            "code": "invalid_command",
                            "message": "The QA fixture command was rejected.",
                        },
                    },
                )

                self.assertTrue(fixture_root.is_dir())
                self.assertEqual(stat.S_IMODE(fixture_root.stat().st_mode), 0o700)
                videos = sorted((fixture_root / "library").glob("*.mp4"))
                self.assertEqual(len(videos), 2)
                source_sha256s = {
                    hashlib.sha256(video.read_bytes()).hexdigest()
                    for video in videos
                }
                for video in videos:
                    self.assertEqual(stat.S_IMODE(video.stat().st_mode), 0o600)
                    probed = subprocess.run(
                        [
                            str(shutil.which("ffprobe")),
                            "-v",
                            "error",
                            "-show_streams",
                            "-show_format",
                            "-of",
                            "json",
                            str(video),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=20,
                    )
                    self.assertEqual(probed.returncode, 0, probed.stderr.decode())
                    probe = json.loads(probed.stdout)
                    streams = probe["streams"]
                    video_streams = [
                        stream for stream in streams if stream.get("codec_type") == "video"
                    ]
                    audio_streams = [
                        stream for stream in streams if stream.get("codec_type") == "audio"
                    ]
                    self.assertEqual(len(video_streams), 1)
                    self.assertEqual(video_streams[0]["codec_name"], "h264")
                    self.assertEqual(video_streams[0]["pix_fmt"], "yuv420p")
                    self.assertEqual(len(audio_streams), 1)
                    self.assertEqual(audio_streams[0]["codec_name"], "aac")
                    self.assertEqual(audio_streams[0]["sample_rate"], "48000")
                    self.assertEqual(audio_streams[0]["channels"], 2)
                    exact = video.read_bytes()
                    self.assertLess(exact.find(b"moov"), exact.find(b"mdat"))
                    decoded = subprocess.run(
                        [
                            str(shutil.which("ffmpeg")),
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-nostdin",
                            "-i",
                            str(video),
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0",
                            "-f",
                            "null",
                            "-",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=20,
                    )
                    self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())

                baseline = _command(process, "snapshot")
                control_responses.append(baseline)
                self.assertEqual(set(baseline), SNAPSHOT_KEYS)
                self.assertEqual(
                    baseline["object"], "memolens.b2b4b_playback_qa_snapshot"
                )
                self.assertEqual(baseline["fixture_mode"], FIXTURE_MODE)
                self.assertIs(baseline["real_user_approval_claimed"], False)
                self.assertEqual(baseline["clip_count"], 2)
                self.assertEqual(
                    baseline["editor_session"],
                    {
                        "alive": True,
                        "status": "awaiting_bootstrap",
                        "preview_only": True,
                        "playable_clip_count": 2,
                    },
                )
                ledgers = baseline["canonical_ledgers"]
                self.assertIsInstance(ledgers, dict)
                assert isinstance(ledgers, dict)
                self.assertIs(ledgers["unchanged"], True)
                self.assertEqual(ledgers["baseline"], ledgers["current"])
                groups = ledgers["baseline"]["groups"]
                self.assertEqual(set(groups), LEDGER_GROUPS)
                self.assertEqual(groups["timeline"]["row_count"], 4)
                self.assertEqual(groups["usage"]["row_count"], 0)
                self.assertEqual(groups["export"]["row_count"], 0)
                self.assertEqual(groups["capability_write"]["row_count"], 3)

                editor_url = str(ready["editor_url"])
                parsed = urlsplit(editor_url)
                self.assertEqual(parsed.scheme, "http")
                self.assertIn(parsed.hostname, {"127.0.0.1", "::1"})
                self.assertNotEqual(parsed.port, 0)
                boot_values = parse_qs(parsed.fragment, strict_parsing=True)
                self.assertEqual(set(boot_values), {"boot"})
                self.assertEqual(len(boot_values["boot"]), 1)
                boot_token = boot_values["boot"][0]
                editor_origin = f"{parsed.scheme}://{parsed.netloc}"
                editor_page_url = urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, "", "")
                )
                session_id = parsed.path.rsplit("/", 1)[-1]
                self.assertTrue(session_id.startswith("canonical_"))
                jar = CookieJar()
                opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))
                status, html, html_headers = _json_request(
                    opener,
                    editor_page_url,
                    method="GET",
                    origin=editor_origin,
                )
                self.assertEqual(status, 200)
                self.assertIsInstance(html, bytes)
                self.assertTrue(
                    html_headers["content-type"].startswith("text/html")
                )
                bootstrap_url = (
                    f"{editor_origin}/api/sessions/{quote(session_id, safe='')}/bootstrap"
                )
                status, state, _headers = _json_request(
                    opener,
                    bootstrap_url,
                    method="POST",
                    origin=editor_origin,
                    body={"token": boot_token},
                )
                self.assertEqual(status, 200)
                self.assertIsInstance(state, dict)
                assert isinstance(state, dict)
                self.assertEqual(state["authority"]["actions"], ["timeline.preview_media"])
                self.assertIs(state["capabilities"]["media_playback"], True)
                for capability in (
                    "move_clip",
                    "trim_clip",
                    "set_clip_duration",
                    "replace_clip",
                    "restore_revision",
                    "save",
                ):
                    self.assertIs(state["capabilities"][capability], False)
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
                self.assertIs(
                    state["playback"]["audio_playback_enabled"],
                    False,
                )
                self.assertIs(
                    state["playback"]["transport_may_include_audio"],
                    True,
                )
                self.assertIs(
                    state["playback"]["audio_stream_attested"],
                    False,
                )
                self.assertIs(state["playback"]["final_fidelity"], False)
                self.assertNotIn("muted", state["playback"])
                self.assertNotIn("source_audio_included", state["playback"])
                clips = state["projection"]["timeline"]["tracks"][0]["clips"]
                self.assertEqual(len(clips), 2)
                self.assertTrue(
                    all(
                        clip["media_kind"] == "video"
                        and clip["source_in_ms"] > 0
                        for clip in clips
                    )
                )
                state_text = json.dumps(state, sort_keys=True)
                self.assertNotIn(str(fixture_root), state_text)
                for forbidden in (
                    "lease_secret",
                    "capability_id",
                    "database_uuid",
                    "asset_source_id",
                    "sha256",
                ):
                    self.assertNotIn(forbidden, state_text.casefold())

                proxied_sha256s: set[str] = set()
                for clip in clips:
                    clip_id = quote(str(clip["clip_id"]), safe="")
                    thumbnail_url = (
                        f"{editor_origin}/api/sessions/{quote(session_id, safe='')}"
                        f"/previews/{clip_id}"
                    )
                    status, thumbnail, thumbnail_headers = _json_request(
                        opener,
                        thumbnail_url,
                        method="GET",
                        origin=editor_origin,
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(thumbnail_headers["content-type"], "image/jpeg")
                    self.assertIsInstance(thumbnail, bytes)
                    assert isinstance(thumbnail, bytes)
                    self.assertTrue(thumbnail.startswith(b"\xff\xd8"))
                    self.assertTrue(thumbnail.endswith(b"\xff\xd9"))

                    media_url = (
                        f"{editor_origin}/api/sessions/{quote(session_id, safe='')}"
                        f"/clip-media/{clip_id}"
                    )
                    status, media, media_headers = _json_request(
                        opener,
                        media_url,
                        method="GET",
                        origin=editor_origin,
                        extra_headers={"Range": "bytes=0-"},
                    )
                    self.assertEqual(status, 206)
                    self.assertEqual(media_headers["content-type"], "video/mp4")
                    self.assertEqual(media_headers["accept-ranges"], "bytes")
                    self.assertTrue(media_headers["content-range"].startswith("bytes 0-"))
                    self.assertEqual(media_headers["cache-control"], "private, no-store")
                    self.assertIsInstance(media, bytes)
                    assert isinstance(media, bytes)
                    self.assertGreater(len(media), 32)
                    self.assertEqual(media[4:8], b"ftyp")
                    self.assertEqual(int(media_headers["content-length"]), len(media))
                    proxied_sha256s.add(hashlib.sha256(media).hexdigest())

                self.assertEqual(proxied_sha256s, source_sha256s)

                final = _command(process, "snapshot")
                control_responses.append(final)
                self.assertEqual(set(final), SNAPSHOT_KEYS)
                self.assertEqual(final["head"], baseline["head"])
                self.assertEqual(
                    final["editor_session"],
                    {
                        "alive": True,
                        "status": "active",
                        "preview_only": True,
                        "playable_clip_count": 2,
                    },
                )
                final_ledgers = final["canonical_ledgers"]
                self.assertIs(final_ledgers["unchanged"], True)
                self.assertEqual(final_ledgers["baseline"], final_ledgers["current"])
                self.assertEqual(final_ledgers["baseline"], ledgers["baseline"])

                shutdown = _command(process, "shutdown")
                control_responses.append(shutdown)
                self.assertEqual(
                    shutdown,
                    {
                        "object": "memolens.b2b4b_playback_qa_shutdown",
                        "schema_version": "1",
                        "status": "stopped",
                        "fixture_retained": False,
                    },
                )
                self.assertEqual(process.wait(timeout=20), 0)
                self.assertFalse(fixture_root.exists())
                assert process.stdout is not None
                self.assertEqual(process.stdout.read(), "")
                with self.assertRaises(URLError):
                    build_opener(ProxyHandler({})).open(editor_page_url, timeout=1)
            finally:
                if process.poll() is None:
                    try:
                        _command(process, "shutdown")
                    except Exception:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                assert process.stderr is not None
                stderr = process.stderr.read()
                self.assertNotIn("Traceback (most recent call last)", stderr)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

            encoded_output = "\n".join(
                json.dumps(value, sort_keys=True) for value in control_responses
            ).casefold()
            self.assertNotIn(str(fixture_root).casefold(), encoded_output)
            for forbidden in FORBIDDEN_OUTPUT_FRAGMENTS:
                self.assertNotIn(forbidden, encoded_output)
            for response in control_responses:
                for key, value in response.items():
                    if isinstance(value, str) and "://" in value:
                        self.assertEqual(key, "editor_url")
                        self.assertIn(
                            urlsplit(value).hostname,
                            {"127.0.0.1", "::1"},
                        )


if __name__ == "__main__":
    unittest.main()
