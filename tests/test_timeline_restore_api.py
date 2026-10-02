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


class TimelineRestoreApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-timeline-restore-api-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.addCleanup(self.repository.close)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self.legacy_timelines = TimelineService(self.repository)
        self.token = "timeline-restore-api-desktop-token"

        app = Flask("timeline-restore-api-test")
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

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return api_fixture.TimelineLoweringApiTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        return api_fixture.TimelineLoweringApiTests._build_project(self)

    def _post_materialize(self, project_id: str, payload: object):
        return self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            query_string={
                "db_path": str(self.db_path),
                "expected_database_uuid": self.repository.database_uuid,
            },
            json=payload,
            headers={
                DESKTOP_TOKEN_HEADER: self.token,
                "Idempotency-Key": "timeline-restore-api-materialize",
            },
        )

    def _post_restore(
        self,
        project_id: str,
        payload: object,
        *,
        key: str = "timeline-restore-api-command",
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
            f"/v1/creative/projects/{project_id}/timeline/restore",
            query_string=query,
            json=payload,
            headers=headers,
        )

    def _materialize_and_edit(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
        project_id, materialize, _sources = self._build_project()
        response = self._post_materialize(project_id, materialize)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        revision_one = self.timelines.read(project_id, revision=1)
        head = revision_one["head"]
        timeline = revision_one["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        clip = timeline["tracks"][0]["clips"][0]
        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": materialize["expected_blueprint"],
                "expected_coverage": materialize["expected_coverage"],
                "expected_timeline_head": head,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_350,
                },
            },
            idempotency_key="timeline-restore-api-parent-edit",
            expected_database_uuid=self.repository.database_uuid,
        )
        return project_id, materialize, revision_one, self.timelines.read(project_id)

    @staticmethod
    def _payload(
        materialize: dict[str, object],
        historical: dict[str, object],
        current: dict[str, object],
    ) -> dict[str, object]:
        selected = historical["selected_revision"]
        assert isinstance(selected, dict)
        return {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": current["head"],
            "restore_from": {
                key: value for key, value in selected.items() if key != "is_head"
            },
        }

    def test_historical_get_is_read_only_and_restore_replays_exact_result(self) -> None:
        project_id, materialize, historical, current = self._materialize_and_edit()
        selected = self.client.get(
            f"/v1/creative/projects/{project_id}/timeline",
            query_string={"db_path": str(self.db_path), "revision": 1},
        )
        self.assertEqual(selected.status_code, 200, selected.get_data(as_text=True))
        selected_body = selected.get_json()
        self.assertEqual(selected_body["head"], current["head"])
        self.assertEqual(selected_body["selected_revision"]["revision"], 1)
        self.assertFalse(selected_body["selected_revision"]["is_head"])
        self.assertEqual(self.timelines.read(project_id)["head"], current["head"])

        payload = self._payload(materialize, historical, current)
        first = self._post_restore(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        replay = self._post_restore(
            project_id,
            payload,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(first.status_code, 201, first.get_data(as_text=True))
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201, replay.get_data(as_text=True))
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.get_json(), first.get_json())
        self.assertEqual(first.get_json()["command_type"], "timeline.restore_revision")
        self.assertEqual(first.get_json()["result"]["kind"], "revision_restored")
        self.assertEqual(first.get_json()["result"]["restore_from"], payload["restore_from"])
        self.assertEqual(first.get_json()["result"]["result_head"]["revision"], 3)
        self.assertNotIn(str(self.library), first.get_data(as_text=True))

    def test_restore_route_requires_desktop_database_and_closed_json(self) -> None:
        project_id, materialize, historical, current = self._materialize_and_edit()
        payload = self._payload(materialize, historical, current)
        denied = self._post_restore(
            project_id,
            payload,
            token=False,
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["code"], "desktop_auth_required")

        missing = self._post_restore(project_id, payload, key="restore-no-db")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.get_json()["code"], "database_identity_required")

        inflated = self._post_restore(
            project_id,
            {**payload, "timeline": historical["timeline"]},
            key="restore-inflated",
            database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(inflated.status_code, 400)
        self.assertEqual(
            inflated.get_json()["code"],
            "invalid_timeline_restore_command",
        )
        self.assertEqual(self.timelines.read(project_id)["head"], current["head"])


if __name__ == "__main__":
    unittest.main()
