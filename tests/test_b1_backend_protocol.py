from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    AgentPairingBroker,
    AgentProtocolError,
    agent_request_proof,
    agent_status_proof,
    proof_secret_sha256,
    sha256_text,
)
from core.config import Settings
from core.media_db import canonical_json


SECRET = "ab" * 32
DATABASE_UUID = "database_b1_backend"
EPOCH = "epoch_b1_backend"
PROJECT_ID = "project_b1_backend"
HEAD = {
    "revision": 1,
    "content_sha256": "1" * 64,
    "semantic_sha256": "2" * 64,
    "operation_id": "op_blueprint_1",
}


def pairing_payload() -> dict[str, object]:
    return {
        "object": "memolens.agent_pairing_request",
        "schema_version": "1",
        "project_id": PROJECT_ID,
        "observed_head": {
            "revision": HEAD["revision"],
            "content_sha256": HEAD["content_sha256"],
        },
        "subject_id": "codex_session_1",
        "claimed_client_label": "Codex local session",
        "proof_secret": SECRET,
        "actions": ["blueprint.commit_proposal", "blueprint.restore_revision"],
        "ttl_seconds": 900,
        "max_operations": 20,
    }


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def _reserve_or_error(
    broker: AgentPairingBroker,
    payload: dict[str, object],
) -> str:
    try:
        return broker.reserve_pairing_intake(payload)
    except AgentProtocolError as exc:
        return exc.code


class AgentPairingBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock()
        self.broker = AgentPairingBroker(EPOCH, now=self.clock)

    def create(self):
        response = self.broker.create_pairing(
            pairing_payload(),
            database_uuid=DATABASE_UUID,
            observed_head=HEAD,
        )
        return response["pairing_id"], response

    def test_frozen_cli_hmac_vectors_use_decoded_32_byte_key(self) -> None:
        self.assertEqual(
            agent_status_proof(
                SECRET,
                kind="pairing",
                identifier="pair_vector_1",
            ),
            "5ff0c5bc1238f57f336255a1a6ec396c9bc2e3ba46798b366d9c06a066a9548c",
        )
        self.assertEqual(
            agent_request_proof(
                SECRET,
                capability_id="cap_vector_1",
                nonce="nonce_vector_1",
                method="POST",
                canonical_path=(
                    "/v1/agent/creative/projects/project_vector/blueprint/commit"
                ),
                database_uuid="database_vector",
                project_id="project_vector",
                body_sha256="5041bf1f713df204784353e82f6a4a535931cb64f1f4b4a5aeaffcb720918b22",
                idempotency_key="idem-vector-1",
            ),
            "bea7c62111d95847399159d3ce51c5223a9ca501495b7e7aebbf5fbe8e02ec42",
        )

    def test_intake_is_closed_server_bound_and_secret_free(self) -> None:
        pairing_id, response = self.create()
        self.assertTrue(str(pairing_id).startswith("pair_"))
        self.assertEqual(response["database_uuid"], DATABASE_UUID)
        self.assertEqual(response["status"], "pending")
        self.assertRegex(str(response["display_code"]), r"^[0-9A-F]{4}-[0-9A-F]{4}$")
        serialized = canonical_json(response)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(proof_secret_sha256(SECRET), serialized)
        self.assertNotIn(EPOCH, serialized)

        stale = pairing_payload()
        stale["observed_head"] = {"revision": 1, "content_sha256": "f" * 64}
        with self.assertRaises(AgentProtocolError) as captured:
            self.broker.create_pairing(stale, database_uuid=DATABASE_UUID, observed_head=HEAD)
        self.assertEqual(captured.exception.code, "blueprint_head_conflict")

        injected = {**pairing_payload(), "database_uuid": "attacker_selected"}
        with self.assertRaises(AgentProtocolError) as captured:
            self.broker.create_pairing(injected, database_uuid=DATABASE_UUID, observed_head=HEAD)
        self.assertEqual(captured.exception.code, "invalid_agent_pairing_request")

    def test_status_requires_secret_possession_without_resending_secret(self) -> None:
        pairing_id, _response = self.create()
        with self.assertRaises(AgentProtocolError) as captured:
            self.broker.agent_pairing_status(str(pairing_id), "0" * 64)
        self.assertEqual(captured.exception.code, "agent_pairing_proof_invalid")

        proof = agent_status_proof(SECRET, kind="pairing", identifier=str(pairing_id))
        response = self.broker.agent_pairing_status(str(pairing_id), proof)
        self.assertEqual(response["status"], "pending")
        self.assertNotIn(SECRET, canonical_json(response))

    def test_nonce_and_exact_request_proof_are_single_use(self) -> None:
        pairing_id, _response = self.create()
        capability_id = "cap_backend_protocol_1"
        self.broker.activate_capability(
            str(pairing_id),
            capability_id=capability_id,
            issued_at=self.clock(),
            expires_at=self.clock() + timedelta(minutes=15),
            max_operations=20,
        )
        action = "blueprint.commit_proposal"
        nonce_response = self.broker.issue_nonce(
            capability_id,
            action=action,
            database_uuid=DATABASE_UUID,
            status_proof=agent_status_proof(
                SECRET,
                kind="nonce",
                identifier=f"{capability_id}:{action}",
            ),
        )
        body = {"expected_head": {"revision": 1, "content_sha256": "1" * 64}}
        body_sha256 = sha256_text(canonical_json(body))
        path = f"/v1/agent/creative/projects/{PROJECT_ID}/blueprint/commit"
        nonce = str(nonce_response["nonce"])
        proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=DATABASE_UUID,
            project_id=PROJECT_ID,
            body_sha256=body_sha256,
            idempotency_key="agent-command-1",
        )
        with self.assertRaises(AgentProtocolError) as captured:
            self.broker.claim_command_proof(
                capability_id=capability_id,
                nonce=nonce,
                proof=proof,
                method="POST",
                canonical_path=path,
                database_uuid=DATABASE_UUID,
                project_id=PROJECT_ID,
                action=action,
                body_sha256="f" * 64,
                idempotency_key="agent-command-1",
            )
        self.assertEqual(captured.exception.code, "agent_pairing_proof_invalid")

        capability = self.broker.claim_command_proof(
            capability_id=capability_id,
            nonce=nonce,
            proof=proof,
            method="POST",
            canonical_path=path,
            database_uuid=DATABASE_UUID,
            project_id=PROJECT_ID,
            action=action,
            body_sha256=body_sha256,
            idempotency_key="agent-command-1",
        )
        self.assertEqual(capability.proof_secret_sha256, proof_secret_sha256(SECRET))

        with self.assertRaises(AgentProtocolError) as captured:
            self.broker.claim_command_proof(
                capability_id=capability_id,
                nonce=nonce,
                proof=proof,
                method="POST",
                canonical_path=path,
                database_uuid=DATABASE_UUID,
                project_id=PROJECT_ID,
                action=action,
                body_sha256=body_sha256,
                idempotency_key="agent-command-1",
            )
        self.assertEqual(captured.exception.code, "agent_operation_nonce_invalid")

    def test_expiry_and_bounds_fail_closed(self) -> None:
        limited = AgentPairingBroker(EPOCH, now=self.clock, max_pending=1)
        limited.create_pairing(pairing_payload(), database_uuid=DATABASE_UUID, observed_head=HEAD)
        second = pairing_payload()
        second["subject_id"] = "codex_session_2"
        with self.assertRaises(AgentProtocolError) as captured:
            limited.create_pairing(second, database_uuid=DATABASE_UUID, observed_head=HEAD)
        self.assertEqual(captured.exception.code, "agent_pairing_rate_limited")

        pairing_id, _response = self.create()
        self.clock.value += timedelta(minutes=6)
        with self.assertRaises(AgentProtocolError) as captured:
            self.broker.agent_pairing_status(
                str(pairing_id),
                agent_status_proof(SECRET, kind="pairing", identifier=str(pairing_id)),
            )
        self.assertEqual(captured.exception.code, "agent_pairing_expired")

    def test_pairing_rate_and_queue_reservations_are_global_and_concurrency_safe(self) -> None:
        globally_limited = AgentPairingBroker(
            EPOCH,
            now=self.clock,
            max_pending=32,
            preauth_rate_limit=100,
            intake_rate_limit=12,
        )
        for index in range(12):
            payload = pairing_payload()
            payload["subject_id"] = f"random_subject_{index}"
            globally_limited.create_pairing(
                payload,
                database_uuid=DATABASE_UUID,
                observed_head=HEAD,
            )
        thirteenth = pairing_payload()
        thirteenth["subject_id"] = "random_subject_12"
        with self.assertRaises(AgentProtocolError) as captured:
            globally_limited.create_pairing(
                thirteenth,
                database_uuid=DATABASE_UUID,
                observed_head=HEAD,
            )
        self.assertEqual(captured.exception.code, "agent_pairing_rate_limited")

        queue_limited = AgentPairingBroker(
            EPOCH,
            now=self.clock,
            max_pending=3,
            preauth_rate_limit=100,
            intake_rate_limit=100,
        )
        with ThreadPoolExecutor(max_workers=12) as executor:
            attempts = list(
                executor.map(
                    lambda _index: _reserve_or_error(
                        queue_limited,
                        pairing_payload(),
                    ),
                    range(12),
                )
            )
        reservations = [value for value in attempts if value.startswith("intake_")]
        self.assertEqual(len(reservations), 3)
        self.assertEqual(attempts.count("agent_pairing_rate_limited"), 9)
        for reservation in reservations:
            queue_limited.release_pairing_intake(reservation)


def semantic(goal: str = "make a creator story") -> dict[str, object]:
    return {
        "intent": {"goal": goal, "stance": "honest", "audience": "creators", "platform": "short-video"},
        "script": {"blocks": [{"block_id": "opening", "text": "opening"}]},
        "direction": {
            "theme": "memory",
            "narrative_arc": "found",
            "emotion": "warm",
            "tone": "natural",
            "pace": "measured",
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
    shutdown_runtime_extensions(app.extensions)


class B1BackendApiBoundaryTests(unittest.TestCase):
    """Focused route-level authority tests; Core state-machine tests live separately."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-b1-backend-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "index.db"
        self.desktop_token = "desktop-token-is-not-main"
        self.main_token = "main-authority-token-is-independent"
        self.epoch = "epoch_backend_integration_1"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(Path(__file__).resolve().parents[1] / "config.yaml"),
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
        self.addCleanup(self.environment.stop)
        self.app = create_app(Settings.from_env())
        self.addCleanup(shutdown_app, self.app)
        self.client = self.app.test_client()
        self.repository = self.app.extensions["media_repository"]
        project = self.repository.create_project(
            "B1 backend",
            {"goal": "legacy", "candidate_refs": []},
            {"created_by": "fixture"},
        )
        self.project_id = str(project["id"])
        brief = self.repository.get_brief(self.project_id, 1)
        assert brief is not None
        initial = {
            "expected_head": None,
            "initial_legacy_brief": {
                "revision": 1,
                "content_sha256": brief["content_sha256"],
            },
            "source_candidate_sha256": "a" * 64,
            "semantic": semantic(),
        }
        response = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/commit",
            query_string={"db_path": str(self.db_path)},
            json=initial,
            headers={
                DESKTOP_TOKEN_HEADER: self.desktop_token,
                "Idempotency-Key": "b1-bootstrap",
            },
        )
        self.assertEqual(response.status_code, 201, response.json)
        self.head = response.json["result"]["result_head"]

    def pair_body(self) -> dict[str, object]:
        value = pairing_payload()
        value["project_id"] = self.project_id
        value["observed_head"] = {
            "revision": self.head["revision"],
            "content_sha256": self.head["content_sha256"],
        }
        return value

    def test_pairing_global_limits_and_queue_reservation_precede_repository_reads(self) -> None:
        original = self.repository.get_creative_project_in_transaction
        with patch.object(
            self.repository,
            "get_creative_project_in_transaction",
            wraps=original,
        ) as project_read:
            for index in range(12):
                body = self.pair_body()
                body["subject_id"] = f"untrusted_subject_{index}"
                accepted = self.client.post("/v1/agent/pairings", json=body)
                self.assertEqual(accepted.status_code, 201, accepted.json)
            calls_at_limit = project_read.call_count
            thirteenth = self.pair_body()
            thirteenth["subject_id"] = "untrusted_subject_12"
            rejected = self.client.post("/v1/agent/pairings", json=thirteenth)
            self.assertEqual(rejected.status_code, 429, rejected.json)
            self.assertEqual(project_read.call_count, calls_at_limit)

        queue_broker = AgentPairingBroker(
            self.epoch,
            max_pending=1,
            preauth_rate_limit=100,
            intake_rate_limit=100,
        )
        self.app.extensions["agent_pairing_broker"] = queue_broker
        with patch.object(
            self.repository,
            "get_creative_project_in_transaction",
            wraps=original,
        ) as project_read:
            accepted = self.client.post("/v1/agent/pairings", json=self.pair_body())
            self.assertEqual(accepted.status_code, 201, accepted.json)
            calls_when_full = project_read.call_count
            second = self.pair_body()
            second["subject_id"] = "queue_bypass_subject"
            rejected = self.client.post("/v1/agent/pairings", json=second)
            self.assertEqual(rejected.status_code, 429, rejected.json)
            self.assertEqual(project_read.call_count, calls_when_full)

    def test_pairing_preauth_counts_invalid_requests_and_valid_requests_once(self) -> None:
        original = self.repository.get_creative_project_in_transaction
        invalid_broker = AgentPairingBroker(
            self.epoch,
            max_pending=10,
            preauth_rate_limit=2,
            intake_rate_limit=10,
        )
        self.app.extensions["agent_pairing_broker"] = invalid_broker
        with patch.object(
            self.repository,
            "get_creative_project_in_transaction",
            wraps=original,
        ) as project_read:
            malformed = self.client.post(
                "/v1/agent/pairings",
                data=b'{"project_id":',
                content_type="application/json",
            )
            self.assertEqual(malformed.status_code, 400, malformed.json)
            invalid_shape = self.client.post(
                "/v1/agent/pairings",
                json={"project_id": self.project_id},
            )
            self.assertEqual(invalid_shape.status_code, 400, invalid_shape.json)
            calls_at_limit = project_read.call_count
            rate_limited = self.client.post(
                "/v1/agent/pairings",
                json=self.pair_body(),
            )
            self.assertEqual(rate_limited.status_code, 429, rate_limited.json)
            self.assertEqual(project_read.call_count, calls_at_limit)

        valid_broker = AgentPairingBroker(
            self.epoch,
            max_pending=10,
            preauth_rate_limit=2,
            intake_rate_limit=10,
        )
        self.app.extensions["agent_pairing_broker"] = valid_broker
        with patch.object(
            self.repository,
            "get_creative_project_in_transaction",
            wraps=original,
        ) as project_read:
            for index in range(2):
                body = self.pair_body()
                body["subject_id"] = f"single_count_subject_{index}"
                accepted = self.client.post("/v1/agent/pairings", json=body)
                self.assertEqual(accepted.status_code, 201, accepted.json)
            calls_at_limit = project_read.call_count
            third = self.pair_body()
            third["subject_id"] = "single_count_subject_2"
            rate_limited = self.client.post("/v1/agent/pairings", json=third)
            self.assertEqual(rate_limited.status_code, 429, rate_limited.json)
            self.assertEqual(project_read.call_count, calls_at_limit)

    def test_global_preauth_budget_stops_bad_nonce_proofs_before_repository(self) -> None:
        broker = AgentPairingBroker(
            self.epoch,
            preauth_rate_limit=3,
            intake_rate_limit=100,
        )
        pending = broker.create_pairing(
            pairing_payload(),
            database_uuid=self.repository.database_uuid,
            observed_head=HEAD,
        )
        capability_id = "cap_nonce_flood"
        issued_at = datetime.now(timezone.utc)
        broker.activate_capability(
            str(pending["pairing_id"]),
            capability_id=capability_id,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=15),
            max_operations=20,
        )
        self.app.extensions["agent_pairing_broker"] = broker
        nonce_body = {
            "object": "memolens.agent_operation_nonce_request",
            "schema_version": "1",
            "action": "blueprint.commit_proposal",
        }
        with patch.object(
            self.repository,
            "get_agent_project_capability",
            return_value={"status": "active"},
        ) as capability_read:
            for _index in range(2):
                rejected = self.client.post(
                    f"/v1/agent/capabilities/{capability_id}/nonce",
                    json=nonce_body,
                    headers={AGENT_STATUS_PROOF_HEADER: "0" * 64},
                )
                self.assertEqual(rejected.status_code, 401, rejected.json)
            calls_at_limit = capability_read.call_count
            rate_limited = self.client.post(
                f"/v1/agent/capabilities/{capability_id}/nonce",
                json=nonce_body,
                headers={AGENT_STATUS_PROOF_HEADER: "0" * 64},
            )
            self.assertEqual(rate_limited.status_code, 429, rate_limited.json)
            self.assertEqual(capability_read.call_count, calls_at_limit)

    def test_desktop_token_origin_and_injected_database_cannot_use_main_or_agent_write(self) -> None:
        main_path = "/v1/main/agent/pairings"
        desktop_only = self.client.get(
            main_path,
            headers={DESKTOP_TOKEN_HEADER: self.desktop_token},
        )
        self.assertEqual(desktop_only.status_code, 403)
        self.assertEqual(desktop_only.json["code"], "native_confirmation_required")

        browser_main = self.client.get(
            main_path,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Origin": "http://127.0.0.1:5173",
            },
        )
        self.assertEqual(browser_main.status_code, 403)
        preflight = self.client.options(
            "/v1/agent/pairings",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        allowed_headers = preflight.headers.get("Access-Control-Allow-Headers", "").casefold()
        self.assertNotIn("x-memolens-main-authority", allowed_headers)
        self.assertNotIn("x-memolens-agent-proof", allowed_headers)
        self.assertNotIn("x-memolens-agent-status-proof", allowed_headers)

        browser_pair = self.client.post(
            "/v1/agent/pairings",
            json=self.pair_body(),
            headers={"Origin": "http://127.0.0.1:5173"},
        )
        self.assertEqual(browser_pair.status_code, 401)

        injected = {**self.pair_body(), "database_uuid": self.repository.database_uuid}
        rejected = self.client.post("/v1/agent/pairings", json=injected)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json["code"], "invalid_agent_pairing_request")

    def test_pairing_approval_nonce_and_agent_commit_form_one_safe_vertical_path(self) -> None:
        paired = self.client.post("/v1/agent/pairings", json=self.pair_body())
        self.assertEqual(paired.status_code, 201, paired.json)
        pairing_id = paired.json["pairing_id"]
        self.assertEqual(paired.json["database_uuid"], self.repository.database_uuid)
        self.assertNotIn(SECRET, json.dumps(paired.json))

        presentation = self.client.get(
            f"/v1/main/agent/pairings/{pairing_id}/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        approved = self.client.post(
            f"/v1/main/agent/pairings/{pairing_id}/approve",
            json={
                "presentation_sha256": presentation.json["presentation_sha256"],
                "native_gesture_nonce": "native_pairing_click_0001",
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(approved.status_code, 201, approved.json)
        capability_id = approved.json["capability_id"]
        serialized = json.dumps(approved.json, sort_keys=True)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn(proof_secret_sha256(SECRET), serialized)
        with closing(self.repository._connect()) as connection:
            database_dump = "\n".join(connection.iterdump())
        self.assertNotIn(SECRET, database_dump)
        self.assertIn(proof_secret_sha256(SECRET), database_dump)

        actionable = self.client.get(
            "/v1/main/agent/capabilities",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(actionable.status_code, 200, actionable.json)
        self.assertEqual(
            [row["capability_id"] for row in actionable.json["capabilities"]],
            [capability_id],
        )

        action = "blueprint.commit_proposal"
        nonce = self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    SECRET,
                    kind="nonce",
                    identifier=f"{capability_id}:{action}",
                )
            },
        )
        self.assertEqual(nonce.status_code, 201, nonce.json)

        changed = semantic("make a sharper creator story")
        body = {
            "expected_head": {
                "revision": self.head["revision"],
                "content_sha256": self.head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "b" * 64,
            "semantic": changed,
        }
        path = f"/v1/agent/creative/projects/{self.project_id}/blueprint/commit"
        key = "paired-agent-commit-1"
        proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=nonce.json["nonce"],
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(canonical_json(body).encode()).hexdigest(),
            idempotency_key=key,
        )
        committed = self.client.post(
            path,
            json=body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: nonce.json["nonce"],
                AGENT_PROOF_HEADER: proof,
                "Idempotency-Key": key,
            },
        )
        self.assertEqual(committed.status_code, 201, committed.json)
        self.assertFalse(committed.json["authority"]["verified"])
        with closing(self.repository._connect()) as connection:
            operation = connection.execute(
                "SELECT actor_json,origin_json FROM creative_blueprint_operations "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            actor = json.loads(operation["actor_json"])
            origin = json.loads(operation["origin_json"])
            self.assertEqual(actor["kind"], "paired_agent_command")
            self.assertEqual(actor["capability_id"], capability_id)
            self.assertTrue(actor["identity_verified"])
            self.assertFalse(actor["vendor_identity_verified"])
            self.assertFalse(actor["user_authority_verified"])
            self.assertEqual(origin, {"principal": "paired_agent", "surface": "agent_cli"})
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_command_receipts "
                    "WHERE receipt_schema_version='1' "
                    "AND resource_type='creative_blueprint'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_capability_events "
                    "WHERE capability_id=? AND event_type='used'",
                    (capability_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_command_receipts "
                    "WHERE operation_id=(SELECT id FROM creative_blueprint_operations "
                    "ORDER BY sequence DESC LIMIT 1)"
                ).fetchone()[0],
                0,
            )

        replayed_nonce = self.client.post(
            path,
            json=body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: nonce.json["nonce"],
                AGENT_PROOF_HEADER: proof,
                "Idempotency-Key": key,
            },
        )
        self.assertEqual(replayed_nonce.status_code, 403)
        self.assertEqual(replayed_nonce.json["code"], "agent_operation_nonce_invalid")

        fresh_nonce = self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    SECRET,
                    kind="nonce",
                    identifier=f"{capability_id}:{action}",
                )
            },
        )
        self.assertEqual(fresh_nonce.status_code, 201, fresh_nonce.json)
        replay_proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=fresh_nonce.json["nonce"],
            method="POST",
            canonical_path=path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(canonical_json(body).encode()).hexdigest(),
            idempotency_key=key,
        )
        durable_replay = self.client.post(
            path,
            json=body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: fresh_nonce.json["nonce"],
                AGENT_PROOF_HEADER: replay_proof,
                "Idempotency-Key": key,
            },
        )
        self.assertEqual(durable_replay.status_code, 201, durable_replay.json)
        self.assertEqual(durable_replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(durable_replay.json, committed.json)
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_capability_events "
                    "WHERE capability_id=? AND event_type='used'",
                    (capability_id,),
                ).fetchone()[0],
                1,
            )

        restore_action = "blueprint.restore_revision"
        restore_nonce = self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": restore_action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    SECRET,
                    kind="nonce",
                    identifier=f"{capability_id}:{restore_action}",
                )
            },
        )
        self.assertEqual(restore_nonce.status_code, 201, restore_nonce.json)
        restore_body = {
            "expected_head": {
                "revision": committed.json["result"]["result_head"]["revision"],
                "content_sha256": committed.json["result"]["result_head"][
                    "content_sha256"
                ],
            },
            "restore_from": {
                "revision": self.head["revision"],
                "content_sha256": self.head["content_sha256"],
            },
        }
        restore_path = f"/v1/agent/creative/projects/{self.project_id}/blueprint/restore"
        restore_key = "paired-agent-restore-1"
        restore_proof = agent_request_proof(
            SECRET,
            capability_id=capability_id,
            nonce=restore_nonce.json["nonce"],
            method="POST",
            canonical_path=restore_path,
            database_uuid=self.repository.database_uuid,
            project_id=self.project_id,
            body_sha256=hashlib.sha256(
                canonical_json(restore_body).encode()
            ).hexdigest(),
            idempotency_key=restore_key,
        )
        restored = self.client.post(
            restore_path,
            json=restore_body,
            headers={
                AGENT_CAPABILITY_HEADER: capability_id,
                AGENT_NONCE_HEADER: restore_nonce.json["nonce"],
                AGENT_PROOF_HEADER: restore_proof,
                "Idempotency-Key": restore_key,
            },
        )
        self.assertEqual(restored.status_code, 201, restored.json)
        self.assertEqual(
            restored.json["result"]["restore_from"]["revision"],
            self.head["revision"],
        )
        with closing(self.repository._connect()) as connection:
            operation = connection.execute(
                "SELECT actor_json,origin_json FROM creative_blueprint_operations "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(json.loads(operation["actor_json"])["capability_id"], capability_id)
            self.assertEqual(
                json.loads(operation["origin_json"]),
                {"principal": "paired_agent", "surface": "agent_cli"},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM agent_project_capability_events "
                    "WHERE capability_id=? AND event_type='used'",
                    (capability_id,),
                ).fetchone()[0],
                2,
            )

        revoke_presentation = self.client.get(
            f"/v1/main/agent/capabilities/{capability_id}/revoke/presentation",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoke_presentation.status_code, 200, revoke_presentation.json)
        revoked = self.client.post(
            f"/v1/main/agent/capabilities/{capability_id}/revoke",
            json={
                "presentation": revoke_presentation.json["presentation"],
                "presentation_sha256": revoke_presentation.json["presentation_sha256"],
                "native_gesture_nonce": "native_capability_revoke_0001",
            },
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoked.status_code, 200, revoked.json)
        self.assertEqual(revoked.json["status"], "revoked")
        actionable_after_revoke = self.client.get(
            "/v1/main/agent/capabilities",
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(
            actionable_after_revoke.json["capabilities"],
            [],
        )

        status = self.client.get(
            f"/v1/agent/pairings/{pairing_id}",
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    SECRET,
                    kind="pairing",
                    identifier=pairing_id,
                )
            },
        )
        self.assertEqual(status.status_code, 200, status.json)
        self.assertEqual(status.json["status"], "revoked")
        nonce_after_revoke = self.client.post(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            json={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                AGENT_STATUS_PROOF_HEADER: agent_status_proof(
                    SECRET,
                    kind="nonce",
                    identifier=f"{capability_id}:{action}",
                )
            },
        )
        self.assertEqual(nonce_after_revoke.status_code, 403)
        self.assertEqual(nonce_after_revoke.json["code"], "agent_pairing_revoked")

    def test_near_limit_blueprint_round_trips_through_authority_http_envelope(self) -> None:
        large_semantic = semantic("near the frozen semantic byte limit")
        blocks = [
            {"block_id": f"large_{index}", "text": "x" * 10_000}
            for index in range(52)
        ]
        blocks[0]["text"] = "x" * 12_000
        blocks[1]["text"] = "x" * 9_800
        large_semantic["script"] = {"blocks": blocks}
        commit_body = {
            "expected_head": {
                "revision": self.head["revision"],
                "content_sha256": self.head["content_sha256"],
            },
            "initial_legacy_brief": None,
            "source_candidate_sha256": "d" * 64,
            "semantic": large_semantic,
        }
        self.assertLessEqual(len(canonical_json(large_semantic).encode()), 524_288)
        self.assertGreater(len(canonical_json(commit_body).encode()), 524_288)

        committed = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/commit",
            query_string={"db_path": str(self.db_path)},
            json=commit_body,
            headers={
                DESKTOP_TOKEN_HEADER: self.desktop_token,
                "Idempotency-Key": "near-limit-blueprint",
            },
        )
        self.assertEqual(committed.status_code, 201, committed.json)

        presentation_path = (
            f"/v1/main/creative/projects/{self.project_id}/blueprint/authority/presentation"
        )
        presentation = self.client.post(
            presentation_path,
            json={"operation": "confirm", "decision_units": ["script"]},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        presentation_bytes = len(
            canonical_json(presentation.json["presentation"]).encode()
        )
        self.assertGreater(presentation_bytes, 512_000)
        self.assertLessEqual(presentation_bytes, 1_048_576)

        confirmed = self.client.post(
            f"/v1/main/creative/projects/{self.project_id}/blueprint/authority/confirm",
            json={
                "presentation": presentation.json["presentation"],
                "presentation_sha256": presentation.json["presentation_sha256"],
                "native_gesture_nonce": "near_limit_native_confirmation",
            },
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "near-limit-authority-confirm",
            },
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.json)
        self.assertTrue(
            confirmed.json["authority_projection"]["decision_units"]["script"]["confirmed"]
        )

    def test_main_exact_decision_presentation_confirm_stale_and_revoke(self) -> None:
        presentation_path = (
            f"/v1/main/creative/projects/{self.project_id}/blueprint/authority/presentation"
        )
        confirmation_path = (
            f"/v1/main/creative/projects/{self.project_id}/blueprint/authority/confirm"
        )
        selected_units = ["intent_stance", "script", "creative_direction"]
        request_body = {"operation": "confirm", "decision_units": selected_units}

        for forbidden_headers, expected_code in (
            ({DESKTOP_TOKEN_HEADER: self.desktop_token}, "native_confirmation_required"),
            (
                {
                    MAIN_AUTHORITY_HEADER: self.main_token,
                    "Origin": "http://127.0.0.1:5173",
                },
                "native_confirmation_required",
            ),
            (
                {
                    AGENT_CAPABILITY_HEADER: "cap_forged",
                    AGENT_PROOF_HEADER: "f" * 64,
                },
                "agent_confirmation_forbidden",
            ),
        ):
            rejected = self.client.post(
                presentation_path,
                json=request_body,
                headers=forbidden_headers,
            )
            self.assertEqual(rejected.status_code, 403, rejected.json)
            self.assertEqual(rejected.json["code"], expected_code)
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_decision_authority_events"
                ).fetchone()[0],
                0,
            )

        presentation = self.client.post(
            presentation_path,
            json=request_body,
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(presentation.status_code, 200, presentation.json)
        self.assertEqual(
            presentation.json["presentation"]["authority_operation"],
            "confirm",
        )
        self.assertEqual(
            [item["name"] for item in presentation.json["presentation"]["decision_units"]],
            selected_units,
        )
        self.assertTrue(
            all(
                set(item) == {"name", "semantic_sha256", "content"}
                for item in presentation.json["presentation"]["decision_units"]
            )
        )
        confirmation_body = {
            "presentation": presentation.json["presentation"],
            "presentation_sha256": presentation.json["presentation_sha256"],
            "native_gesture_nonce": "native_decision_click_0001",
        }

        agent_cannot_confirm = self.client.post(
            confirmation_path,
            json=confirmation_body,
            headers={
                AGENT_CAPABILITY_HEADER: "cap_forged",
                AGENT_PROOF_HEADER: "f" * 64,
                "Idempotency-Key": "forbidden-agent-confirm",
            },
        )
        self.assertEqual(agent_cannot_confirm.status_code, 403)
        self.assertEqual(agent_cannot_confirm.json["code"], "agent_confirmation_forbidden")

        confirmed = self.client.post(
            confirmation_path,
            json=confirmation_body,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "authority-confirm-1",
            },
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.json)
        self.assertEqual(confirmed.headers["Idempotency-Replayed"], "false")
        self.assertEqual(
            confirmed.json["authority_projection"]["confirmed_decision_unit_count"],
            3,
        )
        self.assertEqual(confirmed.json["authority_projection"]["state"], "partially_confirmed")
        self.assertFalse(confirmed.json["creation_authority"]["verified"])

        replay = self.client.post(
            confirmation_path,
            json=confirmation_body,
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "authority-confirm-1",
            },
        )
        self.assertEqual(replay.status_code, 201, replay.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json, confirmed.json)
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_decision_authority_events"
                ).fetchone()[0],
                1,
            )

        stale_presentation = self.client.post(
            presentation_path,
            json={"operation": "confirm", "decision_units": ["output"]},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(stale_presentation.status_code, 200, stale_presentation.json)
        changed = semantic("new direct-parent goal")
        advanced = self.client.post(
            f"/v1/creative/projects/{self.project_id}/blueprint/commit",
            query_string={"db_path": str(self.db_path)},
            json={
                "expected_head": {
                    "revision": self.head["revision"],
                    "content_sha256": self.head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": "c" * 64,
                "semantic": changed,
            },
            headers={
                DESKTOP_TOKEN_HEADER: self.desktop_token,
                "Idempotency-Key": "desktop-advance-after-confirm",
            },
        )
        self.assertEqual(advanced.status_code, 201, advanced.json)
        stale = self.client.post(
            confirmation_path,
            json={
                "presentation": stale_presentation.json["presentation"],
                "presentation_sha256": stale_presentation.json["presentation_sha256"],
                "native_gesture_nonce": "native_stale_click_0001",
            },
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "authority-stale-1",
            },
        )
        self.assertEqual(stale.status_code, 409, stale.json)
        self.assertEqual(stale.json["code"], "blueprint_confirmation_stale")

        revoke_presentation = self.client.post(
            presentation_path,
            json={"operation": "revoke", "decision_units": ["script"]},
            headers={MAIN_AUTHORITY_HEADER: self.main_token},
        )
        self.assertEqual(revoke_presentation.status_code, 200, revoke_presentation.json)
        revoke = self.client.post(
            f"/v1/main/creative/projects/{self.project_id}/blueprint/authority/revoke",
            json={
                "presentation": revoke_presentation.json["presentation"],
                "presentation_sha256": revoke_presentation.json["presentation_sha256"],
                "native_gesture_nonce": "native_revoke_click_0001",
            },
            headers={
                MAIN_AUTHORITY_HEADER: self.main_token,
                "Idempotency-Key": "authority-revoke-1",
            },
        )
        self.assertEqual(revoke.status_code, 201, revoke.json)
        projection = revoke.json["authority_projection"]
        self.assertEqual(projection["confirmed_decision_unit_count"], 2)
        self.assertFalse(projection["decision_units"]["script"]["confirmed"])
        self.assertTrue(projection["decision_units"]["intent_stance"]["confirmed"])

        current = self.client.get(
            f"/v1/creative/projects/{self.project_id}/blueprint"
        )
        self.assertEqual(current.status_code, 200, current.json)
        self.assertFalse(current.json["creation_authority"]["verified"])
        self.assertEqual(
            current.json["authority_projection"]["confirmed_decision_unit_count"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
