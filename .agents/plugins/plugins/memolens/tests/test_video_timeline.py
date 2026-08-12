from __future__ import annotations

from contextlib import closing
from copy import deepcopy
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


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_core import (  # noqa: E402
    TRUST_LOCAL_API_ENV,
    MemoLensError,
    MemoLensGateway,
)
from memolens_mcp import SERVER_INFO, TOOLS  # noqa: E402
from memolens_timeline import (  # noqa: E402
    TimelineInputError,
    draft_timeline,
    revise_timeline_draft,
    validate_timeline,
)


VIDEO_SHA = "a" * 64
IMAGE_SHA = "b" * 64
AUDIO_SHA = "c" * 64


def _draft_items() -> list[dict[str, object]]:
    return [
        {
            "kind": "video",
            "asset_id": "asset_video",
            "asset_source_id": "src_video",
            "asset_sha256": VIDEO_SHA,
            "segment_id": "seg_current",
            "analysis_run_id": "arun_current",
            "analysis_revision": 1,
            "source_in_ms": 1000,
            "source_out_ms": 4000,
            "timeline_duration_ms": 3000,
            "reason": "开场建立海边环境",
            "match_id": "seg_current",
        }
    ]


def _make_timeline(created_at: str = "2026-08-12T12:00:00Z") -> dict:
    return draft_timeline(
        project_id="proj_demo",
        items=_draft_items(),
        created_at=created_at,
    )


class VideoTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "media.db"
        self._create_final_database(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _create_final_database(path: Path) -> None:
        timeline = _make_timeline()
        serialized = json.dumps(
            timeline, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE library_roots (
                    id TEXT PRIMARY KEY,
                    canonical_path TEXT,
                    status TEXT NOT NULL
                );
                CREATE TABLE image_index (
                    id INTEGER PRIMARY KEY,
                    relative_path TEXT,
                    filename TEXT,
                    description TEXT,
                    combined_text TEXT,
                    tags_json TEXT
                );
                CREATE TABLE assets (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime_type TEXT,
                    file_size INTEGER,
                    duration_ms INTEGER,
                    width INTEGER,
                    height INTEGER,
                    rotation_degrees INTEGER,
                    codec_json TEXT,
                    captured_at TEXT,
                    probe_status TEXT,
                    error_code TEXT
                );
                CREATE TABLE asset_sources (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    library_root_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    display_filename TEXT NOT NULL,
                    availability TEXT NOT NULL,
                    is_preferred INTEGER NOT NULL
                );
                CREATE TABLE analysis_runs (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE asset_analysis_heads (
                    asset_id TEXT PRIMARY KEY,
                    analysis_run_id TEXT NOT NULL
                );
                CREATE TABLE video_segments (
                    id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    analysis_run_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    boundary_reason TEXT,
                    summary TEXT,
                    semantic_json TEXT,
                    visible_text TEXT,
                    combined_text TEXT NOT NULL,
                    visual_status TEXT,
                    transcript_status TEXT,
                    confidence REAL
                );
                CREATE VIEW current_video_segments AS
                SELECT s.*, r.revision AS analysis_revision
                FROM video_segments s
                JOIN asset_analysis_heads h
                  ON h.asset_id=s.asset_id AND h.analysis_run_id=s.analysis_run_id
                JOIN analysis_runs r
                  ON r.id=s.analysis_run_id AND r.status='succeeded';
                CREATE TABLE timelines (
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    schema_version TEXT NOT NULL,
                    timeline_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(id, revision)
                );
                """
            )
            connection.execute(
                "INSERT INTO library_roots VALUES (?, ?, ?)",
                ("root_media", "/private/not-returned", "active"),
            )
            connection.execute(
                "INSERT INTO image_index VALUES (?, ?, ?, ?, ?, ?)",
                (
                    1,
                    "image/海边.jpg",
                    "海边.jpg",
                    "海边日落照片",
                    "海边 日落 宁静",
                    '["海边", "日落"]',
                ),
            )
            connection.executemany(
                "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "asset_video",
                        "video",
                        VIDEO_SHA,
                        "video/mp4",
                        100,
                        10_000,
                        1920,
                        1080,
                        0,
                        "{}",
                        None,
                        "ready",
                        None,
                    ),
                    (
                        "asset_image",
                        "image",
                        IMAGE_SHA,
                        "image/jpeg",
                        20,
                        None,
                        1200,
                        900,
                        0,
                        "{}",
                        None,
                        "ready",
                        None,
                    ),
                    (
                        "asset_audio",
                        "audio",
                        AUDIO_SHA,
                        "audio/wav",
                        30,
                        15_000,
                        None,
                        None,
                        0,
                        "{}",
                        None,
                        "ready",
                        None,
                    ),
                ],
            )
            connection.executemany(
                "INSERT INTO asset_sources VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "src_video",
                        "asset_video",
                        "root_media",
                        "video/日落.mp4",
                        "日落.mp4",
                        "available",
                        1,
                    ),
                    (
                        "src_image",
                        "asset_image",
                        "root_media",
                        "image/海边.jpg",
                        "海边.jpg",
                        "available",
                        1,
                    ),
                    (
                        "src_audio",
                        "asset_audio",
                        "root_media",
                        "audio/浪声.wav",
                        "浪声.wav",
                        "available",
                        1,
                    ),
                ],
            )
            connection.executemany(
                "INSERT INTO analysis_runs VALUES (?, ?, ?, ?)",
                [
                    ("arun_current", "asset_video", 1, "succeeded"),
                    ("arun_failed", "asset_video", 2, "failed"),
                ],
            )
            connection.execute(
                "INSERT INTO asset_analysis_heads VALUES (?, ?)",
                ("asset_video", "arun_current"),
            )
            connection.executemany(
                "INSERT INTO video_segments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "seg_current",
                        "asset_video",
                        "arun_current",
                        0,
                        1000,
                        4000,
                        "shot",
                        "海边日落延时",
                        '{"mood":"宁静"}',
                        "日落",
                        "海边 日落 宁静",
                        "complete",
                        "complete",
                        0.91,
                    ),
                    (
                        "seg_failed",
                        "asset_video",
                        "arun_failed",
                        0,
                        0,
                        5000,
                        "shot",
                        "失败分析中的幽灵片段",
                        "{}",
                        "幽灵",
                        "幽灵 失败",
                        "failed",
                        "failed",
                        None,
                    ),
                ],
            )
            connection.execute(
                "INSERT INTO timelines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    timeline["id"],
                    timeline["project_id"],
                    1,
                    "1.0",
                    serialized,
                    hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                    json.dumps(timeline["provenance"], ensure_ascii=False),
                    "valid",
                    "2026-08-12T12:00:00Z",
                ),
            )
            connection.commit()

    def gateway(self, path: Path | None = None, *, trust: bool = False) -> MemoLensGateway:
        with mock.patch.dict(
            os.environ,
            {TRUST_LOCAL_API_ENV: "1" if trust else "0"},
            clear=False,
        ):
            return MemoLensGateway(
                db_path=path or self.db,
                base_url="http://127.0.0.1:1",
                timeout=0.1,
            )

    def test_video_search_uses_current_successful_head_and_unicode(self) -> None:
        result = self.gateway().video_search("海边日落")
        self.assertEqual(result["video_schema_mode"], "analysis_heads_view")
        self.assertEqual([item["segment_id"] for item in result["results"]], ["seg_current"])
        match = result["results"][0]
        self.assertEqual(match["asset_source_id"], "src_video")
        self.assertEqual(match["asset_sha256"], VIDEO_SHA)
        self.assertEqual(match["analysis_run_id"], "arun_current")
        self.assertEqual(match["analysis_revision"], 1)
        self.assertNotIn("absolute_path", match)
        self.assertNotIn("database_path", result)

    def test_mixed_search_returns_one_ranked_photo_video_result_set(self) -> None:
        result = self.gateway().mixed_search("海边日落", limit=6)
        self.assertEqual(result["object"], "memolens.mixed_search")
        self.assertEqual(result["ranking"], "reciprocal_rank_fusion")
        self.assertEqual(result["branch_errors"], [])
        self.assertEqual(
            {item["result_type"] for item in result["results"]},
            {"image", "video_segment"},
        )
        self.assertEqual(result["results"][0]["media_kind"], "image")
        self.assertTrue(
            all(item["rank_score"] > 0 for item in result["results"])
        )

    def test_failed_higher_analysis_revision_is_never_searchable(self) -> None:
        result = self.gateway().video_search("幽灵")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["scanned_count"], 1)

    def test_final_head_join_is_safe_when_view_is_absent(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DROP VIEW current_video_segments")
            connection.commit()
        result = self.gateway().video_search("日落")
        self.assertEqual(result["video_schema_mode"], "analysis_heads_join")
        self.assertEqual(result["results"][0]["analysis_run_id"], "arun_current")

    def test_missing_current_selector_reports_video_index_unavailable(self) -> None:
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DROP VIEW current_video_segments")
            connection.execute("DROP TABLE asset_analysis_heads")
            connection.commit()
        with self.assertRaises(MemoLensError) as raised:
            self.gateway().video_search("日落")
        self.assertEqual(raised.exception.code, "video_index_unavailable")

    def test_legacy_explicit_head_compatibility_does_not_use_max_revision(self) -> None:
        legacy = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(
                """
                CREATE TABLE assets (
                    id TEXT PRIMARY KEY, kind TEXT, sha256 TEXT, status TEXT,
                    current_analysis_revision INTEGER, duration_ms INTEGER
                );
                CREATE TABLE asset_sources (
                    id TEXT PRIMARY KEY, asset_id TEXT, relative_path TEXT,
                    filename TEXT, status TEXT
                );
                CREATE TABLE video_segments (
                    id TEXT PRIMARY KEY, asset_id TEXT, analysis_revision INTEGER,
                    start_ms INTEGER, end_ms INTEGER, combined_text TEXT, summary TEXT
                );
                INSERT INTO assets VALUES ('v1','video','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','ready',1,9000);
                INSERT INTO asset_sources VALUES ('s1','v1','v.mp4','v.mp4','available');
                INSERT INTO video_segments VALUES ('good','v1',1,0,1000,'current term','current');
                INSERT INTO video_segments VALUES ('bad','v1',9,0,1000,'failed max term','failed max');
                """
            )
            connection.commit()
        gateway = self.gateway(legacy)
        self.assertEqual(gateway.video_search("failed max")["results"], [])
        current = gateway.video_search("current")["results"][0]
        self.assertEqual(current["analysis_revision"], 1)
        self.assertIsNone(current["analysis_run_id"])

    def test_media_list_is_mixed_read_only_and_path_minimal(self) -> None:
        result = self.gateway().media_list(limit=10)
        self.assertEqual({item["kind"] for item in result["assets"]}, {"image", "video", "audio"})
        self.assertEqual(
            {item["asset_source_id"] for item in result["assets"]},
            {"src_image", "src_video", "src_audio"},
        )
        self.assertTrue(all("absolute_path" not in item for item in result["assets"]))

    def test_media_get_returns_only_current_video_segments(self) -> None:
        detail = self.gateway().media_get("asset_video")
        self.assertEqual(detail["video_index_status"], "analysis_heads_view")
        self.assertEqual([item["id"] for item in detail["segments"]], ["seg_current"])
        self.assertEqual(detail["segments"][0]["analysis_run_id"], "arun_current")

    def test_timeline_draft_is_deterministic_and_has_source_provenance(self) -> None:
        first = _make_timeline("2026-08-12T12:00:00Z")
        second = _make_timeline("2026-08-13T12:00:00Z")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["tracks"][0]["clips"][0]["id"], second["tracks"][0]["clips"][0]["id"])
        self.assertEqual(first["transitions"], [])
        self.assertEqual(first["tracks"][0]["role"], "primary")
        self.assertEqual(
            first["provenance"]["source_analysis_runs"],
            {"seg_current": "arun_current"},
        )
        self.assertEqual(
            first["provenance"]["source_assets"]["asset_video"],
            {"sha256": VIDEO_SHA, "asset_source_id": "src_video"},
        )
        self.assertTrue(validate_timeline(first)["valid"])

    def test_timeline_draft_requires_asset_source_and_analysis_provenance(self) -> None:
        missing_source = _draft_items()
        missing_source[0].pop("asset_source_id")
        with self.assertRaises(TimelineInputError):
            draft_timeline(
                project_id="proj_demo",
                items=missing_source,
                created_at="2026-08-12T12:00:00Z",
            )
        missing_run = _draft_items()
        missing_run[0].pop("analysis_run_id")
        with self.assertRaises(TimelineInputError):
            draft_timeline(
                project_id="proj_demo",
                items=missing_run,
                created_at="2026-08-12T12:00:00Z",
            )

    def test_validator_rejects_per_clip_transition_and_speed_change(self) -> None:
        timeline = _make_timeline()
        timeline["tracks"][0]["clips"][0]["transition_in"] = {
            "type": "crossfade",
            "duration_ms": 100,
        }
        validation = validate_timeline(timeline)
        self.assertFalse(validation["valid"])
        self.assertIn("unknown_field", {item["code"] for item in validation["errors"]})

        timeline = _make_timeline()
        timeline["tracks"][0]["clips"][0]["timeline_duration_ms"] = 2999
        validation = validate_timeline(timeline)
        self.assertIn("speed_not_supported", {item["code"] for item in validation["errors"]})

    def test_transition_operations_and_persisted_transitions_fail_closed(self) -> None:
        items = _draft_items() + [
            {
                **_draft_items()[0],
                "segment_id": "seg_second",
                "match_id": "seg_second",
                "source_in_ms": 4000,
                "source_out_ms": 7000,
            }
        ]
        timeline = draft_timeline(
            project_id="proj_demo",
            items=items,
            created_at="2026-08-12T12:00:00Z",
        )
        first_clip, second_clip = timeline["tracks"][0]["clips"]
        transition = {
            "id": "tr_crossfade",
            "type": "crossfade",
            "from_clip_id": first_clip["id"],
            "to_clip_id": second_clip["id"],
            "duration_ms": 500,
        }
        with self.assertRaisesRegex(TimelineInputError, "hard cuts only"):
            revise_timeline_draft(
                timeline=timeline,
                operations=[{"op": "set_transitions", "transitions": [transition]}],
                created_at="2026-08-12T13:00:00Z",
            )
        persisted = deepcopy(timeline)
        persisted["transitions"] = [transition]
        validation = validate_timeline(persisted)
        self.assertFalse(validation["valid"])
        self.assertIn(
            ("unsupported_render_transition", "transitions[0]"),
            {(item["code"], item["field"]) for item in validation["errors"]},
        )

    def test_relink_source_is_explicit_and_does_not_mutate_input(self) -> None:
        timeline = _make_timeline()
        original = deepcopy(timeline)
        revised = revise_timeline_draft(
            timeline=timeline,
            operations=[
                {
                    "op": "relink_source",
                    "asset_id": "asset_video",
                    "asset_source_id": "src_relinked",
                    "asset_sha256": "d" * 64,
                }
            ],
            created_at="2026-08-12T13:00:00Z",
        )
        self.assertEqual(timeline, original)
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["provenance"]["parent_revision"], 1)
        self.assertEqual(
            revised["tracks"][0]["clips"][0]["asset_source_id"], "src_relinked"
        )
        self.assertEqual(
            revised["provenance"]["source_assets"]["asset_video"]["sha256"],
            "d" * 64,
        )

    def test_trim_operation_preserves_integer_no_speed_contract(self) -> None:
        timeline = _make_timeline()
        clip_id = timeline["tracks"][0]["clips"][0]["id"]
        revised = revise_timeline_draft(
            timeline=timeline,
            operations=[
                {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1000,
                    "source_out_ms": 3000,
                },
                {"op": "set_format", "duration_ms": 2000},
            ],
            created_at="2026-08-12T13:00:00Z",
        )
        clip = revised["tracks"][0]["clips"][0]
        self.assertEqual(clip["timeline_duration_ms"], 2000)
        self.assertTrue(validate_timeline(revised)["valid"])

    def test_persisted_immutable_timeline_list_and_get_support_final_table(self) -> None:
        gateway = self.gateway()
        listed = gateway.timeline_list(project_id="proj_demo")
        self.assertEqual(listed["result_count"], 1)
        timeline_id = listed["timelines"][0]["timeline_id"]
        detail = gateway.timeline_get(timeline_id, revision=1)
        self.assertEqual(detail["timeline"]["id"], timeline_id)
        self.assertTrue(detail["validation"]["valid"])

    def test_safe_default_denies_network_and_never_writes_sqlite(self) -> None:
        before = self.db.read_bytes()
        gateway = self.gateway()
        with mock.patch(
            "memolens_core.socket.getaddrinfo",
            side_effect=AssertionError("network resolution attempted"),
        ), mock.patch(
            "memolens_core.socket.socket",
            side_effect=AssertionError("socket attempted"),
        ):
            gateway.status()
            gateway.video_search("日落")
            gateway.media_list()
            gateway.timeline_list()
        self.assertEqual(self.db.read_bytes(), before)

    def test_api_opt_in_still_never_grants_write_render_or_export(self) -> None:
        gateway = self.gateway(trust=True)
        with mock.patch.object(
            gateway,
            "health",
            side_effect=MemoLensError("offline", code="service_unavailable"),
        ):
            status = gateway.status()
        for capability in (
            "create_timeline",
            "save_timeline",
            "render_preview",
            "export_video",
        ):
            self.assertFalse(status["capabilities"][capability])

    def test_mcp_exposes_only_read_or_in_memory_tools_with_accurate_annotations(self) -> None:
        self.assertEqual(SERVER_INFO["version"], "0.3.0")
        names = {tool["name"] for tool in TOOLS}
        self.assertEqual(len(names), 13)
        self.assertTrue(
            {
                "memolens_mixed_search",
                "memolens_video_search",
                "memolens_media_list",
                "memolens_media_get",
                "memolens_timeline_draft",
                "memolens_timeline_revise_draft",
                "memolens_timeline_validate",
                "memolens_timeline_list",
                "memolens_timeline_get",
            }.issubset(names)
        )
        self.assertFalse(any("save" in name or "render" in name or "export" in name for name in names))
        self.assertTrue(all(tool["annotations"]["readOnlyHint"] for tool in TOOLS))
        self.assertTrue(all(not tool["annotations"]["openWorldHint"] for tool in TOOLS))

    def test_cli_video_search_is_clean_json_from_non_repo_cwd(self) -> None:
        env = os.environ.copy()
        env[TRUST_LOCAL_API_ENV] = "0"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "memolens_cli.py"),
                "--db",
                str(self.db),
                "video-search",
                "海边日落",
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["results"][0]["segment_id"], "seg_current")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
