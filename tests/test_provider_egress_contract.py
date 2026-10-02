from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from core.provider_egress_contract import (
    PROVIDER_EGRESS_MAX_CANONICAL_BYTES,
    ProviderEgressContractError,
    canonical_json,
    canonical_sha256,
    provider_egress_grant_intent_sha256,
    provider_payload_manifest_plan_sha256,
    require_manifest_matches_grant_intent,
    require_valid_provider_egress_grant_intent,
    require_valid_provider_payload_manifest_plan,
    seal_provider_egress_grant_intent,
    seal_provider_payload_manifest_plan,
    validate_provider_egress_grant_intent,
    validate_provider_payload_manifest_plan,
)


DATABASE_UUID = "11111111-1111-4111-8111-111111111111"
JOB_ID = f"job_{'2' * 32}"
ASSET_ID = f"asset_{'3' * 24}"
SOURCE_ID = f"src_{'4' * 24}"
RUNTIME_GENERATION = f"runtime_generation_{'5' * 64}"
WIRE_PAYLOAD = b'{"image_digest":"exact-wire-body"}'


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def grant_intent() -> dict[str, object]:
    issued = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    value: dict[str, object] = {
        "object": "memolens.provider_egress_grant_intent",
        "schema_version": "1",
        "grant_id": f"pgrant_{'6' * 32}",
        "capability": "image_vision",
        "database_binding": {
            "database_uuid": DATABASE_UUID,
            "database_device": 42,
            "database_inode": 84,
        },
        "job_id": JOB_ID,
        "request_sha256": _sha("request"),
        "asset_id": ASSET_ID,
        "source_id": SOURCE_ID,
        "input_asset_sha256": _sha("asset"),
        "source_binding_sha256": _sha("source-binding"),
        "analysis_profile": {
            "profile_id": "canonical-image-local-v1",
            "profile_version": "1",
            "profile_sha256": _sha("analysis-profile"),
        },
        "provider_profile": {
            "profile_id": "canonical-image-provider-v1",
            "profile_version": "1",
            "profile_sha256": _sha("provider-profile"),
        },
        "provider": "example-provider",
        "model": "example-vision-v1",
        "purpose": "canonical_image_analysis",
        "payload_class": "derived_image_bytes",
        "wire_payload_sha256": hashlib.sha256(WIRE_PAYLOAD).hexdigest(),
        "wire_payload_bytes": len(WIRE_PAYLOAD),
        "adapter_contract": {
            "adapter_id": "memolens.provider.image-vision",
            "adapter_version": "1",
            "contract_sha256": _sha("adapter-contract"),
        },
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + timedelta(minutes=5)),
        "native_confirmation": {
            "confirmation_id": f"confirm_{'7' * 32}",
            "operation": "provider_egress.image_vision",
        },
    }
    confirmation = value["native_confirmation"]
    assert isinstance(confirmation, dict)
    confirmation["operation_sha256"] = canonical_sha256(deepcopy(value))
    return seal_provider_egress_grant_intent(value)


def manifest_plan(intent: dict[str, object] | None = None) -> dict[str, object]:
    grant = intent or grant_intent()
    value = {
        "object": "memolens.provider_payload_manifest_plan",
        "schema_version": "1",
        "manifest_id": f"pmanifest_{'8' * 32}",
        "grant_id": grant["grant_id"],
        "grant_intent_sha256": grant["grant_intent_sha256"],
        "database_binding": deepcopy(grant["database_binding"]),
        "job_id": grant["job_id"],
        "request_sha256": grant["request_sha256"],
        "asset_id": grant["asset_id"],
        "source_id": grant["source_id"],
        "input_asset_sha256": grant["input_asset_sha256"],
        "source_binding_sha256": grant["source_binding_sha256"],
        "analysis_profile": deepcopy(grant["analysis_profile"]),
        "provider_profile": deepcopy(grant["provider_profile"]),
        "provider": grant["provider"],
        "model": grant["model"],
        "capability": grant["capability"],
        "purpose": grant["purpose"],
        "payload_class": grant["payload_class"],
        "wire_payload_sha256": grant["wire_payload_sha256"],
        "wire_payload_bytes": grant["wire_payload_bytes"],
        "adapter_contract": deepcopy(grant["adapter_contract"]),
        "runtime_generation": RUNTIME_GENERATION,
        "attempt": 1,
        "attempt_authority_sha256": _sha("attempt-authority"),
    }
    return seal_provider_payload_manifest_plan(
        value,
        permit_sha256=_sha("process-private-permit"),
        grant_intent=grant,
    )


class ProviderEgressContractTests(unittest.TestCase):
    def test_grant_and_manifest_are_deterministic_detached_closed_documents(self) -> None:
        intent = grant_intent()
        plan = manifest_plan(intent)
        self.assertEqual(
            intent["grant_intent_sha256"],
            provider_egress_grant_intent_sha256(intent),
        )
        self.assertEqual(
            plan["manifest_plan_sha256"],
            provider_payload_manifest_plan_sha256(plan),
        )
        self.assertEqual(require_valid_provider_egress_grant_intent(intent), intent)
        self.assertEqual(require_valid_provider_payload_manifest_plan(plan), plan)
        self.assertEqual(require_manifest_matches_grant_intent(plan, intent), plan)
        detached = require_valid_provider_egress_grant_intent(intent)
        detached["provider"] = "changed"
        self.assertEqual(intent["provider"], "example-provider")

    def test_native_confirmation_binds_every_operation_field_and_expiry(self) -> None:
        for field, replacement in (
            ("model", "different-model"),
            ("wire_payload_sha256", _sha("different-wire")),
            ("expires_at", "2026-08-29T12:06:00Z"),
        ):
            with self.subTest(field=field):
                changed = grant_intent()
                changed[field] = replacement
                errors = validate_provider_egress_grant_intent(changed)
                self.assertIn("native_operation_mismatch", {error["code"] for error in errors})

    def test_grant_is_attempt_independent_but_manifest_binds_exact_attempt(self) -> None:
        intent = grant_intent()
        forged = deepcopy(intent)
        forged["attempt"] = 1
        self.assertIn(
            "unknown_field",
            {error["code"] for error in validate_provider_egress_grant_intent(forged)},
        )
        plan = manifest_plan(intent)
        self.assertEqual(plan["attempt"], 1)
        self.assertEqual(plan["runtime_generation"], RUNTIME_GENERATION)
        self.assertEqual(plan["attempt_authority_sha256"], _sha("attempt-authority"))

    def test_only_image_vision_purpose_payload_class_and_planned_are_allowed(self) -> None:
        intent = grant_intent()
        for field, value, expected in (
            ("capability", "video_vlm", "unsupported_capability"),
            ("purpose", "general_upload", "unsupported_purpose"),
            ("payload_class", "original_image", "unsupported_payload_class"),
        ):
            with self.subTest(field=field):
                changed = deepcopy(intent)
                changed[field] = value
                self.assertIn(
                    expected,
                    {error["code"] for error in validate_provider_egress_grant_intent(changed)},
                )
        plan = manifest_plan(intent)
        plan["outcome"] = "sent"
        self.assertIn(
            "invalid_manifest_outcome",
            {error["code"] for error in validate_provider_payload_manifest_plan(plan)},
        )

    def test_grant_ttl_is_positive_and_at_most_ten_minutes(self) -> None:
        for expires_at in (
            "2026-08-29T12:00:00Z",
            "2026-08-29T12:10:00.000001Z",
            "2026-08-29T12:99:00Z",
            "2026-08-29T12:05:00+00:00",
        ):
            with self.subTest(expires_at=expires_at):
                changed = grant_intent()
                changed["expires_at"] = expires_at
                self.assertTrue(validate_provider_egress_grant_intent(changed))

    def test_database_binding_includes_uuid_device_and_inode(self) -> None:
        intent = grant_intent()
        for missing in ("database_uuid", "database_device", "database_inode"):
            with self.subTest(missing=missing):
                changed = deepcopy(intent)
                binding = changed["database_binding"]
                assert isinstance(binding, dict)
                binding.pop(missing)
                self.assertIn(
                    "required_field_missing",
                    {error["code"] for error in validate_provider_egress_grant_intent(changed)},
                )

    def test_manifest_must_match_every_grant_scope_field(self) -> None:
        intent = grant_intent()
        mutations = (
            ("database_binding", {"database_uuid": DATABASE_UUID, "database_device": 43, "database_inode": 84}),
            ("request_sha256", _sha("different-request")),
            ("asset_id", f"asset_{'9' * 24}"),
            ("source_id", f"src_{'a' * 24}"),
            ("provider", "other-provider"),
            ("model", "other-model"),
            ("wire_payload_sha256", _sha("other-payload")),
            ("wire_payload_bytes", len(WIRE_PAYLOAD) + 1),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                changed = manifest_plan(intent)
                changed[field] = value
                changed = seal_provider_payload_manifest_plan(changed)
                with self.assertRaisesRegex(
                    ProviderEgressContractError,
                    "exact sealed grant intent",
                ):
                    require_manifest_matches_grant_intent(changed, intent)

    def test_digest_corruption_is_detected(self) -> None:
        intent = grant_intent()
        intent["grant_intent_sha256"] = "f" * 64
        self.assertIn(
            "digest_mismatch",
            {error["code"] for error in validate_provider_egress_grant_intent(intent)},
        )
        plan = manifest_plan()
        plan["manifest_plan_sha256"] = "f" * 64
        self.assertIn(
            "digest_mismatch",
            {error["code"] for error in validate_provider_payload_manifest_plan(plan)},
        )

    def test_token_path_raw_payload_and_attempt_cannot_enter_grant(self) -> None:
        for field, value in (
            ("token", "provider-secret"),
            ("path", "/Users/private/image.jpg"),
            ("raw_payload", {"image": "private"}),
            ("attempt", 1),
        ):
            with self.subTest(field=field):
                changed = grant_intent()
                changed[field] = value
                self.assertIn(
                    "unknown_field",
                    {error["code"] for error in validate_provider_egress_grant_intent(changed)},
                )
        serialized = canonical_json(manifest_plan())
        self.assertNotIn("provider-secret", serialized)
        self.assertNotIn("/Users/private", serialized)
        self.assertNotIn("exact-wire-body", serialized)

    def test_path_like_identifier_and_unknown_nested_fields_fail_closed(self) -> None:
        intent = grant_intent()
        intent["provider"] = "/tmp/provider"
        self.assertIn(
            "invalid_string",
            {error["code"] for error in validate_provider_egress_grant_intent(intent)},
        )
        intent = grant_intent()
        adapter = intent["adapter_contract"]
        assert isinstance(adapter, dict)
        adapter["api_key"] = "secret"
        self.assertIn(
            "unknown_field",
            {error["code"] for error in validate_provider_egress_grant_intent(intent)},
        )

    def test_resource_bounds_and_non_json_values_fail_without_recursion_crash(self) -> None:
        intent = grant_intent()
        intent["provider"] = "x" * PROVIDER_EGRESS_MAX_CANONICAL_BYTES
        self.assertIn(
            "document_too_large",
            {error["code"] for error in validate_provider_egress_grant_intent(intent)},
        )
        cyclic = grant_intent()
        cyclic["cycle"] = cyclic
        self.assertIn(
            "cyclic_value",
            {error["code"] for error in validate_provider_egress_grant_intent(cyclic)},
        )
        non_json = grant_intent()
        non_json["provider"] = object()
        self.assertTrue(validate_provider_egress_grant_intent(non_json))

    def test_sealed_documents_are_canonical_json_round_trip_stable(self) -> None:
        for document in (grant_intent(), manifest_plan()):
            encoded = canonical_json(document)
            self.assertEqual(canonical_json(json.loads(encoded)), encoded)


if __name__ == "__main__":
    unittest.main()
