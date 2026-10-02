from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.src import DESKTOP_TOKEN_HEADER, create_app
from core.config import Settings


def semantic(goal: str = "把最近的生活素材剪成一条温暖短片") -> dict[str, object]:
    return {
        "intent": {
            "goal": goal,
            "stance": None,
            "audience": "朋友",
            "platform": "小红书",
        },
        "script": {
            "blocks": [
                {
                    "block_id": "opening",
                    "text": "这是最近被我随手拍下、但一直没有整理的片段。",
                }
            ]
        },
        "direction": {
            "theme": "日常记忆",
            "narrative_arc": "从零散到完整",
            "emotion": "温暖",
            "tone": "自然",
            "pace": "舒缓",
        },
        "output": {"duration_target_ms": 30_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def shutdown_app(app) -> None:
    for name in ("media_job_runner", "render_job_runner", "canonical_export_job_runner"):
        runner = app.extensions.get(name)
        if runner is not None:
            runner.shutdown()


class BlueprintApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-blueprint-api-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "index.db"
        self.token = "blueprint-api-desktop-token"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(Path(__file__).resolve().parents[1] / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.token,
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()
        self.repository = self.app.extensions["media_repository"]
        self.project = self.repository.create_project(
            "Blueprint API",
            {"goal": "legacy", "candidate_refs": []},
            {"created_by": "fixture"},
        )
        self.project_id = str(self.project["id"])
        brief = self.repository.get_brief(self.project_id, 1)
        assert brief is not None
        self.legacy_ref = {
            "revision": 1,
            "content_sha256": str(brief["content_sha256"]),
        }

    def tearDown(self) -> None:
        shutdown_app(self.app)
        self.environment.stop()
        self.temporary.cleanup()

    @property
    def query(self) -> dict[str, str]:
        return {"db_path": str(self.db_path)}

    def headers(self, key: str) -> dict[str, str]:
        return {
            DESKTOP_TOKEN_HEADER: self.token,
            "Idempotency-Key": key,
        }

    def initial_payload(self, *, proposal: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "expected_head": None,
            "initial_legacy_brief": self.legacy_ref,
            "source_candidate_sha256": "a" * 64,
            "semantic": proposal or semantic(),
        }

    def commit(self, payload: dict[str, object], key: str):
        return self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/commit",
            query_string=self.query,
            json=payload,
            headers=self.headers(key),
        )

    def test_commit_requires_desktop_auth_and_exact_database_binding(self) -> None:
        path = f"/v1/creative/projects/{self.project_id}/blueprint/commit"
        without_token = self.client.post(
            path,
            query_string=self.query,
            json=self.initial_payload(),
            headers={"Idempotency-Key": "auth-1"},
        )
        self.assertEqual(without_token.status_code, 401)

        without_binding = self.client.post(
            path,
            json=self.initial_payload(),
            headers=self.headers("auth-2"),
        )
        self.assertEqual(without_binding.status_code, 409)
        self.assertEqual(without_binding.json["code"], "database_binding_required")

        wrong_binding = self.client.post(
            path,
            query_string={"db_path": str(self.root / "other.db")},
            json=self.initial_payload(),
            headers=self.headers("auth-3"),
        )
        self.assertEqual(wrong_binding.status_code, 409)
        self.assertEqual(wrong_binding.json["code"], "database_binding_mismatch")

    def test_strict_json_and_authority_fields_fail_before_write(self) -> None:
        path = f"/v1/creative/projects/{self.project_id}/blueprint/commit"
        raw = json.dumps(self.initial_payload(), ensure_ascii=False)
        duplicate = raw[:-1] + ',"semantic":{}}'
        response = self.client.post(
            path,
            query_string=self.query,
            data=duplicate.encode(),
            content_type="application/json",
            headers=self.headers("strict-1"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "duplicate_json_key")

        injected = {**self.initial_payload(), "user_confirmed": True}
        rejected = self.commit(injected, "strict-2")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json["code"], "invalid_blueprint_command")
        with self.repository._connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_blueprint_revisions").fetchone()[0],
                0,
            )

    def test_reference_pins_are_proven_or_rejected_instead_of_looking_verified(self) -> None:
        unsupported_semantic = semantic()
        unsupported_semantic["technique_refs"] = [
            {"card_id": "card_missing", "revision": 1, "declared_state": "selected"}
        ]
        unsupported_semantic["bindings"] = {
            "creator_context": None,
            "wiki_generation": {
                "generation_id": "wiki_missing",
                "content_sha256": "b" * 64,
            },
        }
        response = self.commit(
            self.initial_payload(proposal=unsupported_semantic),
            "unsupported-pins",
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json["code"], "blueprint_reference_not_resolvable")
        paths = {
            item["path"] for item in response.json["details"]["unsupported"]
        }
        self.assertEqual(
            paths,
            {"/technique_refs/0", "/bindings/wiki_generation"},
        )
        with self.repository._connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_blueprint_revisions").fetchone()[0],
                0,
            )

        non_default, replayed = self.repository.put_creator_profile(
            profile_id="creator_proven",
            base_revision=0,
            profile={"platform": "short-video", "tone": "natural"},
            evidence=[],
            source="user_edit",
            idempotency_scope="test:creator-profile",
            idempotency_key="creator-proof",
            request_sha256="c" * 64,
        )
        self.assertFalse(replayed)
        non_default_semantic = semantic()
        non_default_semantic["bindings"] = {
            "creator_context": {
                "profile_id": non_default["profile_id"],
                "revision": non_default["revision"],
                "content_sha256": non_default["content_sha256"],
            },
            "wiki_generation": None,
        }
        rejected_non_default = self.commit(
            self.initial_payload(proposal=non_default_semantic),
            "non-default-creator-pin",
        )
        self.assertEqual(rejected_non_default.status_code, 422)
        self.assertEqual(
            rejected_non_default.json["code"],
            "blueprint_reference_unresolved",
        )

        creator, replayed = self.repository.put_creator_profile(
            profile_id="default",
            base_revision=0,
            profile={"platform": "short-video", "tone": "natural"},
            evidence=[],
            source="user_edit",
            idempotency_scope="test:creator-profile",
            idempotency_key="default-creator-proof",
            request_sha256="d" * 64,
        )
        self.assertFalse(replayed)
        supported_semantic = semantic()
        supported_semantic["reference_refs"] = [
            {
                "reference_id": "current_project",
                "kind": "project",
                "locator": self.project_id,
                "note": None,
            }
        ]
        supported_semantic["bindings"] = {
            "creator_context": {
                "profile_id": creator["profile_id"],
                "revision": creator["revision"],
                "content_sha256": creator["content_sha256"],
            },
            "wiki_generation": None,
        }
        accepted = self.commit(
            self.initial_payload(proposal=supported_semantic),
            "supported-pins",
        )
        self.assertEqual(accepted.status_code, 201, accepted.json)

    def test_commit_replay_no_change_revision_and_history_are_consistent(self) -> None:
        first = self.commit(self.initial_payload(), "commit-1")
        self.assertEqual(first.status_code, 201, first.json)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertFalse(first.json["authority"]["verified"])
        self.assertEqual(first.json["authority"]["state"], "unverified")
        head = first.json["result"]["result_head"]

        replay = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/commit",
            query_string=self.query,
            json=self.initial_payload(),
            headers={
                **self.headers("commit-1"),
                "Origin": "http://127.0.0.1:5173",
            },
        )
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertIn(
            "idempotency-replayed",
            replay.headers["Access-Control-Expose-Headers"].casefold(),
        )
        self.assertEqual(replay.json, first.json)
        project_updated_at = self.repository.get_project(self.project_id)["updated_at"]

        no_change_payload = {
            "expected_head": {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "b" * 64,
            "semantic": semantic(),
        }
        no_change = self.commit(no_change_payload, "commit-2")
        self.assertEqual(no_change.status_code, 200, no_change.json)
        self.assertEqual(no_change.json["result"]["kind"], "no_change")
        self.assertEqual(no_change.json["result"]["result_head"], head)
        self.assertEqual(
            self.repository.get_project(self.project_id)["updated_at"],
            project_updated_at,
        )

        current = self.client.get(
            f"/v1/creative/projects/{self.project_id}/blueprint"
        )
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json["revision"], 1)
        self.assertEqual(current.json["blueprint"]["status"], "proposal")
        self.assertEqual(current.json["blueprint"]["authority"]["state"], "unverified")

        history = self.client.get(
            f"/v1/creative/projects/{self.project_id}/blueprint/operations",
            query_string={"limit": 10},
        )
        self.assertEqual(history.status_code, 200)
        self.assertFalse(history.json["ledger"]["complete_project_history"])
        self.assertEqual(
            [item["result_kind"] for item in history.json["operations"]],
            ["no_change", "revision_created"],
        )
        self.assertNotIn("semantic", json.dumps(history.json))

        workspace = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string={**self.query, "history_limit": "1"},
        )
        self.assertEqual(workspace.status_code, 200, workspace.json)
        blueprint_lane = workspace.json["project"]["history"]["blueprint"]
        self.assertEqual(blueprint_lane["available_revision_count"], 1)
        self.assertEqual(blueprint_lane["available_operation_count"], 2)
        self.assertTrue(blueprint_lane["operations_truncated"])
        self.assertEqual(blueprint_lane["operations"][0]["sequence"], 2)
        self.assertEqual(blueprint_lane["operations"][0]["result_kind"], "no_change")
        self.assertEqual(blueprint_lane["operations"][0]["result_revision"], 1)
        self.assertEqual(
            blueprint_lane["operations"][0]["result_content_sha256"],
            head["content_sha256"],
        )

    def test_receipt_conflict_wins_before_stale_head_and_restore_creates_revision(self) -> None:
        first = self.commit(self.initial_payload(), "permanent-key")
        self.assertEqual(first.status_code, 201)
        head_one = first.json["result"]["result_head"]
        changed_payload = {
            "expected_head": {
                "revision": head_one["revision"],
                "content_sha256": head_one["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "c" * 64,
            "semantic": semantic("第二版主题"),
        }
        second = self.commit(changed_payload, "commit-second")
        self.assertEqual(second.status_code, 201, second.json)
        head_two = second.json["result"]["result_head"]

        reused = self.commit(
            {**self.initial_payload(), "semantic": semantic("不同请求")},
            "permanent-key",
        )
        self.assertEqual(reused.status_code, 409)
        self.assertEqual(reused.json["code"], "blueprint_idempotency_conflict")

        restored = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/restore",
            query_string={
                **self.query,
                "expected_database_uuid": self.repository.database_uuid,
            },
            json={
                "expected_head": {
                    "revision": head_two["revision"],
                    "content_sha256": head_two["content_sha256"],
                },
                "restore_from": {
                    "revision": head_one["revision"],
                    "content_sha256": head_one["content_sha256"],
                },
            },
            headers=self.headers("restore-1"),
        )
        self.assertEqual(restored.status_code, 201, restored.json)
        self.assertEqual(restored.json["result"]["kind"], "revision_restored")
        self.assertEqual(restored.json["result"]["result_head"]["revision"], 3)
        head_three = restored.json["result"]["result_head"]
        repeated_restore = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/restore",
            query_string={
                **self.query,
                "expected_database_uuid": self.repository.database_uuid,
            },
            json={
                "expected_head": {
                    "revision": head_three["revision"],
                    "content_sha256": head_three["content_sha256"],
                },
                "restore_from": {
                    "revision": head_one["revision"],
                    "content_sha256": head_one["content_sha256"],
                },
            },
            headers=self.headers("restore-same-semantic"),
        )
        self.assertEqual(repeated_restore.status_code, 201, repeated_restore.json)
        self.assertEqual(repeated_restore.json["result"]["kind"], "revision_restored")
        self.assertEqual(repeated_restore.json["result"]["changed_sections"], [])
        self.assertEqual(repeated_restore.json["result"]["result_head"]["revision"], 4)
        old = self.client.get(
            f"/v1/creative/projects/{self.project_id}/blueprint",
            query_string={"revision": 1},
        )
        self.assertEqual(old.status_code, 200)
        self.assertFalse(old.json["current"])

    def test_blueprint_head_blocks_legacy_timeline_creation(self) -> None:
        first = self.commit(self.initial_payload(), "timeline-block-commit")
        self.assertEqual(first.status_code, 201)
        response = self.client.post(
            f"/v1/creative/projects/{self.project_id}/timelines",
            query_string=self.query,
            json={"brief_revision": 1},
            headers=self.headers("timeline-after-blueprint"),
        )
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(response.json["code"], "blueprint_timeline_compiler_unavailable")
        legacy_workbench = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string=self.query,
        )
        self.assertEqual(legacy_workbench.status_code, 200, legacy_workbench.json)
        workspace = legacy_workbench.json["project"]
        self.assertEqual(workspace["database_uuid"], self.repository.database_uuid)
        self.assertEqual(
            workspace["current_blueprint"]["database_uuid"],
            self.repository.database_uuid,
        )
        self.assertEqual(
            workspace["canonical_source"],
            "creative_blueprint",
        )
        self.assertEqual(
            workspace["history"]["legacy_artifacts"]["role"],
            "migration_context_only",
        )
        self.assertEqual(workspace["current_blueprint"]["revision"], 1)
        self.assertEqual(
            workspace["current_blueprint"]["content_sha256"],
            first.json["result"]["result_head"]["content_sha256"],
        )
        self.assertEqual(
            workspace["current_blueprint"]["semantic_sha256"],
            first.json["result"]["result_head"]["semantic_sha256"],
        )
        self.assertFalse(workspace["capabilities"]["complete_project_history"])
        self.assertFalse(workspace["capabilities"]["authoritative_timeline_head"])
        self.assertTrue(workspace["capabilities"]["coverage_plan_materialization"])
        self.assertTrue(workspace["capabilities"]["blueprint_timeline_compiler"])
        self.assertFalse(workspace["capabilities"]["render"])
        self.assertFalse(workspace["capabilities"]["export"])
        self.assertEqual(workspace["executable"]["state"], "not_compiled")
        self.assertIsNone(workspace["executable"]["current_timeline"])
        self.assertEqual(
            workspace["executable"]["reason_code"],
            "coverage_plan_missing",
        )
        self.assertEqual(
            workspace["coverage"],
            {
                "state": "missing",
                "head": None,
                "blueprint_binding": None,
                "beat_count": 0,
                "selected_assignment_count": 0,
                "gap_count": 0,
            },
        )
        self.assertFalse(workspace["history"]["complete_project_history"])
        self.assertFalse(workspace["history"]["global_total_order"])
        self.assertEqual(workspace["history"]["replay_scope"], "per_lane_only")
        self.assertEqual(
            workspace["history"]["blueprint"]["ledger"]["coverage_scope"],
            "creative_blueprint",
        )
        self.assertEqual(
            len(workspace["history"]["blueprint"]["operations"]),
            1,
        )
        self.assertEqual(
            workspace["history"]["decision_authority"]["projection"]
            ["confirmed_decision_unit_count"],
            0,
        )
        self.assertEqual(
            workspace["history"]["decision_authority"]["events"],
            [],
        )
        self.assertEqual(
            len(workspace["history"]["legacy_artifacts"]["briefs"]),
            1,
        )
        self.assertEqual(
            workspace["history"]["legacy_artifacts"]["timelines"],
            [],
        )
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM creative_briefs WHERE project_id=?",
                (self.project_id,),
            )
        canonical = self.client.get(
            f"/v1/creative/projects/{self.project_id}/blueprint"
        )
        self.assertEqual(canonical.status_code, 200, canonical.json)
        self.assertEqual(canonical.json["database_uuid"], self.repository.database_uuid)
        without_legacy_brief = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string=self.query,
        )
        self.assertEqual(without_legacy_brief.status_code, 200, without_legacy_brief.json)
        self.assertEqual(
            without_legacy_brief.json["project"]["canonical_source"],
            "creative_blueprint",
        )
        self.assertEqual(
            without_legacy_brief.json["project"]["history"]["legacy_artifacts"]["briefs"],
            [],
        )

    def test_canonical_project_workspace_is_a_read_only_atomic_projection(self) -> None:
        first = self.commit(self.initial_payload(), "workspace-read-1")
        self.assertEqual(first.status_code, 201, first.json)
        next_semantic = semantic("把被遗忘的片段剪成一条有立场的短片")
        next_semantic["intent"]["stance"] = "生活不应只留在硬盘里"
        second = self.commit(
            {
                "expected_head": {
                    "revision": 1,
                    "content_sha256": first.json["result"]["result_head"]["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": "b" * 64,
                "semantic": next_semantic,
            },
            "workspace-read-2",
        )
        self.assertEqual(second.status_code, 201, second.json)
        before = {}
        with self.repository.transaction() as connection:
            for table in (
                "creative_blueprint_operations",
                "creative_blueprint_revisions",
                "blueprint_decision_authority_events",
            ):
                before[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0]

        response = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string={**self.query, "history_limit": "1"},
        )
        self.assertEqual(response.status_code, 200, response.json)
        workspace = response.json["project"]
        self.assertEqual(workspace["current_blueprint"]["revision"], 2)
        self.assertEqual(
            workspace["current_blueprint"]["blueprint"]["semantic"]["intent"]["stance"],
            "生活不应只留在硬盘里",
        )
        self.assertEqual(len(workspace["history"]["blueprint"]["operations"]), 1)
        self.assertEqual(
            workspace["history"]["blueprint"]["operations"][0]["result_revision"],
            2,
        )
        self.assertEqual(
            workspace["history"]["blueprint"]["available_revision_count"],
            2,
        )
        with self.repository.transaction() as connection:
            after = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (self.project_id,),
                ).fetchone()[0]
                for table in before
            }
        self.assertEqual(after, before)

    def test_restore_is_bound_to_the_server_database_identity(self) -> None:
        committed = self.commit(self.initial_payload(), "workspace-database-identity")
        self.assertEqual(committed.status_code, 201, committed.json)
        head = committed.json["result"]["result_head"]
        wrong_uuid = "00000000-0000-4000-8000-000000000000"
        self.assertNotEqual(wrong_uuid, self.repository.database_uuid)
        missing_identity = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/restore",
            query_string=self.query,
            json={
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "restore_from": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
            },
            headers=self.headers("workspace-missing-database-restore"),
        )
        self.assertEqual(missing_identity.status_code, 400, missing_identity.json)
        self.assertEqual(missing_identity.json["code"], "database_identity_required")
        response = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/restore",
            query_string={
                **self.query,
                "expected_database_uuid": wrong_uuid,
            },
            json={
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "restore_from": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
            },
            headers=self.headers("workspace-wrong-database-restore"),
        )
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(response.json["code"], "database_identity_changed")
        current = self.client.get(
            f"/v1/creative/projects/{self.project_id}/blueprint",
            query_string=self.query,
        )
        self.assertEqual(current.status_code, 200, current.json)
        self.assertEqual(current.json["revision"], head["revision"])

    def test_legacy_timeline_lane_is_metadata_only_and_degrades_honestly_on_tamper(self) -> None:
        timeline_id = "timeline_workspace_history"
        saved = self.repository.save_timeline(
            timeline_id=timeline_id,
            project_id=self.project_id,
            revision=1,
            timeline={
                "schema_version": "1.0",
                "id": timeline_id,
                "project_id": self.project_id,
                "revision": 1,
                "format": {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30,
                    "sample_rate": 48_000,
                    "duration_ms": 1_000,
                    "background_color": "#000000",
                },
                "tracks": [],
                "transitions": [],
                "provenance": {},
            },
            provenance={"brief_revision": 1, "created_by": "fixture"},
            validation_status="valid",
        )
        self.assertEqual(saved["id"], timeline_id)
        committed = self.commit(self.initial_payload(), "workspace-history-timeline")
        self.assertEqual(committed.status_code, 201, committed.json)

        response = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string=self.query,
        )
        self.assertEqual(response.status_code, 200, response.json)
        lane = response.json["project"]["history"]["legacy_artifacts"]
        self.assertFalse(lane["authoritative_timeline_head"])
        self.assertEqual(lane["timeline_revision_count"], 1)
        self.assertEqual(lane["timelines"][0]["timeline_id"], timeline_id)
        self.assertEqual(
            lane["timelines"][0]["role"],
            "historical_observed_context_only",
        )
        self.assertNotIn("timeline", lane["timelines"][0])

        with self.repository._connect() as connection:
            connection.execute(
                "UPDATE timelines SET timeline_json='{}' WHERE id=? AND revision=1",
                (timeline_id,),
            )
        tampered = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string=self.query,
        )
        self.assertEqual(tampered.status_code, 200, tampered.json)
        tampered_workspace = tampered.json["project"]
        self.assertEqual(tampered_workspace["current_blueprint"]["revision"], 1)
        unavailable = tampered_workspace["history"]["legacy_artifacts"]
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(
            unavailable["reason_code"],
            "legacy_artifact_history_integrity_error",
        )
        self.assertEqual(unavailable["timelines"], [])
        self.assertEqual(unavailable["timeline_revision_count"], 1)

    def test_legacy_history_size_preflight_rejects_large_json_before_decode(self) -> None:
        timeline_id = "timeline_workspace_oversize"
        self.repository.save_timeline(
            timeline_id=timeline_id,
            project_id=self.project_id,
            revision=1,
            timeline={
                "schema_version": "1.0",
                "id": timeline_id,
                "project_id": self.project_id,
                "revision": 1,
                "format": {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30,
                    "sample_rate": 48_000,
                    "duration_ms": 1_000,
                    "background_color": "#000000",
                },
                "tracks": [],
                "transitions": [],
                "provenance": {},
            },
            provenance={"brief_revision": 1, "created_by": "fixture"},
            validation_status="valid",
        )
        committed = self.commit(self.initial_payload(), "workspace-oversize-blueprint")
        self.assertEqual(committed.status_code, 201, committed.json)
        oversized = json.dumps(
            {"payload": "x" * (1_048_576 + 1)},
            separators=(",", ":"),
        )
        with self.repository._connect() as connection:
            connection.execute(
                "UPDATE timelines SET timeline_json=? WHERE id=? AND revision=1",
                (oversized, timeline_id),
            )

        original_loads = json.loads

        def bounded_loads(raw):
            if isinstance(raw, str):
                self.assertLessEqual(len(raw.encode("utf-8")), 1_048_576)
            return original_loads(raw)

        with patch("backend.src.media.blueprint.json.loads", side_effect=bounded_loads):
            response = self.client.get(
                f"/v1/creative/projects/{self.project_id}",
                query_string=self.query,
            )
        self.assertEqual(response.status_code, 200, response.json)
        workspace = response.json["project"]
        self.assertEqual(workspace["current_blueprint"]["revision"], 1)
        self.assertEqual(
            workspace["history"]["legacy_artifacts"]["status"],
            "unavailable",
        )

    def test_workspace_authority_lane_is_exact_and_redacts_native_command_material(self) -> None:
        committed = self.commit(self.initial_payload(), "workspace-authority-blueprint")
        self.assertEqual(committed.status_code, 201, committed.json)
        presentation = self.repository.build_blueprint_authority_presentation(
            self.project_id,
            authority_operation="confirm",
            decision_units=["script"],
            runtime_authority_epoch="workspace_authority_epoch",
        )
        self.repository.confirm_blueprint_decision_units(
            self.project_id,
            runtime_authority_epoch="workspace_authority_epoch",
            presentation=presentation["presentation"],
            presentation_sha256=str(presentation["presentation_sha256"]),
            native_gesture_nonce="workspace_authority_native_gesture",
            idempotency_key="workspace-authority-confirm",
        )

        response = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string=self.query,
        )
        self.assertEqual(response.status_code, 200, response.json)
        lane = response.json["project"]["history"]["decision_authority"]
        self.assertEqual(lane["projection"]["confirmed_decision_unit_count"], 1)
        self.assertTrue(lane["projection"]["decision_units"]["script"]["confirmed"])
        self.assertEqual(lane["available_event_count"], 1)
        self.assertFalse(lane["events_truncated"])
        self.assertEqual(len(lane["events"]), 1)
        event = lane["events"][0]
        self.assertEqual(event["authority_operation"], "confirm")
        self.assertEqual(event["decision_units"], ["script"])
        self.assertEqual(
            set(event),
            {
                "event_id",
                "sequence",
                "authority_operation",
                "observed_revision",
                "decision_units",
                "created_at",
            },
        )
        serialized = json.dumps(response.json, sort_keys=True)
        self.assertNotIn("workspace_authority_native_gesture", serialized)
        self.assertNotIn("workspace_authority_epoch", serialized)
        self.assertNotIn("presentation_sha256", json.dumps(event, sort_keys=True))

    def test_workspace_history_limit_is_bounded_and_does_not_accept_order_expressions(self) -> None:
        timeline_id = "timeline_workspace_bounds"
        self.repository.save_timeline(
            timeline_id=timeline_id,
            project_id=self.project_id,
            revision=1,
            timeline={
                "schema_version": "1.0",
                "id": timeline_id,
                "project_id": self.project_id,
                "revision": 1,
                "format": {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30,
                    "sample_rate": 48_000,
                    "duration_ms": 1_000,
                    "background_color": "#000000",
                },
                "tracks": [],
                "transitions": [],
                "provenance": {},
            },
            provenance={"brief_revision": 1, "created_by": "fixture"},
            validation_status="valid",
        )
        committed = self.commit(self.initial_payload(), "workspace-bounds")
        self.assertEqual(committed.status_code, 201, committed.json)
        for index, operation in enumerate(("confirm", "revoke"), start=1):
            presentation = self.repository.build_blueprint_authority_presentation(
                self.project_id,
                authority_operation=operation,
                decision_units=["script"],
                runtime_authority_epoch="workspace_bounds_epoch",
            )
            writer = (
                self.repository.confirm_blueprint_decision_units
                if operation == "confirm"
                else self.repository.revoke_blueprint_decision_units
            )
            writer(
                self.project_id,
                runtime_authority_epoch="workspace_bounds_epoch",
                presentation=presentation["presentation"],
                presentation_sha256=str(presentation["presentation_sha256"]),
                native_gesture_nonce=f"workspace_bounds_gesture_{index}",
                idempotency_key=f"workspace-bounds-authority-{index}",
            )
        for invalid in ("0", "101", "1; DROP TABLE creative_projects", "1.5"):
            with self.subTest(history_limit=invalid):
                response = self.client.get(
                    f"/v1/creative/projects/{self.project_id}",
                    query_string={**self.query, "history_limit": invalid},
                )
                self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(response.json["code"], "invalid_limit")

        bounded = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string={**self.query, "history_limit": "1"},
        )
        self.assertEqual(bounded.status_code, 200, bounded.json)
        self.assertLessEqual(
            len(bounded.json["project"]["history"]["blueprint"]["operations"]),
            1,
        )
        authority_lane = bounded.json["project"]["history"]["decision_authority"]
        self.assertEqual(len(authority_lane["events"]), 1)
        self.assertEqual(authority_lane["available_event_count"], 2)
        self.assertTrue(authority_lane["events_truncated"])
        legacy_lane = bounded.json["project"]["history"]["legacy_artifacts"]
        self.assertLessEqual(
            len(legacy_lane["briefs"]) + len(legacy_lane["timelines"]),
            1,
        )
        self.assertTrue(
            legacy_lane["briefs_truncated"] or legacy_lane["timelines_truncated"]
        )

    def test_blueprint_history_requires_an_existing_project(self) -> None:
        for suffix in ("/blueprint", "/blueprint/operations"):
            with self.subTest(suffix=suffix):
                response = self.client.get(
                    f"/v1/creative/projects/proj_does_not_exist{suffix}",
                    query_string={"limit": 10},
                )
                self.assertEqual(response.status_code, 404, response.json)
                self.assertEqual(response.json["code"], "project_not_found")

    def test_orphaned_blueprint_state_never_reopens_legacy_timeline_creation(self) -> None:
        committed = self.commit(self.initial_payload(), "orphan-commit")
        self.assertEqual(committed.status_code, 201, committed.json)
        with self.repository._connect() as connection:
            connection.execute("DROP TRIGGER trg_blueprint_heads_no_delete")
            connection.execute(
                "DELETE FROM creative_blueprint_heads WHERE project_id=?",
                (self.project_id,),
            )
        response = self.client.post(
            f"/v1/creative/projects/{self.project_id}/timelines",
            query_string=self.query,
            json={"brief_revision": 1},
            headers=self.headers("timeline-after-orphan"),
        )
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(response.json["code"], "blueprint_integrity_error")
        read_response = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string=self.query,
        )
        self.assertEqual(read_response.status_code, 409, read_response.json)
        self.assertEqual(read_response.json["code"], "blueprint_integrity_error")

    def test_normalized_command_digest_ignores_json_representation(self) -> None:
        path = f"/v1/creative/projects/{self.project_id}/blueprint/commit"
        payload = self.initial_payload()
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        spaced = json.dumps(payload, ensure_ascii=False, indent=4).encode()
        first = self.client.post(
            path,
            query_string=self.query,
            data=compact,
            content_type="application/json",
            headers=self.headers("representation-key"),
        )
        replay = self.client.post(
            path,
            query_string=self.query,
            data=spaced,
            content_type="application/json",
            headers=self.headers("representation-key"),
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(
            hashlib.sha256(json.dumps(first.json, sort_keys=True).encode()).hexdigest(),
            hashlib.sha256(json.dumps(replay.json, sort_keys=True).encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
