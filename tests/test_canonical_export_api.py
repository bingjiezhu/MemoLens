from __future__ import annotations

from types import SimpleNamespace
import unittest

from flask import Flask

from backend.src import MAIN_AUTHORITY_HEADER
from backend.src.api import api_blueprint
from core.media_db import CanonicalExportIntegrityError


class _CanonicalExportServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def presentation(self, project_id: str) -> dict[str, object]:
        self.calls.append(("presentation", project_id))
        return {
            "object": "canonical_export.presentation_envelope",
            "schema_version": "1",
            "presentation": {"project_id": project_id},
            "presentation_sha256": "a" * 64,
        }

    def start(
        self,
        project_id: str,
        payload: object,
        *,
        runtime_epoch: str,
        idempotency_key: str,
    ) -> SimpleNamespace:
        self.calls.append(
            ("start", project_id, payload, runtime_epoch, idempotency_key)
        )
        return SimpleNamespace(
            response={
                "object": "canonical_export.command_result",
                "schema_version": "1",
                "project_id": project_id,
            },
            response_status=201,
            replayed=False,
        )

    def list_jobs(self, project_id: str, *, limit: int) -> dict[str, object]:
        self.calls.append(("list", project_id, limit))
        return {
            "object": "canonical_export.job_list",
            "schema_version": "1",
            "project_id": project_id,
            "jobs": [],
        }

    def read_job(self, project_id: str, job_id: str) -> dict[str, object]:
        self.calls.append(("job", project_id, job_id))
        return {
            "object": "canonical_export.job",
            "schema_version": "1",
            "id": job_id,
        }

    def read_revision(self, project_id: str, revision: int) -> dict[str, object]:
        self.calls.append(("revision", project_id, revision))
        return {
            "object": "canonical_export.revision",
            "schema_version": "1",
            "revision": revision,
        }


class CanonicalExportApiBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.main_token = "independent-main-export-token"
        self.desktop_token = "renderer-desktop-token"
        self.service = _CanonicalExportServiceStub()
        app = Flask("canonical-export-api-test")
        app.config.update(
            TESTING=True,
            MAIN_AUTHORITY_TOKEN=self.main_token,
            DESKTOP_SESSION_TOKEN=self.desktop_token,
            RUNTIME_AUTHORITY_EPOCH="runtime-export-api",
        )
        app.extensions["canonical_export_service"] = self.service
        app.register_blueprint(api_blueprint)
        self.client = app.test_client()

    @property
    def presentation_path(self) -> str:
        return "/v1/main/creative/projects/project-1/exports/presentation"

    @property
    def start_path(self) -> str:
        return "/v1/main/creative/projects/project-1/exports"

    def test_main_export_routes_require_originless_independent_authority(self) -> None:
        for headers in (
            {},
            {"X-MemoLens-Desktop-Token": self.desktop_token},
            {
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Origin": "http://127.0.0.1:5173",
            },
        ):
            response = self.client.post(
                self.presentation_path,
                json={},
                headers=headers,
            )
            self.assertEqual(response.status_code, 403, response.json)
            self.assertEqual(response.json["code"], "native_confirmation_required")
        self.assertEqual(self.service.calls, [])

    def test_presentation_is_closed_empty_json_and_query_free(self) -> None:
        headers = {MAIN_AUTHORITY_HEADER: self.main_token}
        for kwargs, expected_status in (
            ({"json": {"extra": True}}, 400),
            ({"json": {}, "query_string": {"unexpected": "1"}}, 409),
            (
                {
                    "data": '{"extra":1,"extra":2}',
                    "content_type": "application/json",
                },
                400,
            ),
        ):
            response = self.client.post(
                self.presentation_path,
                headers=headers,
                **kwargs,
            )
            self.assertEqual(response.status_code, expected_status, response.json)
        response = self.client.post(
            self.presentation_path,
            json={},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(
            self.service.calls,
            [("presentation", "project-1")],
        )

    def test_start_passes_only_strict_body_epoch_and_idempotency(self) -> None:
        payload = {
            "presentation": {"project_id": "project-1"},
            "presentation_sha256": "a" * 64,
            "native_gesture_nonce": "native-export-gesture-1",
            "destination": {
                "canonical_path": "/tmp/exports",
                "package_basename": "film",
                "selection_identity": {"device": "1", "inode": "2"},
            },
        }
        missing_key = self.client.post(
            self.start_path,
            json=payload,
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(missing_key.status_code, 400, missing_key.json)
        self.assertEqual(missing_key.json["code"], "invalid_idempotency_key")

        response = self.client.post(
            self.start_path,
            json=payload,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "export-api-one",
            },
        )
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.headers["Idempotency-Replayed"], "false")
        self.assertEqual(
            self.service.calls,
            [
                (
                    "start",
                    "project-1",
                    payload,
                    "runtime-export-api",
                    "export-api-one",
                )
            ],
        )

    def test_renderer_reads_are_query_bounded_and_revision_route_wins(self) -> None:
        listed = self.client.get(
            "/v1/creative/projects/project-1/exports?limit=7"
        )
        self.assertEqual(listed.status_code, 200, listed.json)
        self.assertEqual(self.service.calls[-1], ("list", "project-1", 7))

        rejected = self.client.get(
            "/v1/creative/projects/project-1/exports?limit=7&unexpected=1"
        )
        self.assertEqual(rejected.status_code, 400, rejected.json)

        revision = self.client.get(
            "/v1/creative/projects/project-1/exports/revisions/7"
        )
        self.assertEqual(revision.status_code, 200, revision.json)
        self.assertEqual(revision.json["object"], "canonical_export.revision")
        self.assertEqual(self.service.calls[-1], ("revision", "project-1", 7))

    def test_workspace_maps_canonical_export_integrity_failure_to_stable_409(self) -> None:
        class BrokenWorkspaceService:
            @staticmethod
            def project_workspace(_project_id: str, *, limit: int):
                del limit
                raise CanonicalExportIntegrityError("injected_v8_tamper")

        self.client.application.extensions["blueprint_service"] = (
            BrokenWorkspaceService()
        )
        response = self.client.get("/v1/creative/projects/project-1")
        self.assertEqual(response.status_code, 409, response.json)
        self.assertEqual(response.json["code"], "canonical_export_integrity_error")


if __name__ == "__main__":
    unittest.main()
