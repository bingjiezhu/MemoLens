from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

from backend.src.media.provider_egress import (
    PROVIDER_ADAPTER_ID,
    PROVIDER_ADAPTER_VERSION,
    ProviderEgressServiceError,
    ProviderImageVisionEgress,
    ProviderSendPermitAuthority,
    ProviderTransportKnownFailure,
    ProviderTransportOutcomeUnknown,
    ProviderTransportResponse,
)
from core.db import ImageIndexRepository
from core.image_analysis_job_contract import canonical_sha256 as job_sha256
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.media_db import MediaRepository
from core.provider_egress_contract import (
    canonical_sha256,
    seal_provider_egress_grant_intent,
)
from core.provider_egress_persistence import ProviderEgressPersistenceError


RUNTIME_GENERATION = f"runtime_generation_{'a' * 64}"
ANALYSIS_PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}
PROVIDER_PROFILE = {
    "provider": "controlled-test-provider",
    "model": "controlled-vision-v1",
    "endpoint_url": "https://provider.invalid/v1/chat/completions",
    "profile_id": "canonical-image-provider-v1",
    "profile_version": "2026-08-29",
    "profile_sha256": hashlib.sha256(b"canonical-image-provider-v1").hexdigest(),
}


class _ImageVisionFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-provider-service-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "private-library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.root_id = str(self.repository.library_roots()[0]["id"])
        self.image_path = self.library / "private-source.png"
        Image.new("RGB", (40, 24), (31, 87, 151)).save(self.image_path)
        source_bytes = self.image_path.read_bytes()
        source_stat = self.image_path.stat()
        self.input_sha256 = hashlib.sha256(source_bytes).hexdigest()
        asset = self.repository.upsert_asset_source(
            root_id=self.root_id,
            relative_path=self.image_path.name,
            filename=self.image_path.name,
            kind="image",
            sha256=self.input_sha256,
            mime_type="image/png",
            file_size=len(source_bytes),
            mtime_ns=source_stat.st_mtime_ns,
            source_file_id=str(source_stat.st_ino),
        )
        self.asset_id = str(asset["id"])
        self.source_id = str(asset["asset_source_id"])
        self.repository.update_image_probe(self.asset_id, width=40, height=24)
        database_stat = os.stat(self.db_path, follow_symlinks=False)
        self.database_identity = (int(database_stat.st_dev), int(database_stat.st_ino))
        job = self.repository.enqueue_image_analysis(
            runtime_generation=RUNTIME_GENERATION,
            database_file_identity=self.database_identity,
            asset_id=self.asset_id,
            source_id=self.source_id,
            analysis_profile=ANALYSIS_PROFILE,
            expected_head=None,
            enqueue_scope="controlled-provider-service-test",
            idempotency_key=f"provider-service-{uuid.uuid4().hex}",
        )
        self.job_id = str(job["id"])
        binding = self.repository.claim_image_analysis_attempt(
            self.job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
        )
        sequence = int(binding["heartbeat_sequence"])
        sequence = self.repository.checkpoint_image_analysis_stage(
            self.job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            expected_sequence=sequence,
            stage="source_admission",
            outcome="succeeded",
            outcome_sha256=job_sha256(
                {"source_binding_sha256": binding["source_binding_sha256"]}
            ),
            reason_code=None,
            artifact_digests=(
                {
                    "stage": "source_admission",
                    "name": "file_identity",
                    "sha256": binding["file_identity_sha256"],
                },
            ),
        )
        sequence = self.repository.checkpoint_image_analysis_stage(
            self.job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            expected_sequence=sequence,
            stage="metadata",
            outcome="succeeded",
            outcome_sha256=hashlib.sha256(b"controlled-metadata").hexdigest(),
            reason_code=None,
        )
        self.repository.checkpoint_image_analysis_stage(
            self.job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            expected_sequence=sequence,
            stage="geocode",
            outcome="disabled",
            outcome_sha256=hashlib.sha256(b"controlled-geocode-disabled").hexdigest(),
            reason_code="network_not_authorized",
        )
        current = self.repository.get_image_analysis_job_binding(self.job_id)
        assert current is not None
        assert current["job_stage"] == "vision"
        assert current["attempt_state"] == "claimed"
        self.binding = current

    def close(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def open_source(self):  # type: ignore[no-untyped-def]
        return self.repository._open_admitted_image_source(
            source_id=self.source_id,
            expected_asset_id=self.asset_id,
            expected_input_sha256=self.input_sha256,
        )

    def provider_rows(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            grants = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM provider_egress_grants ORDER BY issued_at,id"
                ).fetchall()
            ]
            manifests = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM provider_egress_manifests ORDER BY created_at,id"
                ).fetchall()
            ]
        finally:
            connection.close()
        return grants, manifests

    def database_dump(self) -> str:
        connection = sqlite3.connect(self.db_path)
        try:
            return "\n".join(connection.iterdump())
        finally:
            connection.close()


class _NativeSealedGrantResolver:
    def __init__(self, *, mutate=None) -> None:  # type: ignore[no-untyped-def]
        self.mutate = mutate
        self.requests: list[dict[str, object]] = []
        self.envelope: dict[str, object] | None = None
        self.token = f"native-process-private-grant-{uuid.uuid4().hex}"

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def __call__(self, request):  # type: ignore[no-untyped-def]
        exact_request = deepcopy(dict(request))
        self.requests.append(exact_request)
        if self.envelope is not None:
            return deepcopy(self.envelope)
        now = datetime.now(timezone.utc)
        grant = {
            "object": "memolens.provider_egress_grant_intent",
            "schema_version": "1",
            "grant_id": f"pgrant_{uuid.uuid4().hex}",
            **{
                key: deepcopy(value)
                for key, value in exact_request.items()
                if key not in {"object", "schema_version"}
            },
            "issued_at": self._iso(now - timedelta(seconds=1)),
            "expires_at": self._iso(now + timedelta(minutes=5)),
            "native_confirmation": {
                "confirmation_id": f"confirm_{uuid.uuid4().hex}",
                "operation": "provider_egress.image_vision",
            },
        }
        if self.mutate is not None:
            self.mutate(grant)
        confirmation = grant["native_confirmation"]
        assert isinstance(confirmation, dict)
        confirmation["operation_sha256"] = canonical_sha256(grant)
        sealed = seal_provider_egress_grant_intent(grant)
        self.envelope = {"grant_intent": sealed, "grant_token": self.token}
        return deepcopy(self.envelope)


class _RecordingTransport:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode
        self.preflight_error: Exception | None = None
        self.preflight_hook = None
        self.preflight_payloads: list[bytes] = []
        self.sent_payloads: list[bytes] = []

    def preflight(self, **kwargs: object) -> None:
        wire_payload = kwargs["wire_payload"]
        assert isinstance(wire_payload, bytes)
        self.preflight_payloads.append(wire_payload)
        if self.preflight_hook is not None:
            self.preflight_hook()
        if self.preflight_error is not None:
            raise self.preflight_error

    def send(self, **kwargs: object) -> ProviderTransportResponse:
        wire_payload = kwargs["wire_payload"]
        assert isinstance(wire_payload, bytes)
        self.sent_payloads.append(wire_payload)
        if self.mode == "known_failure":
            raise ProviderTransportKnownFailure(
                "provider_http_rejected",
                bytes_sent=len(wire_payload),
            )
        if self.mode == "timeout":
            raise ProviderTransportOutcomeUnknown("transport_timeout")
        if self.mode == "generic":
            raise RuntimeError("connection-result-cannot-be-proven")
        response = ProviderTransportResponse(
            payload={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "description": "  A   blue wall and lamp.  ",
                                    "tags": ["wall", " lamp ", "wall", ""],
                                    "location_hint": "  Golden   Gate  ",
                                }
                            )
                        }
                    }
                ]
            },
            bytes_sent=len(wire_payload),
        )
        if self.mode == "mismatched_accounting":
            return ProviderTransportResponse(
                payload=response.payload,
                bytes_sent=len(wire_payload) - 1,
            )
        return response


class ProviderImageVisionEgressServiceTests(unittest.TestCase):
    @staticmethod
    def _service(
        fixture: _ImageVisionFixture,
        resolver: _NativeSealedGrantResolver,
        transport: _RecordingTransport,
        *,
        credential="controlled-provider-credential",
        profile: dict[str, object] | None = None,
        permits: ProviderSendPermitAuthority | None = None,
    ) -> ProviderImageVisionEgress:
        return ProviderImageVisionEgress(
            fixture.repository,
            profile=profile or PROVIDER_PROFILE,
            grant_resolver=resolver,
            credential_resolver=lambda: credential,
            transport=transport,
            permit_authority=permits,
        )

    @staticmethod
    def _run(
        fixture: _ImageVisionFixture,
        service: ProviderImageVisionEgress,
        *,
        binding: dict[str, object] | None = None,
        expected_attempt: int = 1,
    ) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
        with fixture.open_source() as source:
            return service.run(
                source,
                binding or fixture.binding,
                RUNTIME_GENERATION,
                expected_attempt,
            )

    def test_default_without_resolver_does_zero_decode_and_zero_database_work(self) -> None:
        class _NoDatabase:
            def __getattr__(self, name: str) -> object:
                raise AssertionError(f"unexpected database access: {name}")

        service = ProviderImageVisionEgress(_NoDatabase())
        with patch(
            "backend.src.media.provider_egress._bounded_jpeg_derivative",
            side_effect=AssertionError("unexpected image decode"),
        ):
            stage, artifacts = service.run(  # type: ignore[arg-type]
                object(),
                {},
                RUNTIME_GENERATION,
                1,
            )
        self.assertEqual(stage["status"], "disabled")
        self.assertEqual(stage["reason_code"], "provider_not_authorized")
        self.assertEqual(artifacts, ())

    def test_success_sends_exact_manifest_bytes_and_normalizes_with_provenance(self) -> None:
        fixture = _ImageVisionFixture()
        try:
            resolver = _NativeSealedGrantResolver()
            transport = _RecordingTransport()
            permits = ProviderSendPermitAuthority()
            service = self._service(
                fixture,
                resolver,
                transport,
                permits=permits,
            )

            stage, artifacts = self._run(fixture, service)

            self.assertEqual(stage["status"], "succeeded")
            self.assertEqual(
                stage["output"],
                {
                    "description": "A blue wall and lamp.",
                    "tags": ["lamp", "wall"],
                    "location_hint": "Golden Gate",
                },
            )
            self.assertEqual(
                stage["provenance"],
                {
                    "producer_id": "memolens.provider.controlled-test-provider",
                    "producer_version": PROVIDER_ADAPTER_VERSION,
                    "model_id": "controlled-vision-v1",
                    "model_version": "2026-08-29",
                    "rule_id": PROVIDER_ADAPTER_ID,
                    "rule_version": PROVIDER_ADAPTER_VERSION,
                },
            )
            self.assertEqual(len(artifacts), 1)
            artifact = artifacts[0]
            self.assertEqual(artifact["bytes"], json.dumps(
                stage["output"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"))
            self.assertEqual(
                artifact["artifact_sha256"],
                hashlib.sha256(artifact["bytes"]).hexdigest(),  # type: ignore[arg-type]
            )
            self.assertEqual(len(transport.preflight_payloads), 1)
            self.assertEqual(transport.preflight_payloads, transport.sent_payloads)
            wire_payload = transport.sent_payloads[0]

            grants, manifests = fixture.provider_rows()
            self.assertEqual(len(grants), 1)
            self.assertEqual(len(manifests), 1)
            self.assertEqual(grants[0]["status"], "consumed")
            self.assertEqual(manifests[0]["status"], "sent")
            plan = json.loads(str(manifests[0]["manifest_plan_json"]))
            self.assertEqual(plan["wire_payload_bytes"], len(wire_payload))
            self.assertEqual(
                plan["wire_payload_sha256"],
                hashlib.sha256(wire_payload).hexdigest(),
            )
            self.assertEqual(manifests[0]["bytes_sent"], len(wire_payload))
            self.assertEqual(permits.active_count(), 0)

            dump = fixture.database_dump()
            self.assertNotIn(resolver.token, dump)
            self.assertNotIn(str(fixture.image_path), dump)
            self.assertNotIn(wire_payload.decode("utf-8"), dump)
            self.assertIn(hashlib.sha256(resolver.token.encode()).hexdigest(), dump)
            provider_storage = json.dumps(
                {"grants": grants, "manifests": manifests},
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(fixture.image_path.name, provider_storage)
            self.assertNotIn(str(fixture.library), provider_storage)
            self.assertNotIn(resolver.token, provider_storage)
            self.assertNotIn(wire_payload.decode("utf-8"), provider_storage)
        finally:
            fixture.close()

    def test_offline_and_missing_credential_stop_before_decode_grant_and_send(self) -> None:
        fixture = _ImageVisionFixture()
        try:
            for case in ("offline", "credential"):
                with self.subTest(case=case):
                    resolver = _NativeSealedGrantResolver()
                    transport = _RecordingTransport()
                    profile = dict(PROVIDER_PROFILE)
                    credential: str | None = "controlled-provider-credential"
                    environment = {}
                    if case == "offline":
                        environment = {"MEMOLENS_NETWORK_PROFILE": "offline"}
                    else:
                        credential = None
                    service = self._service(
                        fixture,
                        resolver,
                        transport,
                        credential=credential,
                        profile=profile,
                    )
                    with patch.dict(os.environ, environment, clear=False), patch(
                        "backend.src.media.provider_egress._bounded_jpeg_derivative",
                        side_effect=AssertionError("unexpected image decode"),
                    ):
                        stage, artifacts = self._run(fixture, service)
                    self.assertEqual(stage["status"], "disabled")
                    self.assertEqual(
                        stage["reason_code"],
                        "network_not_authorized"
                        if case == "offline"
                        else "provider_unavailable",
                    )
                    self.assertEqual(artifacts, ())
                    self.assertEqual(resolver.requests, [])
                    self.assertEqual(transport.preflight_payloads, [])
                    self.assertEqual(transport.sent_payloads, [])
            self.assertEqual(fixture.provider_rows(), ([], []))
        finally:
            fixture.close()

    def test_preflight_failure_releases_grant_and_exact_retry_can_send(self) -> None:
        fixture = _ImageVisionFixture()
        try:
            resolver = _NativeSealedGrantResolver()
            transport = _RecordingTransport()
            transport.preflight_error = ProviderEgressServiceError(
                "controlled_preflight_failure"
            )
            permits = ProviderSendPermitAuthority()
            service = self._service(
                fixture,
                resolver,
                transport,
                permits=permits,
            )

            with self.assertRaisesRegex(
                ProviderEgressServiceError,
                "controlled_preflight_failure",
            ):
                self._run(fixture, service)
            grants, manifests = fixture.provider_rows()
            self.assertEqual([row["status"] for row in grants], ["active"])
            self.assertEqual(
                [row["status"] for row in manifests],
                ["failed_before_send"],
            )
            self.assertEqual(transport.sent_payloads, [])
            self.assertEqual(permits.active_count(), 0)

            transport.preflight_error = None
            stage, _ = self._run(fixture, service)
            self.assertEqual(stage["status"], "succeeded")
            grants, manifests = fixture.provider_rows()
            self.assertEqual([row["status"] for row in grants], ["consumed"])
            self.assertEqual(
                sorted(row["status"] for row in manifests),
                ["failed_before_send", "sent"],
            )
            self.assertEqual(len(transport.sent_payloads), 1)
        finally:
            fixture.close()

    def test_source_database_job_profile_and_attempt_tamper_never_send(self) -> None:
        cases = (
            (
                "source",
                lambda binding: binding.__setitem__("input_asset_sha256", "f" * 64),
                1,
                ImageAnalysisPersistenceError,
                "image_source_changed",
            ),
            (
                "database",
                lambda binding: binding.__setitem__(
                    "database_inode", int(binding["database_inode"]) + 1
                ),
                1,
                ProviderEgressPersistenceError,
                "provider_egress_database_scope_changed",
            ),
            (
                "job",
                lambda binding: binding.__setitem__("job_id", f"job_{'f' * 32}"),
                1,
                ProviderEgressPersistenceError,
                "provider_egress_job_missing",
            ),
            (
                "profile",
                lambda binding: binding.__setitem__(
                    "analysis_profile_id", "tampered-analysis-profile"
                ),
                1,
                ProviderEgressPersistenceError,
                "provider_egress_job_scope_changed",
            ),
            (
                "attempt",
                lambda binding: None,
                2,
                ProviderEgressPersistenceError,
                "provider_egress_job_scope_changed",
            ),
        )
        for name, mutate, attempt, error_type, error_code in cases:
            with self.subTest(name=name):
                fixture = _ImageVisionFixture()
                try:
                    binding = deepcopy(fixture.binding)
                    mutate(binding)
                    resolver = _NativeSealedGrantResolver()
                    transport = _RecordingTransport()
                    service = self._service(fixture, resolver, transport)
                    with self.assertRaisesRegex(error_type, error_code):
                        self._run(
                            fixture,
                            service,
                            binding=binding,
                            expected_attempt=attempt,
                        )
                    self.assertEqual(transport.preflight_payloads, [])
                    self.assertEqual(transport.sent_payloads, [])
                    _, manifests = fixture.provider_rows()
                    self.assertEqual(manifests, [])
                finally:
                    fixture.close()

    def test_native_grant_provider_profile_tamper_is_rejected_before_database(self) -> None:
        fixture = _ImageVisionFixture()
        try:
            def mutate(grant: dict[str, object]) -> None:
                provider_profile = grant["provider_profile"]
                assert isinstance(provider_profile, dict)
                provider_profile["profile_version"] = "tampered"

            resolver = _NativeSealedGrantResolver(mutate=mutate)
            transport = _RecordingTransport()
            service = self._service(fixture, resolver, transport)

            with self.assertRaisesRegex(
                ProviderEgressServiceError,
                "provider_grant_scope_mismatch",
            ):
                self._run(fixture, service)
            self.assertEqual(transport.preflight_payloads, [])
            self.assertEqual(transport.sent_payloads, [])
            self.assertEqual(fixture.provider_rows(), ([], []))
        finally:
            fixture.close()

    def test_http_known_failure_is_failed_after_send_and_not_retryable(self) -> None:
        fixture = _ImageVisionFixture()
        try:
            resolver = _NativeSealedGrantResolver()
            transport = _RecordingTransport("known_failure")
            service = self._service(fixture, resolver, transport)

            stage, artifacts = self._run(fixture, service)
            self.assertEqual(stage["status"], "failed")
            self.assertEqual(stage["reason_code"], "provider_http_rejected")
            self.assertEqual(artifacts, ())
            grants, manifests = fixture.provider_rows()
            self.assertEqual([row["status"] for row in grants], ["consumed"])
            self.assertEqual(
                [row["status"] for row in manifests],
                ["failed_after_send"],
            )
            plan = json.loads(str(manifests[0]["manifest_plan_json"]))
            self.assertEqual(manifests[0]["bytes_sent"], plan["wire_payload_bytes"])

            with self.assertRaisesRegex(
                ProviderEgressPersistenceError,
                "provider_egress_grant_unavailable",
            ):
                self._run(fixture, service)
            self.assertEqual(len(transport.sent_payloads), 1)
            self.assertEqual(len(fixture.provider_rows()[1]), 1)
        finally:
            fixture.close()

    def test_timeout_generic_and_byte_mismatch_are_ambiguous_and_not_retryable(self) -> None:
        for mode, reason in (
            ("timeout", "transport_outcome_unknown"),
            ("generic", "transport_outcome_unknown"),
            ("mismatched_accounting", "transport_accounting_invalid"),
        ):
            with self.subTest(mode=mode):
                fixture = _ImageVisionFixture()
                try:
                    resolver = _NativeSealedGrantResolver()
                    transport = _RecordingTransport(mode)
                    service = self._service(fixture, resolver, transport)

                    stage, artifacts = self._run(fixture, service)
                    self.assertEqual(stage["status"], "failed")
                    self.assertEqual(stage["reason_code"], reason)
                    self.assertEqual(artifacts, ())
                    grants, manifests = fixture.provider_rows()
                    self.assertEqual([row["status"] for row in grants], ["ambiguous"])
                    self.assertEqual(
                        [row["status"] for row in manifests],
                        ["ambiguous"],
                    )
                    self.assertIsNone(manifests[0]["bytes_sent"])

                    with self.assertRaisesRegex(
                        ProviderEgressPersistenceError,
                        "provider_egress_grant_unavailable",
                    ):
                        self._run(fixture, service)
                    self.assertEqual(len(transport.sent_payloads), 1)
                    self.assertEqual(len(fixture.provider_rows()[1]), 1)
                finally:
                    fixture.close()


if __name__ == "__main__":
    unittest.main()
