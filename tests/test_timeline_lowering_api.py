from __future__ import annotations

import copy
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src import (
    DESKTOP_TOKEN_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline import TimelineService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
from tests.test_coverage_persistence import semantic


class TimelineLoweringApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-api-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self.legacy_timelines = TimelineService(self.repository)
        self.token = "timeline-api-desktop-token"
        self._project_sequence = 0

        app = Flask("timeline-lowering-api-test")
        app.config.update(TESTING=True, DESKTOP_SESSION_TOKEN=self.token)
        app.extensions.update(
            {
                "media_repository": self.repository,
                "blueprint_service": self.blueprints,
                "coverage_service": self.coverage,
                "timeline_service": self.legacy_timelines,
                "timeline_lowering_service": self.timelines,
            }
        )
        app.register_blueprint(api_blueprint)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        from tests.test_timeline_lowering_integration import (
            TimelineLoweringIntegrationTests,
        )

        return TimelineLoweringIntegrationTests._register_image(
            self,  # type: ignore[arg-type]
            name,
            content,
        )

    def _build_project(
        self,
        *,
        lowerable: bool = True,
        with_legacy_timeline: bool = False,
    ) -> tuple[str, dict[str, object], list[str]]:
        self._project_sequence = getattr(self, "_project_sequence", 0) + 1
        sequence = str(self._project_sequence).encode("ascii")
        first = self._register_image(
            f"opening-{self._project_sequence}.jpg",
            b"timeline-api-opening-" + sequence,
        )
        first_ref = f"memolens://evidence/asset/{first['id']}"
        proposed = semantic("timeline-api", evidence_ref=first_ref)
        source_ids = [
            str(self.repository.available_sources(str(first["id"]))[0]["asset_source_id"])
        ]
        if lowerable:
            second = self._register_image(
                f"ending-{self._project_sequence}.jpg",
                b"timeline-api-ending-" + sequence,
            )
            second_ref = f"memolens://evidence/asset/{second['id']}"
            proposed["material_hints"].append(
                {
                    "hint_id": "ending_image",
                    "evidence_ref": second_ref,
                    "script_block_ids": ["ending"],
                    "reason": "Use the exact ending image.",
                }
            )
            source_ids.append(
                str(
                    self.repository.available_sources(str(second["id"]))[0][
                        "asset_source_id"
                    ]
                )
            )
        project = self.repository.create_project(
            "Canonical Timeline API",
            {
                "goal": "legacy history",
                "duration_ms": 3_000,
                "aspect_ratio": "9:16",
                "candidate_refs": [
                    {
                        "result_type": "image",
                        "id": first["id"],
                        "asset_id": first["id"],
                    }
                ],
            },
            {"created_by": "timeline-api-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        if with_legacy_timeline:
            self.legacy_timelines.create_from_project(project_id)
        self.blueprints.commit_proposal(
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
            idempotency_key=f"blueprint-{project_id}",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key=f"coverage-{project_id}",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        payload = {
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
                key: coverage[key]
                for key in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": None,
        }
        return project_id, payload, source_ids

    def _post(
        self,
        project_id: str,
        payload: object,
        *,
        key: str = "timeline-api-command",
        token: bool = True,
        database_uuid: str | None = None,
    ):
        headers = {"Idempotency-Key": key}
        if token:
            headers[DESKTOP_TOKEN_HEADER] = self.token
        query = {"db_path": str(self.db_path)}
        if database_uuid is not None:
            query["expected_database_uuid"] = database_uuid
        return self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            query_string=query,
            json=payload,
            headers=headers,
        )

    def _post_reconcile(
        self,
        project_id: str,
        payload: object,
        *,
        key: str = "timeline-api-reconcile",
        token: bool = True,
        database_uuid: str | None = None,
    ):
        headers = {"Idempotency-Key": key}
        if token:
            headers[DESKTOP_TOKEN_HEADER] = self.token
        query = {"db_path": str(self.db_path)}
        if database_uuid is not None:
            query["expected_database_uuid"] = database_uuid
        return self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/reconcile",
            query_string=query,
            json=payload,
            headers=headers,
        )

    def _advance_coverage(self, project_id: str) -> dict[str, object]:
        blueprint = self.repository.get_blueprint_head(project_id)
        coverage = self.repository.get_coverage_head(project_id)
        assert blueprint is not None and coverage is not None
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    field: blueprint[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": {
                    "revision": coverage["revision"],
                    "content_sha256": coverage["content_sha256"],
                },
            },
            idempotency_key=f"coverage-reconcile-{project_id}",
            expected_database_uuid=self.repository.database_uuid,
        )
        advanced = self.repository.get_coverage_head(project_id)
        assert advanced is not None
        return advanced

    def _reconcile_payload(
        self,
        project_id: str,
        timeline_head: dict[str, object],
    ) -> dict[str, object]:
        blueprint = self.repository.get_blueprint_head(project_id)
        coverage = self.repository.get_coverage_head(project_id)
        assert blueprint is not None and coverage is not None
        return {
            "expected_blueprint": {
                field: blueprint[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "semantic_sha256",
                    "operation_id",
                )
            },
            "expected_coverage": {
                field: coverage[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": dict(timeline_head),
        }

    def _project_workspace(self, project_id: str) -> dict[str, object]:
        response = self.client.get(
            f"/v1/creative/projects/{project_id}",
            query_string={"db_path": str(self.db_path)},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["project"]

    def test_post_requires_desktop_exact_database_and_closed_json(self) -> None:
        project_id, payload, _source_ids = self._build_project()

        denied = self._post(
            project_id,
            payload,
            token=False,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["code"], "desktop_auth_required")

        missing_identity = self._post(project_id, payload, key="missing-identity")
        self.assertEqual(missing_identity.status_code, 400)
        self.assertEqual(
            missing_identity.get_json()["code"],
            "database_identity_required",
        )

        wrong_identity = self._post(
            project_id,
            payload,
            key="wrong-identity",
            database_uuid="00000000-0000-4000-8000-000000000000",
        )
        self.assertEqual(wrong_identity.status_code, 409)
        self.assertEqual(wrong_identity.get_json()["code"], "database_identity_changed")

        inflated = {**payload, "raw_path": str(self.library)}
        unsupported = self._post(
            project_id,
            inflated,
            key="inflated-command",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(unsupported.status_code, 400)
        self.assertEqual(unsupported.get_json()["code"], "invalid_timeline_command")

        scalar = self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            query_string={
                "db_path": str(self.db_path),
                "expected_database_uuid": self.repository.database_uuid,
            },
            data="[]",
            content_type="application/json",
            headers={
                DESKTOP_TOKEN_HEADER: self.token,
                "Idempotency-Key": "scalar-command",
            },
        )
        self.assertEqual(scalar.status_code, 400)
        self.assertEqual(scalar.get_json()["code"], "json_object_required")

    def test_missing_materialize_replay_and_current_workspace_are_exact(self) -> None:
        project_id, payload, _source_ids = self._build_project()

        missing = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline"
        )
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(missing.get_json()["freshness"]["state"], "missing")
        self.assertIsNone(missing.get_json()["head"])
        before = self._project_workspace(project_id)
        self.assertEqual(before["timeline"]["state"], "missing")
        self.assertEqual(before["executable"]["state"], "not_compiled")
        self.assertEqual(
            before["executable"]["reason_code"],
            "canonical_timeline_materialization_available",
        )
        self.assertTrue(before["capabilities"]["timeline_materialization"])
        self.assertTrue(before["capabilities"]["blueprint_timeline_compiler"])

        first = self._post(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        replay = self._post(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.get_json(), first.get_json())

        timeline = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            query_string={"revision": "1"},
        )
        self.assertEqual(timeline.status_code, 200)
        timeline_workspace = timeline.get_json()
        self.assertEqual(timeline_workspace["freshness"]["state"], "current")
        self.assertEqual(timeline_workspace["lifecycle"]["state"], "draft")
        self.assertEqual(
            timeline_workspace["lifecycle"]["approval"],
            "not_established",
        )
        self.assertFalse(timeline_workspace["lifecycle"]["exportable"])
        self.assertNotIn(str(self.library), timeline.get_data(as_text=True))

        current = self._project_workspace(project_id)
        self.assertEqual(current["timeline"]["state"], "current")
        self.assertEqual(current["timeline"]["clip_count"], 2)
        self.assertEqual(current["timeline"]["source_binding_count"], 2)
        self.assertEqual(current["executable"]["state"], "compiled_draft")
        self.assertEqual(
            current["executable"]["reason_code"],
            "canonical_timeline_current",
        )
        self.assertEqual(current["executable"]["current_timeline"], current["timeline"]["head"])
        self.assertTrue(current["capabilities"]["authoritative_timeline_head"])
        self.assertFalse(current["capabilities"]["timeline_materialization"])
        self.assertTrue(current["capabilities"]["timeline_mutation"])
        for capability in ("preview", "render", "export"):
            self.assertFalse(current["capabilities"][capability])

    def test_reconcile_route_is_desktop_only_exact_and_advances_stale_timeline(self) -> None:
        project_id, payload, _source_ids = self._build_project()
        created = self._post(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(created.status_code, 201)
        old_head = created.get_json()["result"]["result_head"]
        self._advance_coverage(project_id)
        stale = self._project_workspace(project_id)
        self.assertEqual(stale["timeline"]["state"], "stale_coverage")
        self.assertTrue(stale["capabilities"]["timeline_reconciliation"])
        self.assertFalse(stale["capabilities"]["timeline_mutation"])
        reconcile_payload = self._reconcile_payload(project_id, old_head)

        denied = self._post_reconcile(
            project_id,
            reconcile_payload,
            token=False,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["code"], "desktop_auth_required")
        missing_identity = self._post_reconcile(
            project_id,
            reconcile_payload,
            key="timeline-api-reconcile-no-db",
        )
        self.assertEqual(missing_identity.status_code, 400)
        self.assertEqual(
            missing_identity.get_json()["code"],
            "database_identity_required",
        )
        inflated = self._post_reconcile(
            project_id,
            {**reconcile_payload, "extra": True},
            key="timeline-api-reconcile-inflated",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(inflated.status_code, 400)
        self.assertEqual(inflated.get_json()["code"], "invalid_timeline_command")

        first = self._post_reconcile(
            project_id,
            reconcile_payload,
            database_uuid=self.repository.database_uuid,
        )
        replay = self._post_reconcile(
            project_id,
            reconcile_payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.get_json(), first.get_json())
        self.assertEqual(first.get_json()["command_type"], "timeline.reconcile_from_coverage")
        self.assertEqual(first.get_json()["result"]["result_head"]["revision"], 2)
        current = self._project_workspace(project_id)
        self.assertEqual(current["timeline"]["state"], "current")
        self.assertEqual(current["timeline"]["head"]["revision"], 2)
        self.assertFalse(current["capabilities"]["timeline_reconciliation"])
        self.assertTrue(current["capabilities"]["timeline_mutation"])

        no_op = self._post_reconcile(
            project_id,
            self._reconcile_payload(project_id, current["timeline"]["head"]),
            key="timeline-api-reconcile-noop",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(no_op.status_code, 409)
        self.assertEqual(no_op.get_json()["code"], "timeline_already_current")

    def test_gap_is_honest_and_maps_to_422_without_partial_timeline(self) -> None:
        project_id, payload, _source_ids = self._build_project(lowerable=False)

        before = self._project_workspace(project_id)
        self.assertEqual(before["timeline"]["state"], "missing")
        self.assertEqual(before["executable"]["reason_code"], "coverage_not_lowerable")
        self.assertFalse(before["capabilities"]["timeline_materialization"])
        response = self._post(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["code"], "coverage_not_lowerable")
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "canonical_timeline_operations",
                        "canonical_timeline_revisions",
                        "canonical_timeline_heads",
                        "canonical_timeline_receipts",
                    )
                ),
                (0, 0, 0, 0),
            )

    def test_source_staleness_disables_execution_without_source_failover(self) -> None:
        project_id, payload, _source_ids = self._build_project()
        response = self._post(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(response.status_code, 201)
        exact = self.timelines.read(project_id)
        fixed_source_id = str(exact["source_bindings"][0]["asset_source_id"])
        self.repository.mark_source_availability(fixed_source_id, "missing")

        read = self.client.get(f"/v1/creative/projects/{project_id}/timeline")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.get_json()["freshness"]["state"], "stale_source_binding")
        self.assertEqual(
            read.get_json()["source_bindings"][0]["asset_source_id"],
            fixed_source_id,
        )
        workspace = self._project_workspace(project_id)
        self.assertEqual(workspace["timeline"]["state"], "stale_source_binding")
        self.assertEqual(
            workspace["executable"]["reason_code"],
            "canonical_timeline_stale_source_binding",
        )
        self.assertTrue(workspace["capabilities"]["authoritative_timeline_head"])
        for capability in (
            "timeline_materialization",
            "timeline_mutation",
            "preview",
            "render",
            "export",
        ):
            self.assertFalse(workspace["capabilities"][capability])

    def test_upstream_freshness_has_explicit_blueprint_and_coverage_states(self) -> None:
        blueprint_project, blueprint_payload, _source_ids = self._build_project()
        self.assertEqual(
            self._post(
                blueprint_project,
                blueprint_payload,
                database_uuid=self.repository.database_uuid,
            ).status_code,
            201,
        )
        current_blueprint = self.repository.get_blueprint_head(blueprint_project)
        assert current_blueprint is not None
        changed_semantic = copy.deepcopy(current_blueprint["blueprint"]["semantic"])
        changed_semantic["intent"]["goal"] = "A deliberately newer Blueprint goal"
        self.blueprints.commit_proposal(
            blueprint_project,
            {
                "expected_head": {
                    "revision": current_blueprint["revision"],
                    "content_sha256": current_blueprint["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": changed_semantic,
            },
            idempotency_key="timeline-api-new-blueprint",
        )
        stale_blueprint = self._project_workspace(blueprint_project)
        self.assertEqual(stale_blueprint["timeline"]["state"], "stale_blueprint")
        self.assertEqual(
            stale_blueprint["executable"]["reason_code"],
            "canonical_timeline_stale_blueprint",
        )

        coverage_project, coverage_payload, _source_ids = self._build_project()
        self.assertEqual(
            self._post(
                coverage_project,
                coverage_payload,
                database_uuid=self.repository.database_uuid,
            ).status_code,
            201,
        )
        current_blueprint = self.repository.get_blueprint_head(coverage_project)
        current_coverage = self.repository.get_coverage_head(coverage_project)
        assert current_blueprint is not None and current_coverage is not None
        self.coverage.materialize_baseline(
            coverage_project,
            {
                "expected_blueprint": {
                    key: current_blueprint[key]
                    for key in ("revision", "content_sha256", "semantic_sha256")
                },
                "expected_plan_head": {
                    "revision": current_coverage["revision"],
                    "content_sha256": current_coverage["content_sha256"],
                },
            },
            idempotency_key="timeline-api-new-coverage",
            expected_database_uuid=self.repository.database_uuid,
        )
        stale_coverage = self._project_workspace(coverage_project)
        self.assertEqual(stale_coverage["timeline"]["state"], "stale_coverage")
        self.assertEqual(
            stale_coverage["executable"]["reason_code"],
            "canonical_timeline_stale_coverage",
        )

    def test_conflict_receipt_and_missing_source_error_codes_are_stable(self) -> None:
        source_project, source_payload, source_ids = self._build_project()
        self.repository.mark_source_availability(source_ids[0], "missing")
        unavailable = self._post(
            source_project,
            source_payload,
            key="timeline-source-unavailable",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(unavailable.status_code, 422)
        self.assertEqual(unavailable.get_json()["code"], "timeline_source_unavailable")
        with self.repository.transaction() as connection:
            removal = connection.execute(
                """SELECT change.position
                     FROM image_projection_changes change
                     JOIN asset_sources source ON source.asset_id=change.asset_id
                    WHERE source.id=? AND change.operation='remove'
                    ORDER BY change.position DESC LIMIT 1""",
                (source_ids[0],),
            ).fetchone()
        assert removal is not None
        self.repository.project_image_source_removal(
            change_position=int(removal["position"]),
        )

        project_id, payload, _source_ids = self._build_project()
        first = self._post(
            project_id,
            payload,
            key="timeline-permanent-key",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(first.status_code, 201)
        current_wins = self._post(
            project_id,
            payload,
            key="timeline-second-command",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(current_wins.status_code, 409)
        self.assertEqual(
            current_wins.get_json()["code"],
            "timeline_manual_current_conflict",
        )

        rebound = copy.deepcopy(payload)
        rebound["expected_coverage"]["content_sha256"] = "f" * 64
        receipt_conflict = self._post(
            project_id,
            rebound,
            key="timeline-permanent-key",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(receipt_conflict.status_code, 409)
        self.assertEqual(
            receipt_conflict.get_json()["code"],
            "timeline_idempotency_conflict",
        )

    def test_legacy_timeline_remains_metadata_only_and_never_becomes_current(self) -> None:
        project_id, _payload, _source_ids = self._build_project(
            with_legacy_timeline=True
        )

        workspace = self._project_workspace(project_id)
        legacy = workspace["history"]["legacy_artifacts"]
        self.assertEqual(legacy["timeline_revision_count"], 1)
        self.assertEqual(len(legacy["timelines"]), 1)
        self.assertEqual(
            legacy["timelines"][0]["role"],
            "historical_observed_context_only",
        )
        self.assertFalse(legacy["authoritative_timeline_head"])
        self.assertEqual(workspace["timeline"]["state"], "missing")
        self.assertIsNone(workspace["executable"]["current_timeline"])
        self.assertFalse(workspace["capabilities"]["authoritative_timeline_head"])

    def test_timeline_corruption_is_409_and_never_falls_back_to_legacy(self) -> None:
        project_id, payload, _source_ids = self._build_project(
            with_legacy_timeline=True
        )
        response = self._post(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(response.status_code, 201)
        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger_sql = connection.execute(
                """SELECT sql FROM sqlite_schema
                    WHERE name='trg_canonical_timeline_revisions_no_update'"""
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_canonical_timeline_revisions_no_update")
            connection.execute(
                """UPDATE canonical_timeline_revisions SET timeline_json='{}'
                    WHERE project_id=?""",
                (project_id,),
            )
            connection.execute(trigger_sql)
            connection.commit()

        timeline = self.client.get(f"/v1/creative/projects/{project_id}/timeline")
        self.assertEqual(timeline.status_code, 409)
        self.assertEqual(timeline.get_json()["code"], "timeline_integrity_error")
        self.assertNotIn("timeline", timeline.get_json())

        project = self.client.get(f"/v1/creative/projects/{project_id}")
        self.assertEqual(project.status_code, 409)
        self.assertEqual(project.get_json()["code"], "timeline_integrity_error")
        self.assertNotIn("project", project.get_json())

    def test_local_read_boundary_and_exact_revision_errors_are_stable(self) -> None:
        project_id, _payload, _source_ids = self._build_project()
        remote = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            environ_base={"REMOTE_ADDR": "203.0.113.10"},
        )
        self.assertEqual(remote.status_code, 403)
        self.assertEqual(remote.get_json()["code"], "loopback_required")
        malformed = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            query_string={"revision": "not-an-integer"},
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.get_json()["code"], "invalid_revision")
        missing = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            query_string={"revision": "1"},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["code"], "timeline_not_found")


class TimelineLoweringProductionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-runtime-")
        root = Path(self.temporary.name).resolve()
        library = root / "library"
        library.mkdir()
        self.token = "timeline-production-desktop-token"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(
                    Path(__file__).resolve().parents[1] / "config.yaml"
                ),
                "MEMOLENS_APP_STATE_DIR": str(root / "state"),
                "IMAGE_LIBRARY_DIR": str(library),
                "SQLITE_DB_PATH": str(root / "state" / "media.db"),
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

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary.cleanup()

    def test_create_app_exposes_timeline_lowering_through_the_pinned_runtime(self) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        repository = bundle.extension("media_repository")
        service = bundle.extension("timeline_lowering_service")
        self.assertIsInstance(service, TimelineLoweringService)
        self.assertIs(service.repository, repository)
        self.assertIs(self.app.extensions["timeline_lowering_service"], service)

        project = repository.create_project(
            "Production Timeline wiring",
            {"goal": "legacy only", "candidate_refs": []},
            {"created_by": "timeline-production-test"},
        )
        response = self.client.get(
            f"/v1/creative/projects/{project['id']}/timeline",
            query_string={"db_path": str(repository.db_path)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["freshness"]["state"], "missing")
        self.assertIsNone(response.get_json()["head"])


if __name__ == "__main__":
    unittest.main()
