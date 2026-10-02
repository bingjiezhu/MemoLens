from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.src import (
    DESKTOP_TOKEN_HEADER,
    MAIN_AUTHORITY_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from backend.src.agent_authority import (
    AGENT_CAPABILITY_HEADER,
    AGENT_NONCE_HEADER,
    AGENT_PROOF_HEADER,
    AGENT_STATUS_PROOF_HEADER,
    agent_request_proof,
    agent_status_proof,
    proof_secret_sha256,
)
from backend.src.media.timeline_lowering import TimelineLoweringServiceError
from core.config import Settings
from core.media_db import (
    AgentCapabilityError,
    CanonicalTimelineIntegrityError,
    MediaRepository,
    canonical_json,
    content_sha256,
)
from core.timeline_lowering_contract import canonical_timeline_content_sha256
from core.timeline_structural_edit_contract import (
    require_exact_timeline_structural_edit_replay,
)

try:
    from tests import test_timeline_lowering_integration as integration_fixture
    from tests import test_timeline_lowering_api as timeline_fixture
except ImportError:  # unittest discover adds tests/ directly to sys.path.
    import test_timeline_lowering_integration as integration_fixture
    import test_timeline_lowering_api as timeline_fixture


SECRET = "c4" * 32


class TimelineStructuralEditBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-timeline-structural-backend-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "index.db"
        self.desktop_token = "timeline-structural-desktop-token"
        self.main_token = "timeline-structural-main-token"
        self.epoch = "epoch_timeline_structural_test"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(
                    Path(__file__).resolve().parents[1] / "config.yaml"
                ),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": self.main_token,
                "MEMOLENS_RUNTIME_AUTHORITY_EPOCH": self.epoch,
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
        self.blueprints = self.app.extensions["blueprint_service"]
        self.coverage = self.app.extensions["coverage_service"]
        self.timelines = self.app.extensions["timeline_lowering_service"]
        self.legacy_timelines = self.app.extensions["timeline_service"]
        self.token = self.desktop_token
        self._native_nonce_sequence = 0

        self.project_id, self.materialize_payload, _sources = self._build_project()
        materialized = self.timelines.materialize_first_cut(
            self.project_id,
            self.materialize_payload,
            idempotency_key="structural-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(materialized.response_status, 201)
        workspace = self.timelines.read(self.project_id)
        self.timeline_head = workspace["head"]
        timeline = workspace["timeline"]
        assert isinstance(self.timeline_head, dict) and isinstance(timeline, dict)
        self.clip_ids = [
            str(clip["clip_id"])
            for clip in timeline["tracks"][0]["clips"]
        ]
        self.assertEqual(len(self.clip_ids), 2)

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary.cleanup()
        gc.collect()

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringApiTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        return timeline_fixture.TimelineLoweringApiTests._build_project(self)

    def _build_video_project(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        """Materialize one exact image + verified video-span Timeline."""

        image = self._register_image(
            "structural-video-opening.jpg",
            b"structural-video-opening",
        )
        span_ref = (
            integration_fixture.TimelineLoweringIntegrationTests._register_video_span(
                self
            )
        )
        proposed = timeline_fixture.semantic(
            "structural-video",
            evidence_ref=f"memolens://evidence/asset/{image['id']}",
        )
        proposed["material_hints"].append(
            {
                "hint_id": "structural_video_span",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact verified video span.",
            }
        )
        project = self.repository.create_project(
            "Structural video Timeline",
            {"goal": "split and restore", "candidate_refs": []},
            {"created_by": "structural-backend-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
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
            idempotency_key="structural-video-blueprint",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            field: blueprint[field]
            for field in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    field: blueprint_binding[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key="structural-video-coverage",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        payload = {
            "expected_blueprint": blueprint_binding,
            "expected_coverage": {
                field: coverage[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": None,
        }
        materialized = self.timelines.materialize_first_cut(
            project_id,
            payload,
            idempotency_key="structural-video-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(materialized.response_status, 201)
        return project_id, payload, self.timelines.read(project_id)

    def _body(self) -> dict[str, object]:
        return {
            "expected_blueprint": self.materialize_payload["expected_blueprint"],
            "expected_coverage": self.materialize_payload["expected_coverage"],
            "expected_timeline_head": self.timeline_head,
            "structural_edit": {
                "op": "delete_clip",
                "clip_id": self.clip_ids[0],
            },
        }

    def _structural_state(
        self,
        project_id: str | None = None,
    ) -> dict[str, object]:
        """Freeze every durable row that one structural command may change."""

        tables = (
            "canonical_timeline_heads",
            "canonical_timeline_operations",
            "canonical_timeline_revisions",
            "canonical_timeline_receipts",
            "agent_project_command_receipts",
            "agent_project_capability_events",
        )
        with closing(self.repository._connect()) as connection:
            rows = {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                )
                for table in tables
            }
        return {
            "rows": rows,
            "head": self.repository.get_canonical_timeline_head(
                project_id or self.project_id
            ),
        }

    def _assert_cold_structural_state(
        self,
        expected: dict[str, object],
        *,
        project_id: str | None = None,
    ) -> None:
        """Require the same durable state through a new repository/connection."""

        cold = MediaRepository(self.db_path)
        try:
            self.assertEqual(
                cold.get_canonical_timeline_head(project_id or self.project_id),
                expected["head"],
            )
            with closing(cold._connect()) as connection:
                rows = {
                    table: tuple(
                        tuple(row)
                        for row in connection.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        )
                    )
                    for table in expected["rows"]
                }
            self.assertEqual(rows, expected["rows"])
        finally:
            cold.close()

    def _pair(
        self,
        *,
        actions: list[str],
        max_operations: int = 2,
        secret: str = SECRET,
    ) -> str:
        blueprint = self.repository.get_blueprint_head(self.project_id)
        assert blueprint is not None
        pairing = self.client.post(
            "/v1/agent/pairings",
            json={
                "object": "memolens.agent_pairing_request",
                "schema_version": "1",
                "project_id": self.project_id,
                "observed_head": {
                    "revision": blueprint["revision"],
                    "content_sha256": blueprint["content_sha256"],
                },
                "subject_id": f"structural_{self._native_nonce_sequence}",
                "claimed_client_label": "Codex or DeepSeek canonical editor",
                "proof_secret": secret,
                "actions": actions,
                "ttl_seconds": 900,
                "max_operations": max_operations,
            },
        )
        self.assertEqual(pairing.status_code, 201, pairing.json)
        pairing_id = str(pairing.json["pairing_id"])
        presentation = self.client.get(
            f"/v1/main/agent/pairings/{pairing_id}/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        self._native_nonce_sequence += 1
        approved = self.client.post(
            f"/v1/main/agent/pairings/{pairing_id}/approve",
            json={
                "presentation_sha256": presentation.json["presentation_sha256"],
                "native_gesture_nonce": (
                    f"native_structural_{self._native_nonce_sequence}"
                ),
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(approved.status_code, 201, approved.json)
        return str(approved.json["capability_id"])

    def _nonce(self, capability_id: str, *, action: str, secret: str = SECRET):
        return self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    secret,
                    kind="nonce",
                    identifier=f"{capability_id}:{action}",
                )
            },
        )

    def _agent_save(
        self,
        capability_id: str,
        body: dict[str, object],
        *,
        key: str,
        secret: str = SECRET,
    ):
        action = "timeline.apply_structural_edit"
        nonce_response = self._nonce(
            capability_id,
            action=action,
            secret=secret,
        )
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        nonce = str(nonce_response.json["nonce"])
        path = (
            f"/v1/agent/creative/projects/{self.project_id}"
            "/timeline/structural-edit"
        )
        proof = agent_request_proof(
            secret,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest(),
            idempotency_key=key,
        )
        return self.client.post(
            path,
            json=body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: nonce,
                AGENT_PROOF_HEADER: proof,
                "Idempotency-Key": key,
            },
        )

    def _agent_request(
        self,
        capability_id: str,
        body: dict[str, object],
        *,
        key: str,
        secret: str = SECRET,
        nonce_action: str = "timeline.apply_structural_edit",
        actual_project_id: str | None = None,
        signed_database_uuid: str | None = None,
        signed_project_id: str | None = None,
        signed_key: str | None = None,
        signed_path: str | None = None,
        actual_path: str | None = None,
        proof_body: dict[str, object] | None = None,
        include_key: bool = True,
        environ_overrides: dict[str, str] | None = None,
    ):
        nonce_response = self._nonce(
            capability_id,
            action=nonce_action,
            secret=secret,
        )
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        nonce = str(nonce_response.json["nonce"])
        project_id = actual_project_id or self.project_id
        default_path = (
            f"/v1/agent/creative/projects/{project_id}"
            "/timeline/structural-edit"
        )
        path = actual_path or default_path
        canonical_path = signed_path or default_path
        signed_body = proof_body if proof_body is not None else body
        proof = agent_request_proof(
            secret,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=canonical_path,
            database_uuid=(
                self.repository.database_uuid
                if signed_database_uuid is None
                else signed_database_uuid
            ),
            project_id=project_id if signed_project_id is None else signed_project_id,
            body_sha256=hashlib.sha256(
                canonical_json(signed_body).encode("utf-8")
            ).hexdigest(),
            idempotency_key=key if signed_key is None else signed_key,
        )
        headers = {
            AGENT_CAPABILITY_HEADER: capability_id,
            AGENT_NONCE_HEADER: nonce,
            AGENT_PROOF_HEADER: proof,
        }
        if include_key:
            headers["Idempotency-Key"] = key
        return self.client.post(
            path,
            json=body,
            headers=headers,
            environ_overrides=environ_overrides,
        )

    def test_desktop_structural_authority_scope_and_fault_denials_write_zero_rows(
        self,
    ) -> None:
        action_path = (
            f"/v1/creative/projects/{self.project_id}/timeline/structural-edit"
        )
        body = self._body()
        valid_query = {
            "db_path": str(self.db_path),
            "expected_database_uuid": self.repository.database_uuid,
        }
        valid_headers = {
            DESKTOP_TOKEN_HEADER: self.desktop_token,
            "Idempotency-Key": "structural-desktop-denial",
        }
        other_project_id, other_materialize, _other_sources = self._build_project()
        other_materialized = self.timelines.materialize_first_cut(
            other_project_id,
            other_materialize,
            idempotency_key="structural-desktop-other-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(other_materialized.response_status, 201)

        cases = (
            (
                "missing-desktop-session",
                action_path,
                valid_query,
                body,
                {"Idempotency-Key": "structural-desktop-no-session"},
                None,
                401,
                "desktop_auth_required",
            ),
            (
                "non-loopback",
                action_path,
                valid_query,
                body,
                valid_headers,
                {"REMOTE_ADDR": "198.51.100.11"},
                403,
                None,
            ),
            (
                "missing-idempotency",
                action_path,
                valid_query,
                body,
                {DESKTOP_TOKEN_HEADER: self.desktop_token},
                None,
                400,
                "invalid_idempotency_key",
            ),
            (
                "missing-active-database-path",
                action_path,
                {
                    "expected_database_uuid": self.repository.database_uuid,
                },
                body,
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-no-db-path",
                },
                None,
                409,
                "database_binding_required",
            ),
            (
                "wrong-active-database-path",
                action_path,
                {
                    **valid_query,
                    "db_path": str(self.root / "outside" / "foreign.db"),
                },
                body,
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-wrong-db-path",
                },
                None,
                409,
                "database_binding_mismatch",
            ),
            (
                "wrong-active-database-identity",
                action_path,
                {
                    **valid_query,
                    "expected_database_uuid": (
                        "00000000-0000-4000-8000-000000000000"
                    ),
                },
                body,
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-wrong-db-identity",
                },
                None,
                409,
                "database_identity_changed",
            ),
            (
                "creative-project",
                (
                    f"/v1/creative/projects/{other_project_id}"
                    "/timeline/structural-edit"
                ),
                valid_query,
                body,
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-wrong-project",
                },
                None,
                409,
                "timeline_head_conflict",
            ),
            (
                "canonical-timeline-head",
                action_path,
                valid_query,
                {
                    **body,
                    "expected_timeline_head": {
                        **body["expected_timeline_head"],
                        "revision_sha256": "f" * 64,
                    },
                },
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-stale-timeline",
                },
                None,
                409,
                "timeline_head_conflict",
            ),
            (
                "blueprint-head",
                action_path,
                valid_query,
                {
                    **body,
                    "expected_blueprint": {
                        **body["expected_blueprint"],
                        "content_sha256": "f" * 64,
                    },
                },
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-stale-blueprint",
                },
                None,
                409,
                "blueprint_head_conflict",
            ),
            (
                "coverage-head",
                action_path,
                valid_query,
                {
                    **body,
                    "expected_coverage": {
                        **body["expected_coverage"],
                        "content_sha256": "f" * 64,
                    },
                },
                {
                    **valid_headers,
                    "Idempotency-Key": "structural-desktop-stale-coverage",
                },
                None,
                409,
                "coverage_head_conflict",
            ),
        )
        for (
            label,
            path,
            query,
            request_body,
            headers,
            environ_overrides,
            status,
            code,
        ) in cases:
            with self.subTest(denial=label):
                before = self._structural_state()
                response = self.client.post(
                    path,
                    query_string=query,
                    json=request_body,
                    headers=headers,
                    environ_overrides=environ_overrides,
                )
                self.assertEqual(response.status_code, status, response.json)
                if code is not None:
                    self.assertEqual(response.json["code"], code)
                self.assertEqual(self._structural_state(), before)

        before_fault = self._structural_state()
        prior_testing = self.app.testing
        self.app.testing = True
        try:
            with patch.object(
                self.repository,
                "append_canonical_timeline_operation",
                side_effect=RuntimeError("injected-desktop-structural-fault"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected-desktop-structural-fault",
                ):
                    self.client.post(
                        action_path,
                        query_string=valid_query,
                        json=body,
                        headers={
                            **valid_headers,
                            "Idempotency-Key": "structural-desktop-fault",
                        },
                    )
        finally:
            self.app.testing = prior_testing
        self.assertEqual(self._structural_state(), before_fault)

    def test_paired_structural_authority_scope_and_fault_denials_write_zero_rows(
        self,
    ) -> None:
        body = self._body()
        action_path = (
            f"/v1/agent/creative/projects/{self.project_id}"
            "/timeline/structural-edit"
        )

        before_missing = self._structural_state()
        missing = self.client.post(action_path, json=body)
        self.assertEqual(missing.status_code, 401, missing.json)
        self.assertEqual(missing.json["code"], "agent_pairing_required")
        self.assertEqual(self._structural_state(), before_missing)

        structural_capability = self._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=16,
        )
        edit_secret = "d4" * 32
        edit_capability = self._pair(
            actions=["timeline.apply_edit"],
            max_operations=4,
            secret=edit_secret,
        )

        def assert_unchanged(
            response,
            before,
            status: int,
            code: str | None,
        ) -> None:
            self.assertEqual(response.status_code, status, response.json)
            if code is not None:
                self.assertEqual(response.json["code"], code)
            self.assertEqual(self._structural_state(), before)

        before = self._structural_state()
        assert_unchanged(
            self._agent_request(
                structural_capability,
                body,
                key="",
                include_key=False,
            ),
            before,
            400,
            "invalid_idempotency_key",
        )

        before = self._structural_state()
        assert_unchanged(
            self._agent_request(
                structural_capability,
                body,
                key="structural-agent-wrong-db",
                signed_database_uuid=(
                    "00000000-0000-4000-8000-000000000000"
                ),
            ),
            before,
            401,
            "agent_pairing_proof_invalid",
        )

        before = self._structural_state()
        assert_unchanged(
            self._agent_request(
                structural_capability,
                body,
                key="structural-agent-wrong-project",
                actual_project_id="not_this_project",
            ),
            before,
            403,
            "agent_pairing_scope_denied",
        )

        before = self._structural_state()
        assert_unchanged(
            self._agent_request(
                edit_capability,
                body,
                key="structural-agent-wrong-action",
                secret=edit_secret,
                nonce_action="timeline.apply_edit",
            ),
            before,
            403,
            "agent_pairing_scope_denied",
        )

        before = self._structural_state()
        assert_unchanged(
            self._agent_request(
                structural_capability,
                body,
                key="structural-agent-non-loopback",
                environ_overrides={"REMOTE_ADDR": "198.51.100.12"},
            ),
            before,
            403,
            None,
        )

        stale_variants = (
            (
                "canonical-timeline-head",
                "expected_timeline_head",
                "revision_sha256",
                "timeline_head_conflict",
            ),
            (
                "blueprint-head",
                "expected_blueprint",
                "content_sha256",
                "blueprint_head_conflict",
            ),
            (
                "coverage-head",
                "expected_coverage",
                "content_sha256",
                "coverage_head_conflict",
            ),
        )
        for label, section, field, code in stale_variants:
            stale_body = copy.deepcopy(body)
            stale_body[section][field] = "e" * 64
            with self.subTest(scope=label):
                before = self._structural_state()
                response = self._agent_request(
                    structural_capability,
                    stale_body,
                    key=f"structural-agent-stale-{label}",
                )
                assert_unchanged(response, before, 409, code)

        before_fault = self._structural_state()
        prior_testing = self.app.testing
        self.app.testing = True
        try:
            with patch.object(
                self.repository,
                "append_canonical_timeline_operation",
                side_effect=RuntimeError("injected-paired-structural-fault"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected-paired-structural-fault",
                ):
                    self._agent_request(
                        structural_capability,
                        body,
                        key="structural-agent-fault",
                    )
        finally:
            self.app.testing = prior_testing
        self.assertEqual(self._structural_state(), before_fault)

    def test_concurrent_structural_same_head_has_one_cas_winner(self) -> None:
        capability_id = self._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=4,
        )
        path = (
            f"/v1/agent/creative/projects/{self.project_id}"
            "/timeline/structural-edit"
        )
        requests: list[tuple[dict[str, object], dict[str, str]]] = []
        for index, clip_id in enumerate(self.clip_ids, start=1):
            body = {
                **self._body(),
                "structural_edit": {
                    "op": "delete_clip",
                    "clip_id": clip_id,
                },
            }
            key = f"structural-agent-cas-{index}"
            nonce_response = self._nonce(
                capability_id,
                action="timeline.apply_structural_edit",
            )
            self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
            nonce = str(nonce_response.json["nonce"])
            proof = agent_request_proof(
                SECRET,
                capability_id=capability_id,
                nonce=nonce,
                method="POST",
                canonical_path=path,
                database_uuid=self.repository.database_uuid,
                project_id=self.project_id,
                body_sha256=hashlib.sha256(
                    canonical_json(body).encode("utf-8")
                ).hexdigest(),
                idempotency_key=key,
            )
            requests.append(
                (
                    body,
                    {
                        AGENT_CAPABILITY_HEADER: capability_id,
                        AGENT_NONCE_HEADER: nonce,
                        AGENT_PROOF_HEADER: proof,
                        "Idempotency-Key": key,
                    },
                )
            )
        before = self._structural_state()

        def post(item: tuple[dict[str, object], dict[str, str]]):
            body, headers = item
            with self.app.test_client() as client:
                response = client.post(path, json=body, headers=headers)
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(post, requests))

        self.assertEqual(sorted(status for status, _body in outcomes), [201, 409])
        loser = next(body for status, body in outcomes if status == 409)
        self.assertEqual(loser["code"], "timeline_head_conflict")
        after = self._structural_state()
        expected_deltas = {
            "canonical_timeline_heads": 0,
            "canonical_timeline_operations": 1,
            "canonical_timeline_revisions": 1,
            "canonical_timeline_receipts": 0,
            "agent_project_command_receipts": 1,
            "agent_project_capability_events": 1,
        }
        for table, expected_delta in expected_deltas.items():
            self.assertEqual(
                len(after["rows"][table]) - len(before["rows"][table]),
                expected_delta,
                table,
            )
        self.assertEqual(after["head"]["revision"], 2)
        clips = self.timelines.read(self.project_id)["timeline"]["tracks"][0][
            "clips"
        ]
        self.assertEqual(len(clips), 1)
        self.assertIn(str(clips[0]["clip_id"]), self.clip_ids)
        capability = self.repository.get_agent_project_capability(
            capability_id,
            runtime_authority_epoch=self.epoch,
        )
        assert capability is not None
        self.assertEqual(capability["operations_used"], 1)
        self._assert_cold_structural_state(after)

    def test_missing_and_changed_sources_fail_before_structural_mutation(self) -> None:
        cases: list[
            tuple[str, str, dict[str, object], dict[str, object], str]
        ] = []
        current = self.timelines.read(self.project_id)
        cases.append(
            (
                "missing",
                self.project_id,
                self.materialize_payload,
                current,
                "structural-source-missing",
            )
        )

        changed_project, changed_payload, _sources = self._build_project()
        changed_materialized = self.timelines.materialize_first_cut(
            changed_project,
            changed_payload,
            idempotency_key="structural-changed-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(changed_materialized.response_status, 201)
        cases.append(
            (
                "changed",
                changed_project,
                changed_payload,
                self.timelines.read(changed_project),
                "structural-source-changed",
            )
        )

        for availability, project_id, payload, workspace, key in cases:
            with self.subTest(availability=availability):
                timeline = workspace["timeline"]
                head = workspace["head"]
                source_bindings = workspace["source_bindings"]
                assert (
                    isinstance(timeline, dict)
                    and isinstance(head, dict)
                    and isinstance(source_bindings, list)
                )
                source_id = str(source_bindings[0]["asset_source_id"])
                clip_id = str(timeline["tracks"][0]["clips"][0]["clip_id"])
                self.repository.mark_source_availability(source_id, availability)
                before = self._structural_state(project_id)
                with self.assertRaises(TimelineLoweringServiceError) as raised:
                    self.timelines.apply_structural_edit(
                        project_id,
                        {
                            "expected_blueprint": payload["expected_blueprint"],
                            "expected_coverage": payload["expected_coverage"],
                            "expected_timeline_head": head,
                            "structural_edit": {
                                "op": "delete_clip",
                                "clip_id": clip_id,
                            },
                        },
                        idempotency_key=key,
                        expected_database_uuid=self.repository.database_uuid,
                    )
                self.assertEqual(raised.exception.code, "timeline_not_current")
                self.assertEqual(self._structural_state(project_id), before)
                self._assert_cold_structural_state(
                    before,
                    project_id=project_id,
                )

    def test_paired_structural_fault_matrix_rolls_back_every_stage(self) -> None:
        capability_id = self._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=16,
        )
        body = self._body()
        stages = (
            ("revision", "append_canonical_timeline_revision"),
            ("operation", "append_canonical_timeline_operation"),
            ("head", "cas_canonical_timeline_head"),
            ("agent-receipt", "_store_agent_project_timeline_receipt"),
            ("use-event", "_append_capability_event"),
            ("after-use", "_prepare_verified_canonical_timeline_successor"),
        )
        prior_testing = self.app.testing
        self.app.testing = True
        try:
            for label, method_name in stages:
                with self.subTest(stage=label):
                    before = self._structural_state()
                    original = getattr(self.repository, method_name)

                    def fail_after(*args, _original=original, **kwargs):
                        _original(*args, **kwargs)
                        raise RuntimeError(f"injected-structural-{label}")

                    with patch.object(
                        self.repository,
                        method_name,
                        side_effect=fail_after,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            f"injected-structural-{label}",
                        ):
                            self._agent_request(
                                capability_id,
                                body,
                                key=f"structural-fault-{label}",
                            )
                    self.assertEqual(self._structural_state(), before)
                    self._assert_cold_structural_state(before)
        finally:
            self.app.testing = prior_testing

    def test_structural_capability_lifecycle_denials_write_zero_rows(self) -> None:
        body = self._body()

        for action, scoped_secret in (
            ("timeline.restore_revision", "d1" * 32),
            ("timeline.preview_media", "d2" * 32),
        ):
            with self.subTest(capability_action=action):
                scoped_capability = self._pair(
                    actions=[action],
                    secret=scoped_secret,
                )
                before = self._structural_state()
                denied = self._nonce(
                    scoped_capability,
                    action="timeline.apply_structural_edit",
                    secret=scoped_secret,
                )
                self.assertEqual(denied.status_code, 403, denied.json)
                self.assertEqual(denied.json["code"], "agent_pairing_scope_denied")
                self.assertEqual(self._structural_state(), before)
                self._assert_cold_structural_state(before)

        structural_capability = self._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=8,
        )
        before = self._structural_state()
        denied_restore = self._nonce(
            structural_capability,
            action="timeline.restore_revision",
        )
        self.assertEqual(denied_restore.status_code, 403, denied_restore.json)
        self.assertEqual(
            denied_restore.json["code"],
            "agent_pairing_scope_denied",
        )
        self.assertEqual(self._structural_state(), before)

        before = self._structural_state()
        wrong_secret_nonce = self._nonce(
            structural_capability,
            action="timeline.apply_structural_edit",
            secret="ff" * 32,
        )
        self.assertEqual(wrong_secret_nonce.status_code, 401)
        self.assertEqual(
            wrong_secret_nonce.json["code"],
            "agent_pairing_proof_invalid",
        )
        self.assertEqual(self._structural_state(), before)
        with self.assertRaises(AgentCapabilityError) as wrong_secret:
            self.timelines.apply_paired_structural_edit(
                self.project_id,
                body,
                idempotency_key="structural-wrong-secret-direct",
                expected_database_uuid=self.repository.database_uuid,
                paired_capability_id=structural_capability,
                runtime_authority_epoch=self.epoch,
                presented_secret_sha256=proof_secret_sha256("ff" * 32),
            )
        self.assertEqual(wrong_secret.exception.code, "agent_pairing_proof_invalid")
        self.assertEqual(self._structural_state(), before)

        with self.assertRaises(AgentCapabilityError) as restarted:
            self.timelines.apply_paired_structural_edit(
                self.project_id,
                body,
                idempotency_key="structural-runtime-epoch-restart",
                expected_database_uuid=self.repository.database_uuid,
                paired_capability_id=structural_capability,
                runtime_authority_epoch="epoch_after_structural_restart",
                presented_secret_sha256=proof_secret_sha256(SECRET),
            )
        self.assertEqual(restarted.exception.code, "agent_pairing_expired")
        self.assertEqual(self._structural_state(), before)
        self._assert_cold_structural_state(before)

        revoke_presentation = self.client.get(
            (
                f"/v1/main/agent/capabilities/{structural_capability}"
                "/revoke/presentation"
            ),
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(
            revoke_presentation.status_code,
            200,
            revoke_presentation.json,
        )
        revoked = self.client.post(
            f"/v1/main/agent/capabilities/{structural_capability}/revoke",
            json={
                "presentation": revoke_presentation.json["presentation"],
                "presentation_sha256": revoke_presentation.json[
                    "presentation_sha256"
                ],
                "native_gesture_nonce": "native_structural_revoke_lifecycle",
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoked.status_code, 200, revoked.json)
        self.assertEqual(revoked.json["status"], "revoked")
        after_revoke = self._structural_state()
        revoked_nonce = self._nonce(
            structural_capability,
            action="timeline.apply_structural_edit",
        )
        self.assertEqual(revoked_nonce.status_code, 403, revoked_nonce.json)
        self.assertEqual(revoked_nonce.json["code"], "agent_pairing_revoked")
        with self.assertRaises(AgentCapabilityError) as revoked_direct:
            self.timelines.apply_paired_structural_edit(
                self.project_id,
                body,
                idempotency_key="structural-revoked-direct",
                expected_database_uuid=self.repository.database_uuid,
                paired_capability_id=structural_capability,
                runtime_authority_epoch=self.epoch,
                presented_secret_sha256=proof_secret_sha256(SECRET),
            )
        self.assertEqual(revoked_direct.exception.code, "agent_pairing_revoked")
        self.assertEqual(self._structural_state(), after_revoke)
        self._assert_cold_structural_state(after_revoke)

        expired_secret = "e5" * 32
        pairing_id = "pair_expired_structural"
        capability_id = "cap_expired_structural"
        request_sha256 = "a" * 64
        presentation = self.repository.build_agent_pairing_presentation(
            pairing_id=pairing_id,
            runtime_authority_epoch=self.epoch,
            project_id=self.project_id,
            paired_subject_id="expired_structural_subject",
            claimed_client_label="Expired structural editor",
            actions=["timeline.apply_structural_edit"],
            ttl_seconds=60,
            max_operations=2,
            pairing_request_sha256=request_sha256,
        )
        issued_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.repository.issue_agent_project_capability(
            capability_id=capability_id,
            pairing_id=pairing_id,
            runtime_authority_epoch=self.epoch,
            project_id=self.project_id,
            paired_subject_id="expired_structural_subject",
            claimed_client_label="Expired structural editor",
            actions=["timeline.apply_structural_edit"],
            secret_sha256=proof_secret_sha256(expired_secret),
            ttl_seconds=60,
            max_operations=2,
            pairing_request_sha256=request_sha256,
            presentation=presentation["presentation"],
            presentation_sha256=str(presentation["presentation_sha256"]),
            native_gesture_nonce="native_expired_structural",
            issued_at=issued_at,
        )
        expired_before = self._structural_state()
        with self.assertRaises(AgentCapabilityError) as expired:
            self.timelines.apply_paired_structural_edit(
                self.project_id,
                body,
                idempotency_key="structural-expired-direct",
                expected_database_uuid=self.repository.database_uuid,
                paired_capability_id=capability_id,
                runtime_authority_epoch=self.epoch,
                presented_secret_sha256=proof_secret_sha256(expired_secret),
            )
        self.assertEqual(expired.exception.code, "agent_pairing_expired")
        self.assertEqual(self._structural_state(), expired_before)
        self._assert_cold_structural_state(expired_before)

    def test_structural_transport_substitution_and_nonce_denials_are_zero_write(
        self,
    ) -> None:
        capability_id = self._pair(
            actions=["timeline.apply_edit", "timeline.apply_structural_edit"],
            max_operations=8,
        )
        body = self._body()
        cases = (
            (
                "body",
                {
                    "proof_body": {
                        **body,
                        "structural_edit": {
                            "op": "delete_clip",
                            "clip_id": self.clip_ids[1],
                        },
                    }
                },
                401,
                "agent_pairing_proof_invalid",
            ),
            (
                "path",
                {
                    "signed_path": (
                        f"/v1/agent/creative/projects/{self.project_id}"
                        "/timeline/edit"
                    )
                },
                401,
                "agent_pairing_proof_invalid",
            ),
            (
                "nonce-action",
                {"nonce_action": "timeline.apply_edit"},
                403,
                "agent_operation_nonce_invalid",
            ),
        )
        for label, overrides, status, code in cases:
            with self.subTest(denial=label):
                before = self._structural_state()
                response = self._agent_request(
                    capability_id,
                    body,
                    key=f"structural-transport-{label}",
                    **overrides,
                )
                self.assertEqual(response.status_code, status, response.json)
                self.assertEqual(response.json["code"], code)
                self.assertEqual(self._structural_state(), before)
                self._assert_cold_structural_state(before)

    def test_structural_nonce_is_one_shot_after_exact_success(self) -> None:
        capability_id = self._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=4,
        )
        body = self._body()
        key = "structural-one-shot-nonce"
        path = (
            f"/v1/agent/creative/projects/{self.project_id}"
            "/timeline/structural-edit"
        )
        nonce_response = self._nonce(
            capability_id,
            action="timeline.apply_structural_edit",
        )
        self.assertEqual(nonce_response.status_code, 201, nonce_response.json)
        nonce = str(nonce_response.json["nonce"])
        proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest(),
            idempotency_key=key,
        )
        headers = {
            AGENT_CAPABILITY_HEADER: capability_id,
            AGENT_NONCE_HEADER: nonce,
            AGENT_PROOF_HEADER: proof,
            "Idempotency-Key": key,
        }
        first = self.client.post(path, json=body, headers=headers)
        self.assertEqual(first.status_code, 201, first.json)
        committed = self._structural_state()
        replayed_nonce = self.client.post(path, json=body, headers=headers)
        self.assertEqual(replayed_nonce.status_code, 403, replayed_nonce.json)
        self.assertEqual(
            replayed_nonce.json["code"],
            "agent_operation_nonce_invalid",
        )
        self.assertEqual(self._structural_state(), committed)
        self._assert_cold_structural_state(committed)

    def test_service_delete_replays_exact_echo_and_preserves_sources(self) -> None:
        body = self._body()
        with closing(self.repository._connect()) as connection:
            source_count = int(
                connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0]
            )

        first = self.timelines.apply_structural_edit(
            self.project_id,
            body,
            idempotency_key="structural-service-delete",
            expected_database_uuid=self.repository.database_uuid,
        )
        replay = self.timelines.apply_structural_edit(
            self.project_id,
            body,
            idempotency_key="structural-service-delete",
            expected_database_uuid=self.repository.database_uuid,
        )
        reopened = self.timelines.read(self.project_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(
            first.response["command_type"],
            "timeline.apply_structural_edit",
        )
        self.assertEqual(
            first.response["result"],
            {
                "kind": "revision_created",
                "result_head": reopened["head"],
                "structural_edit": body["structural_edit"],
            },
        )
        self.assertEqual(reopened["timeline"]["schema_version"], "2")
        self.assertEqual(reopened["head"]["revision"], 2)
        self.assertEqual(len(reopened["timeline"]["tracks"][0]["clips"]), 1)
        self.assertEqual(len(reopened["source_bindings"]), 1)
        self.assertNotIn(self.clip_ids[0], str(reopened["source_bindings"]))
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM asset_sources"
                    ).fetchone()[0]
                ),
                source_count,
            )

        with self.assertRaises(TimelineLoweringServiceError) as stale:
            self.timelines.apply_structural_edit(
                self.project_id,
                body,
                idempotency_key="structural-service-stale",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(stale.exception.code, "timeline_head_conflict")

    def test_v2_split_restore_and_historical_tamper_fail_closed(self) -> None:
        project_id, payload, revision_one = self._build_video_project()
        timeline_one = revision_one["timeline"]
        head_one = revision_one["head"]
        assert isinstance(timeline_one, dict) and isinstance(head_one, dict)
        video = next(
            clip
            for clip in timeline_one["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        )
        split_ms = (int(video["source_in_ms"]) + int(video["source_out_ms"])) // 2
        self.timelines.apply_structural_edit(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": head_one,
                "structural_edit": {
                    "op": "split_clip",
                    "clip_id": video["clip_id"],
                    "source_split_ms": split_ms,
                },
            },
            idempotency_key="structural-v2-split",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_two = self.timelines.read(project_id)
        frozen_two = copy.deepcopy(revision_two)
        timeline_two = revision_two["timeline"]
        head_two = revision_two["head"]
        assert isinstance(timeline_two, dict) and isinstance(head_two, dict)
        video_children = [
            clip
            for clip in timeline_two["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        ]
        self.assertEqual(timeline_two["schema_version"], "2")
        self.assertEqual(len(video_children), 2)
        self.assertEqual(
            [(clip["source_in_ms"], clip["source_out_ms"]) for clip in video_children],
            [
                (video["source_in_ms"], split_ms),
                (split_ms, video["source_out_ms"]),
            ],
        )
        self.assertEqual(
            len({clip["assignment_id"] for clip in video_children}),
            1,
        )
        self.assertEqual(
            len({clip["evidence_ref"] for clip in video_children}),
            1,
        )

        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": head_two,
                "edit": {
                    "op": "move_clip",
                    "clip_id": video_children[1]["clip_id"],
                    "to_index": 0,
                },
            },
            idempotency_key="structural-v2-advance",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_three = self.timelines.read(project_id)
        head_three = revision_three["head"]
        timeline_three = revision_three["timeline"]
        assert isinstance(head_three, dict) and isinstance(timeline_three, dict)
        restore_from = {
            field: value
            for field, value in revision_two["selected_revision"].items()
            if field != "is_head"
        }
        restored = self.timelines.restore_revision(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": head_three,
                "restore_from": restore_from,
            },
            idempotency_key="structural-v2-restore-two-as-four",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_four = self.timelines.read(project_id)
        expected_timeline = copy.deepcopy(timeline_two)
        expected_timeline["revision"] = 4
        expected_timeline["parent"] = {
            "revision": 3,
            "content_sha256": canonical_timeline_content_sha256(timeline_three),
        }
        self.assertEqual(restored.response["result"]["restore_from"], restore_from)
        self.assertEqual(restored.response["result"]["result_head"], revision_four["head"])
        self.assertEqual(revision_four["timeline"], expected_timeline)
        self.assertEqual(
            revision_four["source_bindings"],
            revision_two["source_bindings"],
        )
        self.assertEqual(revision_four["timeline"]["schema_version"], "2")
        historical_two = self.timelines.read(project_id, revision=2)
        self.assertEqual(historical_two["timeline"], frozen_two["timeline"])
        self.assertEqual(
            historical_two["source_bindings"],
            frozen_two["source_bindings"],
        )

        head_four = revision_four["head"]
        assert isinstance(head_four, dict)
        deleted_child_id = str(video_children[0]["clip_id"])
        self.timelines.apply_structural_edit(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": head_four,
                "structural_edit": {
                    "op": "delete_clip",
                    "clip_id": deleted_child_id,
                },
            },
            idempotency_key="structural-v2-delete-five",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_five = self.timelines.read(project_id)
        frozen_five = copy.deepcopy(revision_five)
        timeline_five = revision_five["timeline"]
        head_five = revision_five["head"]
        assert isinstance(timeline_five, dict) and isinstance(head_five, dict)
        self.assertEqual(timeline_five["schema_version"], "2")
        self.assertNotIn(
            deleted_child_id,
            {
                clip["clip_id"]
                for clip in timeline_five["tracks"][0]["clips"]
            },
        )
        self.assertEqual(len(revision_five["source_bindings"]), 2)
        remaining_video = next(
            clip
            for clip in timeline_five["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        )
        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": head_five,
                "edit": {
                    "op": "move_clip",
                    "clip_id": remaining_video["clip_id"],
                    "to_index": 0,
                },
            },
            idempotency_key="structural-v2-delete-advance-six",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_six = self.timelines.read(project_id)
        timeline_six = revision_six["timeline"]
        head_six = revision_six["head"]
        assert isinstance(timeline_six, dict) and isinstance(head_six, dict)
        restore_delete_from = {
            field: value
            for field, value in revision_five["selected_revision"].items()
            if field != "is_head"
        }
        restored_delete = self.timelines.restore_revision(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": head_six,
                "restore_from": restore_delete_from,
            },
            idempotency_key="structural-v2-restore-delete-five-as-seven",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_seven = self.timelines.read(project_id)
        expected_delete_restore = copy.deepcopy(timeline_five)
        expected_delete_restore["revision"] = 7
        expected_delete_restore["parent"] = {
            "revision": 6,
            "content_sha256": canonical_timeline_content_sha256(timeline_six),
        }
        self.assertEqual(
            restored_delete.response["result"]["restore_from"],
            restore_delete_from,
        )
        self.assertEqual(
            restored_delete.response["result"]["result_head"],
            revision_seven["head"],
        )
        self.assertEqual(revision_seven["timeline"], expected_delete_restore)
        self.assertEqual(
            revision_seven["source_bindings"],
            revision_five["source_bindings"],
        )
        self.assertEqual(revision_seven["timeline"]["schema_version"], "2")
        historical_five = self.timelines.read(project_id, revision=5)
        self.assertEqual(historical_five["timeline"], frozen_five["timeline"])
        self.assertEqual(
            historical_five["source_bindings"],
            frozen_five["source_bindings"],
        )
        restored_state = self._structural_state(project_id)
        self._assert_cold_structural_state(
            restored_state,
            project_id=project_id,
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT timeline_json,source_bindings_json "
                "FROM canonical_timeline_revisions "
                "WHERE project_id=? AND revision=2",
                (project_id,),
            ).fetchone()
        assert row is not None
        original_timeline_json = str(row[0])
        original_bindings_json = str(row[1])
        child_id = str(video_children[0]["clip_id"])

        tampered_timeline_interval = json.loads(original_timeline_json)
        interval_clip = next(
            clip
            for clip in tampered_timeline_interval["tracks"][0]["clips"]
            if clip["clip_id"] == child_id
        )
        interval_clip["source_out_ms"] = int(interval_clip["source_out_ms"]) - 1

        tampered_binding_interval = json.loads(original_bindings_json)
        interval_binding = next(
            binding
            for binding in tampered_binding_interval
            if binding["clip_id"] == child_id
        )
        interval_binding["source_out_ms"] = (
            int(interval_binding["source_out_ms"]) - 1
        )

        tampered_proof = json.loads(original_timeline_json)
        proof_clip = next(
            clip
            for clip in tampered_proof["tracks"][0]["clips"]
            if clip["clip_id"] == child_id
        )
        proof_clip["source_binding_sha256"] = "f" * 64

        tamper_cases = (
            (
                "child-source-interval",
                "timeline_json",
                canonical_json(tampered_timeline_interval),
                original_timeline_json,
            ),
            (
                "child-source-binding",
                "source_bindings_json",
                canonical_json(tampered_binding_interval),
                original_bindings_json,
            ),
            (
                "child-proof-identity",
                "timeline_json",
                canonical_json(tampered_proof),
                original_timeline_json,
            ),
        )
        for label, column, tampered_value, original_value in tamper_cases:
            with self.subTest(tamper=label):
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    trigger_sql = connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE "
                        "name='trg_canonical_timeline_revisions_no_update'"
                    ).fetchone()[0]
                    connection.execute(
                        "DROP TRIGGER trg_canonical_timeline_revisions_no_update"
                    )
                    connection.execute(
                        f"UPDATE canonical_timeline_revisions SET {column}=? "
                        "WHERE project_id=? AND revision=2",
                        (tampered_value, project_id),
                    )
                    connection.execute(trigger_sql)
                cold = MediaRepository(self.db_path)
                try:
                    with self.assertRaises(CanonicalTimelineIntegrityError):
                        cold.get_canonical_timeline_head(project_id)
                finally:
                    cold.close()
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    trigger_sql = connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE "
                        "name='trg_canonical_timeline_revisions_no_update'"
                    ).fetchone()[0]
                    connection.execute(
                        "DROP TRIGGER trg_canonical_timeline_revisions_no_update"
                    )
                    connection.execute(
                        f"UPDATE canonical_timeline_revisions SET {column}=? "
                        "WHERE project_id=? AND revision=2",
                        (original_value, project_id),
                    )
                    connection.execute(trigger_sql)
                self._assert_cold_structural_state(
                    restored_state,
                    project_id=project_id,
                )

        with closing(sqlite3.connect(self.db_path)) as connection:
            operation_row = connection.execute(
                "SELECT id,result_json,request_sha256 "
                "FROM canonical_timeline_operations "
                "WHERE project_id=? AND sequence=5",
                (project_id,),
            ).fetchone()
        assert operation_row is not None
        delete_operation_id = str(operation_row[0])
        original_delete_result_json = str(operation_row[1])
        original_delete_request_sha256 = str(operation_row[2])
        tampered_delete_result = json.loads(original_delete_result_json)
        alternate_delete_target = str(
            next(
                clip["clip_id"]
                for clip in revision_four["timeline"]["tracks"][0]["clips"]
                if clip["clip_id"] != deleted_child_id
            )
        )
        tampered_delete_result["structural_edit"]["clip_id"] = (
            alternate_delete_target
        )
        tampered_delete_result_json = canonical_json(tampered_delete_result)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            operation_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_operations_no_update'"
            ).fetchone()[0]
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_operations_no_update"
            )
            connection.execute(
                "UPDATE canonical_timeline_operations SET result_json=? WHERE id=?",
                (tampered_delete_result_json, delete_operation_id),
            )
            connection.execute(operation_trigger_sql)
        cold = MediaRepository(self.db_path)
        try:
            with patch(
                "core.timeline_structural_edit_contract."
                "require_exact_timeline_structural_edit_replay",
                wraps=require_exact_timeline_structural_edit_replay,
            ) as replay_verifier:
                with self.assertRaises(CanonicalTimelineIntegrityError):
                    cold.get_canonical_timeline_head(project_id)
            tampered_replay = next(
                call
                for call in replay_verifier.call_args_list
                if call.kwargs["edit"].get("op") == "delete_clip"
            )
            self.assertEqual(
                tampered_replay.kwargs["edit"]["clip_id"],
                alternate_delete_target,
            )
        finally:
            cold.close()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            operation_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_operations_no_update'"
            ).fetchone()[0]
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_operations_no_update"
            )
            connection.execute(
                "UPDATE canonical_timeline_operations SET result_json=? WHERE id=?",
                (original_delete_result_json, delete_operation_id),
            )
            connection.execute(operation_trigger_sql)
        self._assert_cold_structural_state(
            restored_state,
            project_id=project_id,
        )

        tampered_delete_request = {
            "object": "memolens.timeline_apply_structural_edit_command",
            "schema_version": "1",
            "project_id": project_id,
            "expected_database_uuid": self.repository.database_uuid,
            "expected_blueprint": payload["expected_blueprint"],
            "expected_coverage": payload["expected_coverage"],
            "expected_timeline_head": revision_four["head"],
            "structural_edit": tampered_delete_result["structural_edit"],
        }
        tampered_delete_request_sha256 = hashlib.sha256(
            canonical_json(tampered_delete_request).encode("utf-8")
        ).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            receipt_row = connection.execute(
                "SELECT * FROM canonical_timeline_receipts WHERE operation_id=?",
                (delete_operation_id,),
            ).fetchone()
        assert receipt_row is not None
        original_receipt = dict(receipt_row)
        tampered_response = json.loads(str(receipt_row["response_json"]))
        tampered_response["result"] = tampered_delete_result
        tampered_response_json = canonical_json(tampered_response)
        tampered_response_sha256 = hashlib.sha256(
            tampered_response_json.encode("utf-8")
        ).hexdigest()
        tampered_receipt_material = MediaRepository._canonical_timeline_receipt_material(
            database_uuid=receipt_row["database_uuid"],
            authenticated_principal=receipt_row["authenticated_principal"],
            project_id=receipt_row["project_id"],
            command_type=receipt_row["command_type"],
            command_version=receipt_row["command_version"],
            idempotency_key=receipt_row["idempotency_key"],
            request_sha256=tampered_delete_request_sha256,
            operation_id=receipt_row["operation_id"],
            response_status=receipt_row["response_status"],
            response_sha256=tampered_response_sha256,
            resource_id=receipt_row["resource_id"],
            created_at=receipt_row["created_at"],
        )
        tampered_receipt_sha256 = content_sha256(tampered_receipt_material)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            operation_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_operations_no_update'"
            ).fetchone()[0]
            receipt_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_receipts_no_update'"
            ).fetchone()[0]
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_operations_no_update"
            )
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_receipts_no_update"
            )
            connection.execute(
                "UPDATE canonical_timeline_operations "
                "SET result_json=?,request_sha256=? WHERE id=?",
                (
                    tampered_delete_result_json,
                    tampered_delete_request_sha256,
                    delete_operation_id,
                ),
            )
            connection.execute(
                "UPDATE canonical_timeline_receipts "
                "SET request_sha256=?,response_json=?,response_sha256=?,"
                "receipt_sha256=? WHERE operation_id=?",
                (
                    tampered_delete_request_sha256,
                    tampered_response_json,
                    tampered_response_sha256,
                    tampered_receipt_sha256,
                    delete_operation_id,
                ),
            )
            connection.execute(operation_trigger_sql)
            connection.execute(receipt_trigger_sql)
        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalTimelineIntegrityError,
                "canonical_timeline_structural_edit_replay_invalid",
            ):
                cold.get_canonical_timeline_head(project_id)
        finally:
            cold.close()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            operation_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_operations_no_update'"
            ).fetchone()[0]
            receipt_trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_receipts_no_update'"
            ).fetchone()[0]
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_operations_no_update"
            )
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_receipts_no_update"
            )
            connection.execute(
                "UPDATE canonical_timeline_operations "
                "SET result_json=?,request_sha256=? WHERE id=?",
                (
                    original_delete_result_json,
                    original_delete_request_sha256,
                    delete_operation_id,
                ),
            )
            connection.execute(
                "UPDATE canonical_timeline_receipts "
                "SET request_sha256=?,response_json=?,response_sha256=?,"
                "receipt_sha256=? WHERE operation_id=?",
                (
                    original_receipt["request_sha256"],
                    original_receipt["response_json"],
                    original_receipt["response_sha256"],
                    original_receipt["receipt_sha256"],
                    delete_operation_id,
                ),
            )
            connection.execute(operation_trigger_sql)
            connection.execute(receipt_trigger_sql)
        self._assert_cold_structural_state(
            restored_state,
            project_id=project_id,
        )

    def test_service_fault_after_revision_rolls_back_all_structural_rows(self) -> None:
        body = self._body()
        with closing(self.repository._connect()) as connection:
            before = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (self.project_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_timeline_operations",
                    "canonical_timeline_revisions",
                    "canonical_timeline_receipts",
                )
            )
        with patch.object(
            self.repository,
            "append_canonical_timeline_operation",
            side_effect=RuntimeError("injected-structural-after-revision"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected-structural-after-revision",
            ):
                self.timelines.apply_structural_edit(
                    self.project_id,
                    body,
                    idempotency_key="structural-service-fault",
                    expected_database_uuid=self.repository.database_uuid,
                )
        self.assertEqual(self.timelines.read(self.project_id)["head"], self.timeline_head)
        with closing(self.repository._connect()) as connection:
            after = tuple(
                int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (self.project_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "canonical_timeline_operations",
                    "canonical_timeline_revisions",
                    "canonical_timeline_receipts",
                )
            )
        self.assertEqual(after, before)

    def test_desktop_route_requires_closed_structural_body_and_replays(self) -> None:
        body = self._body()
        path = f"/v1/creative/projects/{self.project_id}/timeline/structural-edit"
        query = {
            "db_path": str(self.db_path),
            "expected_database_uuid": self.repository.database_uuid,
        }
        headers = {
            DESKTOP_TOKEN_HEADER: self.desktop_token,
            "Idempotency-Key": "structural-desktop-delete",
        }
        first = self.client.post(path, query_string=query, json=body, headers=headers)
        replay = self.client.post(path, query_string=query, json=body, headers=headers)

        self.assertEqual(first.status_code, 201, first.json)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201, replay.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json, first.json)
        self.assertEqual(first.json["result"]["kind"], "revision_created")
        self.assertEqual(
            first.json["result"]["structural_edit"],
            body["structural_edit"],
        )

        wrong_field = self.client.post(
            path,
            query_string=query,
            json={
                **body,
                "edit": body["structural_edit"],
            },
            headers={
                **headers,
                "Idempotency-Key": "structural-desktop-wrong-field",
            },
        )
        self.assertEqual(wrong_field.status_code, 400)
        self.assertEqual(
            wrong_field.json["code"],
            "invalid_timeline_structural_edit_command",
        )

    def test_paired_route_requires_distinct_structural_authority(self) -> None:
        ordinary_secret = "d4" * 32
        ordinary_capability = self._pair(
            actions=["timeline.apply_edit"],
            secret=ordinary_secret,
        )
        denied_nonce = self._nonce(
            ordinary_capability,
            action="timeline.apply_structural_edit",
            secret=ordinary_secret,
        )
        self.assertEqual(denied_nonce.status_code, 403, denied_nonce.json)
        self.assertEqual(denied_nonce.json["code"], "agent_pairing_scope_denied")

        structural_capability = self._pair(
            actions=["timeline.apply_structural_edit"],
            max_operations=1,
        )
        body = self._body()
        first = self._agent_save(
            structural_capability,
            body,
            key="structural-agent-delete",
        )
        replay = self._agent_save(
            structural_capability,
            body,
            key="structural-agent-delete",
        )

        self.assertEqual(first.status_code, 201, first.json)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201, replay.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json, first.json)
        self.assertEqual(
            first.json["command_type"],
            "timeline.apply_structural_edit",
        )
        self.assertEqual(
            first.json["result"]["structural_edit"],
            body["structural_edit"],
        )
        capability = self.repository.get_agent_project_capability(
            structural_capability,
            runtime_authority_epoch=self.epoch,
        )
        assert capability is not None
        self.assertEqual(capability["operations_used"], 1)
        self.assertEqual(capability["status"], "exhausted")
        committed = self._structural_state()
        exhausted = self._agent_save(
            structural_capability,
            body,
            key="structural-agent-delete-after-exhaustion",
        )
        self.assertEqual(exhausted.status_code, 403, exhausted.json)
        self.assertEqual(exhausted.json["code"], "agent_pairing_expired")
        self.assertEqual(self._structural_state(), committed)
        self._assert_cold_structural_state(committed)


if __name__ == "__main__":
    unittest.main()
