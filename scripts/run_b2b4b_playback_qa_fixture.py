#!/usr/bin/env python3
"""Run a private, production-wired B2B4B browser-playback QA fixture.

This launcher performs an automated native-authority approval for a synthetic
QA project.  It is not evidence of a real user's approval.  Its stdout is a
newline-delimited, closed JSON control channel; diagnostics belong on stderr.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import TracebackType
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from werkzeug.serving import WSGIRequestHandler, make_server


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = (
    PROJECT_ROOT / ".agents/plugins/plugins/memolens/scripts"
).resolve()
for import_root in (PROJECT_ROOT, PLUGIN_SCRIPTS):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backend.src import (  # noqa: E402
    MAIN_AUTHORITY_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from backend.src.media.video import ffprobe, resolve_binary  # noqa: E402
from core.config import Settings  # noqa: E402
from core.media_db import (  # noqa: E402
    SCHEMA_VERSION,
    MediaRepository,
    canonical_json,
)
from memolens_agent import AgentPairingWorkflow  # noqa: E402
from memolens_canonical_editor import (  # noqa: E402
    paired_canonical_editor_backend,
)
from memolens_editor_server import EditorServerManager  # noqa: E402


READY_OBJECT = "memolens.b2b4b_playback_qa_ready"
COMMAND_OBJECT = "memolens.b2b4b_playback_qa_command"
SNAPSHOT_OBJECT = "memolens.b2b4b_playback_qa_snapshot"
SHUTDOWN_OBJECT = "memolens.b2b4b_playback_qa_shutdown"
ERROR_OBJECT = "memolens.b2b4b_playback_qa_error"
SCHEMA_VERSION_STRING = "1"
FIXTURE_MODE = "automated_qa_native_approval_fixture_not_real_user_approval"
PREVIEW_ACTION = "timeline.preview_media"
FIXTURE_DIRECTORY = re.compile(
    r"^memolens-b2b4b-playback-[A-Za-z0-9._-]{1,80}$"
)
MAX_CONTROL_LINE_BYTES = 4_096
MAX_HTTP_JSON_BYTES = 1_048_576

LEDGER_GROUPS: dict[str, tuple[str, ...]] = {
    "timeline": (
        "canonical_timeline_operations",
        "canonical_timeline_revisions",
        "canonical_timeline_heads",
        "canonical_timeline_receipts",
    ),
    "usage": ("canonical_usage_occurrences",),
    "export": (
        "export_grants",
        "canonical_export_operations",
        "canonical_export_jobs",
        "canonical_export_revisions",
        "canonical_export_receipts",
    ),
    "capability_write": (
        "agent_project_capabilities",
        "agent_pairing_confirmation_receipts",
        "agent_project_command_receipts",
        "agent_project_capability_events",
    ),
}

_ENVIRONMENT = (
    "APP_CONFIG_PATH",
    "MEMOLENS_APP_STATE_DIR",
    "IMAGE_LIBRARY_DIR",
    "SQLITE_DB_PATH",
    "MEMOLENS_DESKTOP_SESSION_TOKEN",
    "MEMOLENS_MAIN_AUTHORITY_TOKEN",
    "MEMOLENS_RUNTIME_AUTHORITY_EPOCH",
    "MINIMAX_KEY",
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "VERTEX_ACCESS_TOKEN",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
)


class _SilentRequestHandler(WSGIRequestHandler):
    def log(self, type: str, message: str, *args: object) -> None:  # noqa: A002
        del type, message, args


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)


def _fixture_root(requested: Path | None) -> Path:
    if requested is None:
        root = Path(
            tempfile.mkdtemp(prefix="memolens-b2b4b-playback-")
        ).resolve()
        root.chmod(0o700)
        return root
    root = requested.expanduser().resolve()
    if FIXTURE_DIRECTORY.fullmatch(root.name) is None:
        raise ValueError(
            "--fixture-dir basename must start with memolens-b2b4b-playback-"
        )
    if _is_relative_to(root, PROJECT_ROOT):
        raise ValueError("--fixture-dir must be outside the MemoLens repository")
    if root.exists():
        identity = os.lstat(root)
        if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
            raise ValueError("--fixture-dir must be a real directory")
        if any(root.iterdir()):
            raise ValueError("--fixture-dir must be empty")
        root.chmod(0o700)
    else:
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_directory(root)
    return root


def _run_checked(command: list[str], *, timeout: float) -> None:
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if completed.returncode != 0:
        raise RuntimeError("local ffmpeg rejected the synthetic QA media")


def _require_video_tools() -> tuple[str, str]:
    ffmpeg = resolve_binary("ffmpeg")
    ffprobe_binary = resolve_binary("ffprobe")
    if ffmpeg is None or ffprobe_binary is None:
        raise RuntimeError("local ffmpeg and ffprobe 6+ are required")
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
        env={"PATH": os.environ.get("PATH", "")},
    )
    if (
        completed.returncode != 0
        or b"libx264" not in completed.stdout
        or b" aac " not in completed.stdout
    ):
        raise RuntimeError("local ffmpeg must provide libx264 and AAC encoders")
    return ffmpeg, ffprobe_binary


def _generate_video(
    ffmpeg: str,
    path: Path,
    *,
    color: str,
    tone_hz: int,
) -> dict[str, object]:
    _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:r=30:d=6",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={tone_hz}:sample_rate=48000:duration=6",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-movflags",
            "+faststart",
            "-metadata",
            "comment=MemoLens automated QA native-approval fixture",
            "-y",
            str(path),
        ],
        timeout=30,
    )
    path.chmod(0o600)
    content = path.read_bytes()
    if content.find(b"moov") < 0 or content.find(b"mdat") < 0:
        raise RuntimeError("synthetic MP4 is missing required container atoms")
    if content.find(b"moov") > content.find(b"mdat"):
        raise RuntimeError("synthetic MP4 was not written with +faststart")
    _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        timeout=30,
    )
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }


def _generate_keyframe(
    ffmpeg: str,
    video_path: Path,
    destination: Path,
) -> dict[str, object]:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-ss",
            "3",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(destination),
        ],
        timeout=30,
    )
    destination.chmod(0o600)
    content = destination.read_bytes()
    if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
        raise RuntimeError("synthetic representative keyframe is not JPEG")
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "width": 320,
        "height": 180,
    }


def _semantic(first_ref: str, second_ref: str) -> dict[str, object]:
    return {
        "intent": {
            "goal": "verify production-wired safe canonical source playback",
            "stance": "playback must not become canonical editing authority",
            "audience": "MemoLens QA reviewers",
            "platform": "short-video",
        },
        "script": {
            "blocks": [
                {"block_id": "opening", "text": "Play the exact first source span."},
                {"block_id": "ending", "text": "Continue into the exact second span."},
            ]
        },
        "direction": {
            "theme": "safe source playback",
            "narrative_arc": "first clip to second clip",
            "emotion": "calm",
            "tone": "precise",
            "pace": "measured",
        },
        "output": {"duration_target_ms": 8_000, "aspect_ratio": "16:9"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [
            {
                "hint_id": "opening_video",
                "evidence_ref": first_ref,
                "script_block_ids": ["opening"],
                "reason": "Use the first verified H.264 source span.",
            },
            {
                "hint_id": "ending_video",
                "evidence_ref": second_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the second verified H.264 source span.",
            },
        ],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        content = bytes(value)
        return {
            "binary_sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    raise TypeError("unsupported SQLite value in canonical QA ledger")


def _ledger_snapshot(db_path: Path) -> dict[str, object]:
    groups: dict[str, object] = {}
    all_rows: dict[str, object] = {}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for group_name, table_names in LEDGER_GROUPS.items():
            tables: dict[str, int] = {}
            group_rows: dict[str, object] = {}
            for table_name in table_names:
                rows = [
                    {key: _json_safe(row[key]) for key in row.keys()}
                    for row in connection.execute(
                        f'SELECT * FROM "{table_name}" ORDER BY rowid'
                    ).fetchall()
                ]
                tables[table_name] = len(rows)
                group_rows[table_name] = rows
                all_rows[table_name] = rows
            groups[group_name] = {
                "row_count": sum(tables.values()),
                "tables": tables,
                "canonical_sha256": hashlib.sha256(
                    canonical_json(group_rows).encode("utf-8")
                ).hexdigest(),
            }
        connection.rollback()
    return {
        "groups": groups,
        "canonical_sha256": hashlib.sha256(
            canonical_json(all_rows).encode("utf-8")
        ).hexdigest(),
    }


def _http_json(
    url: str,
    *,
    method: str,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    encoded = None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        encoded = canonical_json(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = Request(url, data=encoded, method=method, headers=request_headers)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=5)
    except HTTPError as exc:
        exc.close()
        raise RuntimeError("production authority rejected fixture setup") from exc
    with response:
        raw = response.read(MAX_HTTP_JSON_BYTES + 1)
        if len(raw) > MAX_HTTP_JSON_BYTES:
            raise RuntimeError("production authority response exceeded QA bound")
        if not 200 <= response.status < 300:
            raise RuntimeError("production authority returned an unexpected status")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("production authority returned invalid JSON") from exc
    if type(value) is not dict:
        raise RuntimeError("production authority returned a non-object")
    return value


class PlaybackQAFixture:
    """Own the synthetic media, production authority, pairing, and editor."""

    def __init__(self, *, fixture_dir: Path | None, keep_fixture: bool) -> None:
        if keep_fixture and fixture_dir is None:
            raise ValueError("--keep-fixture requires an explicit --fixture-dir")
        self.keep_fixture = keep_fixture
        self.root = _fixture_root(fixture_dir)
        self.library = self.root / "library"
        self.state = self.root / "state"
        self.db_path = self.state / "media.db"
        self.app: Any = None
        self.authority_server: Any = None
        self.authority_thread: threading.Thread | None = None
        self.editor_manager: EditorServerManager | None = None
        self.project_id: str | None = None
        self.session_id: str | None = None
        self.handoff: dict[str, Any] | None = None
        self.baseline: dict[str, object] | None = None
        self.clip_ids: tuple[str, ...] = ()
        self._closed = False
        self._environment_before = {key: os.environ.get(key) for key in _ENVIRONMENT}
        self._main_token = secrets.token_urlsafe(32)
        self._desktop_token = secrets.token_urlsafe(32)
        self._runtime_epoch = f"epoch_{secrets.token_urlsafe(24)}"

    def __enter__(self) -> PlaybackQAFixture:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()

    def _configure_environment(self) -> None:
        values = {
            "APP_CONFIG_PATH": str(PROJECT_ROOT / "config.yaml"),
            "MEMOLENS_APP_STATE_DIR": str(self.state),
            "IMAGE_LIBRARY_DIR": str(self.library),
            "SQLITE_DB_PATH": str(self.db_path),
            "MEMOLENS_DESKTOP_SESSION_TOKEN": self._desktop_token,
            "MEMOLENS_MAIN_AUTHORITY_TOKEN": self._main_token,
            "MEMOLENS_RUNTIME_AUTHORITY_EPOCH": self._runtime_epoch,
            "MINIMAX_KEY": "",
            "OPENAI_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "VERTEX_ACCESS_TOKEN": "",
            "GOOGLE_OAUTH_ACCESS_TOKEN": "",
        }
        os.environ.update(values)

    def _restore_environment(self) -> None:
        for key, previous in self._environment_before.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    def _register_video_span(
        self,
        repository: MediaRepository,
        *,
        ffmpeg: str,
        root_id: str,
        path: Path,
        probe: dict[str, object],
        ordinal: int,
    ) -> str:
        content = path.read_bytes()
        observed = path.stat()
        digest = hashlib.sha256(content).hexdigest()
        asset = repository.upsert_asset_source(
            root_id=root_id,
            relative_path=path.name,
            filename=path.name,
            kind="video",
            sha256=digest,
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        repository.update_asset_probe(asset_id, probe)
        job = repository.create_analysis_job(
            asset_id=asset_id,
            analysis_profile_id="b2b4b-playback-qa-v1",
        )
        revision = int(job["analysis_revision"])
        segment_id = (
            f"seg_{asset_id.removeprefix('asset_')}_{revision}_0"
        )
        duration_ms = int(probe["duration_ms"])
        source_out_ms = min(duration_ms, 5_500)
        if source_out_ms <= 5_000:
            raise RuntimeError("synthetic video is too short for the QA Timeline")
        cache_root = self.state / "media-cache"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_root.chmod(0o700)
        keyframe_path = cache_root / "keyframes" / f"{segment_id}-3000.jpg"
        keyframe = _generate_keyframe(ffmpeg, path, keyframe_path)
        committed = repository.commit_video_analysis(
            job_id=str(job["id"]),
            segments=[
                {
                    "id": segment_id,
                    "ordinal": 0,
                    "start_ms": 1_000,
                    "end_ms": source_out_ms,
                    "boundary_reason": "b2b4b-playback-qa",
                    "summary": f"Automated QA source clip {ordinal}.",
                    "semantic": {
                        "tags": ["video", "automated qa", "safe playback"],
                        "external_analysis": False,
                    },
                    "visible_text": None,
                    "combined_text": f"automated QA source clip {ordinal}",
                    "visual_status": "local_fallback",
                    "transcript_status": "unavailable",
                    "confidence": None,
                }
            ],
            keyframes=[
                {
                    "id": f"kf_{digest[:32]}",
                    "segment_id": segment_id,
                    "timestamp_ms": 3_000,
                    "cache_key": keyframe_path.relative_to(cache_root).as_posix(),
                    "sha256": keyframe["sha256"],
                    "width": keyframe["width"],
                    "height": keyframe["height"],
                    "selection_reason": "Automated QA midpoint representative.",
                    "clarity_score": None,
                    "novelty_score": None,
                    "is_representative": True,
                }
            ],
            transcripts=[],
        )
        if (
            committed.get("segment_count") != 1
            or committed.get("keyframe_count") != 1
        ):
            raise RuntimeError("production video analysis commit was incomplete")
        return f"memolens://evidence/span/{segment_id}"

    def _build_project(
        self,
        video_paths: tuple[Path, Path],
        ffmpeg: str,
        ffprobe_binary: str,
    ) -> None:
        repository = self.app.extensions["media_repository"]
        if not isinstance(repository, MediaRepository):
            raise RuntimeError("production MediaRepository is unavailable")
        if SCHEMA_VERSION != 20:
            raise RuntimeError("B2B4B playback QA requires the V20 schema")
        root_id = str(repository.register_library_root(self.library)["id"])
        evidence_refs = tuple(
            self._register_video_span(
                repository,
                ffmpeg=ffmpeg,
                root_id=root_id,
                path=path,
                probe=ffprobe(path, ffprobe_binary),
                ordinal=index,
            )
            for index, path in enumerate(video_paths, start=1)
        )
        project = repository.create_project(
            "B2B4B production playback QA",
            {
                "goal": "automated QA native-approval fixture",
                "duration_ms": 8_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [],
            },
            {
                "created_by": "run_b2b4b_playback_qa_fixture",
                "real_user_approval_claimed": False,
            },
        )
        self.project_id = str(project["id"])
        brief = repository.get_brief(self.project_id, 1)
        if brief is None:
            raise RuntimeError("fixture brief was not persisted")
        blueprints = self.app.extensions["blueprint_service"]
        coverage = self.app.extensions["coverage_service"]
        timelines = self.app.extensions["timeline_lowering_service"]
        created = blueprints.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": _semantic(*evidence_refs),
            },
            idempotency_key="b2b4b-playback-blueprint",
        )
        if created.response_status != 201:
            raise RuntimeError("fixture Blueprint revision one was not created")
        blueprint = repository.get_blueprint_head(self.project_id)
        if blueprint is None:
            raise RuntimeError("fixture Blueprint head is unavailable")
        materialized = coverage.materialize_baseline(
            self.project_id,
            {
                "expected_blueprint": {
                    key: blueprint[key]
                    for key in ("revision", "content_sha256", "semantic_sha256")
                },
                "expected_plan_head": None,
            },
            idempotency_key="b2b4b-playback-coverage",
            expected_database_uuid=repository.database_uuid,
        )
        if materialized.response_status != 201:
            raise RuntimeError("fixture Coverage revision one was not created")
        coverage_head = repository.get_coverage_head(self.project_id)
        if coverage_head is None:
            raise RuntimeError("fixture Coverage head is unavailable")
        lowered = timelines.materialize_first_cut(
            self.project_id,
            {
                "expected_blueprint": {
                    key: blueprint[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                        "operation_id",
                    )
                },
                "expected_coverage": {
                    key: coverage_head[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "evidence_manifest_sha256",
                        "operation_id",
                    )
                },
                "expected_timeline_head": None,
            },
            idempotency_key="b2b4b-playback-timeline",
            expected_database_uuid=repository.database_uuid,
        )
        if lowered.response_status != 201:
            raise RuntimeError("fixture canonical Timeline revision one was not created")
        workspace = timelines.read(self.project_id)
        timeline = workspace.get("timeline")
        clips = (
            timeline.get("tracks", [{}])[0].get("clips")
            if isinstance(timeline, dict)
            else None
        )
        if (
            workspace.get("freshness") != {"state": "current", "reasons": []}
            or not isinstance(clips, list)
            or len(clips) != 2
            or any(
                not isinstance(clip, dict)
                or clip.get("media_kind") != "video"
                or int(clip.get("source_in_ms", -1)) <= 0
                for clip in clips
            )
        ):
            raise RuntimeError("fixture must expose two current video clips")
        self.clip_ids = tuple(str(clip["clip_id"]) for clip in clips)

    def _start_authority(self) -> str:
        self.authority_server = make_server(
            "127.0.0.1",
            0,
            self.app,
            threaded=True,
            request_handler=_SilentRequestHandler,
        )
        self.authority_thread = threading.Thread(
            target=self.authority_server.serve_forever,
            name="b2b4b-playback-authority",
            daemon=True,
        )
        self.authority_thread.start()
        return f"http://127.0.0.1:{self.authority_server.server_port}"

    def _pair_preview_only(self, authority_origin: str) -> None:
        assert self.project_id is not None
        repository = self.app.extensions["media_repository"]
        blueprint = repository.get_blueprint_head(self.project_id)
        if blueprint is None:
            raise RuntimeError("fixture Blueprint head disappeared before pairing")
        workflow = AgentPairingWorkflow(base_url=authority_origin, timeout=5)
        pending = workflow.create(
            project_id=self.project_id,
            current_head=blueprint,
            claimed_client_label="MemoLens automated B2B4B playback QA fixture",
            ttl_seconds=900,
            max_operations=4,
            actions=[PREVIEW_ACTION],
        )
        pairing_id = pending.get("pairing_id")
        if type(pairing_id) is not str:
            raise RuntimeError("fixture pairing did not expose an opaque pairing id")
        presentation = _http_json(
            f"{authority_origin}/v1/main/agent/pairings/{pairing_id}/presentation",
            method="GET",
            headers={MAIN_AUTHORITY_HEADER: self._main_token},
        )
        digest = presentation.get("presentation_sha256")
        if type(digest) is not str:
            raise RuntimeError("fixture pairing presentation digest is unavailable")
        _http_json(
            f"{authority_origin}/v1/main/agent/pairings/{pairing_id}/approve",
            method="POST",
            body={
                "presentation_sha256": digest,
                "native_gesture_nonce": "automated_qa_native_approval_fixture",
            },
            headers={MAIN_AUTHORITY_HEADER: self._main_token},
        )
        status = workflow.status(self.project_id)
        if (
            status.get("status") != "active"
            or status.get("actions") != [PREVIEW_ACTION]
            or status.get("preview_ready") is not True
            or status.get("write_ready") is not False
        ):
            raise RuntimeError("fixture pairing did not become preview-only")

    def start(self) -> PlaybackQAFixture:
        try:
            _private_directory(self.library)
            _private_directory(self.state)
            ffmpeg, ffprobe_binary = _require_video_tools()
            video_paths = (
                self.library / "opening-h264.mp4",
                self.library / "ending-h264.mp4",
            )
            _generate_video(
                ffmpeg,
                video_paths[0],
                color="0x2457d6",
                tone_hz=440,
            )
            _generate_video(
                ffmpeg,
                video_paths[1],
                color="0x32a86b",
                tone_hz=660,
            )
            self._configure_environment()
            self.app = create_app(Settings.from_env())
            self._build_project(video_paths, ffmpeg, ffprobe_binary)
            authority_origin = self._start_authority()
            self._pair_preview_only(authority_origin)
            assert self.project_id is not None
            backend = paired_canonical_editor_backend(self.project_id, timeout=5)
            if not backend.can_preview or backend.can_edit or backend.can_restore:
                raise RuntimeError("production editor backend is not preview-only")
            self.editor_manager = EditorServerManager()
            self.handoff = self.editor_manager.create_canonical_handoff(
                backend=backend
            )
            self.session_id = str(self.handoff["session_id"])
            self.baseline = _ledger_snapshot(self.db_path)
            return self
        except BaseException:
            self.close()
            raise

    def ready(self) -> dict[str, object]:
        if self.handoff is None or self.project_id is None:
            raise RuntimeError("fixture is not ready")
        return {
            "object": READY_OBJECT,
            "schema_version": SCHEMA_VERSION_STRING,
            "status": "ready",
            "fixture_mode": FIXTURE_MODE,
            "real_user_approval_claimed": False,
            "editor_url": self.handoff["editor_url"],
            "project_id": self.project_id,
            "clip_count": len(self.clip_ids),
            "codec": {
                "container": "video/mp4",
                "video_codec": "h264",
                "pixel_format": "yuv420p",
                "audio": True,
                "audio_codec": "aac",
                "audio_sample_rate_hz": 48_000,
                "audio_channels": 2,
                "faststart": True,
            },
            "approved_actions": [PREVIEW_ACTION],
            "stdin_commands": ["snapshot", "shutdown"],
            "fixture_retained_on_shutdown": self.keep_fixture,
        }

    def _session_state(self) -> dict[str, object]:
        manager = self.editor_manager
        if manager is None or self.session_id is None:
            return {
                "alive": False,
                "status": "closed",
                "preview_only": False,
                "playable_clip_count": 0,
            }
        with manager._lock:  # Fixture-only introspection; no authority leaves stdout.
            session = manager._sessions.get(self.session_id)
            alive = session is not None and session.expires_at > time.monotonic()
            if not alive or session is None:
                return {
                    "alive": False,
                    "status": "expired",
                    "preview_only": False,
                    "playable_clip_count": 0,
                }
            backend = getattr(session, "backend", None)
            preview_lease = getattr(session, "preview_lease", None)
            clips = preview_lease.get("clips", []) if isinstance(preview_lease, dict) else []
            playable = sum(
                1
                for clip in clips
                if isinstance(clip, dict) and clip.get("status") == "playable"
            )
            return {
                "alive": True,
                "status": (
                    "active"
                    if getattr(session, "auth_token_sha256", None)
                    else "awaiting_bootstrap"
                ),
                "preview_only": bool(
                    getattr(backend, "can_preview", False)
                    and not getattr(backend, "can_edit", True)
                    and not getattr(backend, "can_restore", True)
                ),
                "playable_clip_count": playable,
            }

    def snapshot(self) -> dict[str, object]:
        if self.app is None or self.project_id is None or self.baseline is None:
            raise RuntimeError("fixture is not ready")
        timelines = self.app.extensions["timeline_lowering_service"]
        workspace = timelines.read(self.project_id)
        head = workspace.get("head")
        if not isinstance(head, dict):
            raise RuntimeError("canonical Timeline head is unavailable")
        current = _ledger_snapshot(self.db_path)
        return {
            "object": SNAPSHOT_OBJECT,
            "schema_version": SCHEMA_VERSION_STRING,
            "status": "ok",
            "fixture_mode": FIXTURE_MODE,
            "real_user_approval_claimed": False,
            "project_id": self.project_id,
            "head": {
                "revision": head["revision"],
                "timeline_id": head["timeline_id"],
                "timeline_content_sha256": head["timeline_content_sha256"],
            },
            "clip_count": len(self.clip_ids),
            "editor_session": self._session_state(),
            "canonical_ledgers": {
                "baseline": self.baseline,
                "current": current,
                "unchanged": current == self.baseline,
            },
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        editor_manager = self.editor_manager
        self.editor_manager = None
        if editor_manager is not None:
            try:
                editor_manager.close()
            except BaseException as exc:
                errors.append(exc)
        server = self.authority_server
        thread = self.authority_thread
        self.authority_server = None
        self.authority_thread = None
        if server is not None:
            try:
                server.shutdown()
            except BaseException as exc:
                errors.append(exc)
            try:
                server.server_close()
            except BaseException as exc:
                errors.append(exc)
        if thread is not None:
            try:
                thread.join(timeout=5)
                if thread.is_alive():
                    errors.append(
                        RuntimeError("production authority server did not stop")
                    )
            except BaseException as exc:
                errors.append(exc)
        app = self.app
        self.app = None
        if app is not None:
            try:
                preview_broker = app.extensions.get("agent_preview_broker")
                drop_all = getattr(preview_broker, "drop_all", None)
                if callable(drop_all):
                    drop_all()
                shutdown_runtime_extensions(app.extensions)
            except BaseException as exc:
                errors.append(exc)
        try:
            self._restore_environment()
        except BaseException as exc:
            errors.append(exc)
        self._main_token = ""
        self._desktop_token = ""
        self._runtime_epoch = ""
        if not self.keep_fixture and self.root.exists():
            if (
                FIXTURE_DIRECTORY.fullmatch(self.root.name) is None
                or _is_relative_to(self.root, PROJECT_ROOT)
            ):
                errors.append(
                    RuntimeError("refusing to clean an unsafe fixture directory")
                )
            else:
                try:
                    shutil.rmtree(self.root)
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]


def _closed_command(line: str) -> str:
    if len(line.encode("utf-8")) > MAX_CONTROL_LINE_BYTES:
        raise ValueError("command_too_large")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_command_json") from exc
    if (
        type(value) is not dict
        or set(value) != {"object", "schema_version", "command"}
        or value.get("object") != COMMAND_OBJECT
        or value.get("schema_version") != SCHEMA_VERSION_STRING
        or value.get("command") not in {"snapshot", "shutdown"}
    ):
        raise ValueError("invalid_command")
    return str(value["command"])


def _emit(value: dict[str, object]) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        flush=True,
    )


def _error(code: str) -> dict[str, object]:
    return {
        "object": ERROR_OBJECT,
        "schema_version": SCHEMA_VERSION_STRING,
        "status": "error",
        "error": {
            "code": code,
            "message": "The QA fixture command was rejected.",
        },
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an automated QA native-approval fixture for production-wired "
            "B2B4B canonical source playback. This does not claim real user approval."
        )
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        help=(
            "Fresh or empty private directory outside the repository; basename must "
            "start with memolens-b2b4b-playback-."
        ),
    )
    parser.add_argument(
        "--keep-fixture",
        action="store_true",
        help="Retain the explicitly selected evidence directory after shutdown.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    fixture = PlaybackQAFixture(
        fixture_dir=args.fixture_dir,
        keep_fixture=bool(args.keep_fixture),
    )

    def _stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        fixture.start()
        _emit(fixture.ready())
        while True:
            line = sys.stdin.readline()
            if line == "":
                break
            try:
                command = _closed_command(line)
            except ValueError as exc:
                _emit(_error(str(exc)))
                continue
            if command == "snapshot":
                _emit(fixture.snapshot())
                continue
            fixture.close()
            _emit(
                {
                    "object": SHUTDOWN_OBJECT,
                    "schema_version": SCHEMA_VERSION_STRING,
                    "status": "stopped",
                    "fixture_retained": bool(args.keep_fixture),
                }
            )
            return 0
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        fixture.close()


if __name__ == "__main__":
    raise SystemExit(main())
