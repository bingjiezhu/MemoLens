from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from flask import Flask

from backend.src import DESKTOP_TOKEN_HEADER
from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline import TimelineService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
import test_timeline_lowering_api as api_fixture


class TimelineEditApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-edit-api-")
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
        self.token = "timeline-edit-api-desktop-token"

        app = Flask("timeline-edit-api-test")
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
        return api_fixture.TimelineLoweringApiTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        return api_fixture.TimelineLoweringApiTests._build_project(self)

    def _post_materialize(
        self,
        project_id: str,
        payload: object,
        *,
        key: str = "timeline-edit-api-materialize",
    ):
        return self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            query_string={
                "db_path": str(self.db_path),
                "expected_database_uuid": self.repository.database_uuid,
            },
            json=payload,
            headers={
                DESKTOP_TOKEN_HEADER: self.token,
                "Idempotency-Key": key,
            },
        )

    def _post_edit(
        self,
        project_id: str,
        payload: object,
        *,
        key: str = "timeline-edit-api-command",
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
            f"/v1/creative/projects/{project_id}/timeline/edit",
            query_string=query,
            json=payload,
            headers=headers,
        )

    def _current_edit_payload(
        self,
        project_id: str,
        materialize_payload: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        workspace = self.timelines.read(project_id)
        head = workspace["head"]
        timeline = workspace["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        clip = timeline["tracks"][0]["clips"][0]
        return (
            {
                "expected_blueprint": materialize_payload["expected_blueprint"],
                "expected_coverage": materialize_payload["expected_coverage"],
                "expected_timeline_head": head,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_400,
                },
            },
            head,
        )

    def test_edit_route_requires_desktop_database_and_closed_json(self) -> None:
        project_id, materialize, _sources = self._build_project()
        self.assertEqual(
            self._post_materialize(project_id, materialize).status_code,
            201,
        )
        payload, _head = self._current_edit_payload(project_id, materialize)

        denied = self._post_edit(
            project_id,
            payload,
            token=False,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["code"], "desktop_auth_required")

        missing_identity = self._post_edit(project_id, payload, key="edit-no-db")
        self.assertEqual(missing_identity.status_code, 400)
        self.assertEqual(
            missing_identity.get_json()["code"],
            "database_identity_required",
        )

        inflated_command = self._post_edit(
            project_id,
            {**payload, "raw_path": str(self.library)},
            key="edit-inflated-command",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(inflated_command.status_code, 400)
        self.assertEqual(
            inflated_command.get_json()["code"],
            "invalid_timeline_edit_command",
        )

        inflated_edit = {
            **payload,
            "edit": {**payload["edit"], "asset_source_id": "src_forbidden"},
        }
        rejected_source = self._post_edit(
            project_id,
            inflated_edit,
            key="edit-inflated-edit",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(rejected_source.status_code, 422)
        self.assertEqual(rejected_source.get_json()["code"], "invalid_timeline_edit")
        self.assertNotIn(str(self.library), rejected_source.get_data(as_text=True))

    def test_edit_route_replays_receipt_and_exposes_current_mutation_capability(self) -> None:
        project_id, materialize, _sources = self._build_project()
        self.assertEqual(
            self._post_materialize(project_id, materialize).status_code,
            201,
        )
        payload, old_head = self._current_edit_payload(project_id, materialize)

        first = self._post_edit(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        replay = self._post_edit(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )

        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.get_json(), first.get_json())
        body = first.get_json()
        self.assertEqual(body["command_type"], "timeline.apply_edit")
        self.assertEqual(body["result"]["edit"], payload["edit"])
        self.assertEqual(body["result"]["result_head"]["revision"], 2)
        self.assertNotEqual(body["result"]["result_head"], old_head)
        self.assertNotIn(str(self.library), first.get_data(as_text=True))

        reopened_response = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            query_string={"db_path": str(self.db_path)},
        )
        self.assertEqual(
            reopened_response.status_code,
            200,
            reopened_response.get_data(as_text=True),
        )
        reopened = reopened_response.get_json()
        self.assertEqual(reopened["head"], body["result"]["result_head"])
        self.assertTrue(reopened["selected_revision"]["is_head"])
        self.assertEqual(reopened["timeline"]["revision"], 2)
        edited_clip = reopened["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(edited_clip["end_ms"] - edited_clip["start_ms"], 1_400)
        self.assertNotIn(str(self.library), reopened_response.get_data(as_text=True))

        historical_response = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            query_string={"db_path": str(self.db_path), "revision": 1},
        )
        self.assertEqual(historical_response.status_code, 200)
        historical = historical_response.get_json()
        self.assertEqual(historical["head"], body["result"]["result_head"])
        self.assertFalse(historical["selected_revision"]["is_head"])
        self.assertEqual(historical["timeline"]["revision"], 1)
        historical_clip = historical["timeline"]["tracks"][0]["clips"][0]
        self.assertNotEqual(
            historical_clip["end_ms"] - historical_clip["start_ms"],
            1_400,
        )

        workspace_response = self.client.get(
            f"/v1/creative/projects/{project_id}",
            query_string={"db_path": str(self.db_path)},
        )
        self.assertEqual(workspace_response.status_code, 200)
        workspace = workspace_response.get_json()["project"]
        self.assertEqual(workspace["timeline"]["head"]["revision"], 2)
        self.assertTrue(workspace["capabilities"]["timeline_mutation"])

        conflict = self._post_edit(
            project_id,
            {**payload, "edit": {**payload["edit"], "duration_ms": 1_401}},
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "timeline_idempotency_conflict")


if __name__ == "__main__":
    unittest.main()
