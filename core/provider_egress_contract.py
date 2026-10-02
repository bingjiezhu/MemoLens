"""Closed, path-free contracts for one explicitly authorized provider send.

The grant intent binds user consent to an exact derived payload without
persisting either the consent token or the wire body.  The manifest plan adds
the exact runtime attempt and a digest of a process-private send permit.  This
module is deliberately transport- and persistence-free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any
import unicodedata


PROVIDER_EGRESS_GRANT_INTENT_OBJECT = "memolens.provider_egress_grant_intent"
PROVIDER_PAYLOAD_MANIFEST_PLAN_OBJECT = "memolens.provider_payload_manifest_plan"
PROVIDER_EGRESS_SCHEMA_VERSION = "1"
PROVIDER_EGRESS_CAPABILITY = "image_vision"
PROVIDER_EGRESS_PURPOSE = "canonical_image_analysis"
PROVIDER_EGRESS_PAYLOAD_CLASS = "derived_image_bytes"
PROVIDER_EGRESS_NATIVE_OPERATION = "provider_egress.image_vision"

PROVIDER_EGRESS_MAX_CANONICAL_BYTES = 65_536
PROVIDER_EGRESS_MAX_DEPTH = 16
PROVIDER_EGRESS_MAX_NODES = 2_048
PROVIDER_EGRESS_MAX_ERRORS = 64
PROVIDER_EGRESS_MAX_WIRE_BYTES = 16_777_216
PROVIDER_EGRESS_MAX_GRANT_TTL_SECONDS = 600

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_GRANT_ID = re.compile(r"^pgrant_[0-9a-f]{32}$")
_MANIFEST_ID = re.compile(r"^pmanifest_[0-9a-f]{32}$")
_CONFIRMATION_ID = re.compile(r"^confirm_[0-9a-f]{32}$")
_RUNTIME_GENERATION = re.compile(r"^runtime_generation_[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807

_DATABASE_FIELDS = {"database_uuid", "database_device", "database_inode"}
_PROFILE_FIELDS = {"profile_id", "profile_version", "profile_sha256"}
_ADAPTER_FIELDS = {"adapter_id", "adapter_version", "contract_sha256"}
_CONFIRMATION_FIELDS = {"confirmation_id", "operation", "operation_sha256"}
_GRANT_FIELDS = {
    "object",
    "schema_version",
    "grant_id",
    "capability",
    "database_binding",
    "job_id",
    "request_sha256",
    "asset_id",
    "source_id",
    "input_asset_sha256",
    "source_binding_sha256",
    "analysis_profile",
    "provider_profile",
    "provider",
    "model",
    "purpose",
    "payload_class",
    "wire_payload_sha256",
    "wire_payload_bytes",
    "adapter_contract",
    "issued_at",
    "expires_at",
    "native_confirmation",
}
_MANIFEST_FIELDS = {
    "object",
    "schema_version",
    "manifest_id",
    "grant_id",
    "grant_intent_sha256",
    "database_binding",
    "job_id",
    "request_sha256",
    "asset_id",
    "source_id",
    "input_asset_sha256",
    "source_binding_sha256",
    "analysis_profile",
    "provider_profile",
    "provider",
    "model",
    "capability",
    "purpose",
    "payload_class",
    "wire_payload_sha256",
    "wire_payload_bytes",
    "adapter_contract",
    "runtime_generation",
    "attempt",
    "attempt_authority_sha256",
    "permit_sha256",
    "outcome",
}
_SHARED_SCOPE_FIELDS = (
    "database_binding",
    "job_id",
    "request_sha256",
    "asset_id",
    "source_id",
    "input_asset_sha256",
    "source_binding_sha256",
    "analysis_profile",
    "provider_profile",
    "provider",
    "model",
    "capability",
    "purpose",
    "payload_class",
    "wire_payload_sha256",
    "wire_payload_bytes",
    "adapter_contract",
)


@dataclass
class ProviderEgressContractError(ValueError):
    """Raised when a provider-egress document is not closed and exact."""

    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_provider_egress_contract"),
                "path": str(item.get("path") or ""),
                "message": str(item.get("message") or "Provider-egress contract is invalid."),
            }
            for item in errors
        )
        self.errors = normalized
        ValueError.__init__(
            self,
            normalized[0]["message"] if normalized else "Provider-egress contract is invalid.",
        )


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) < PROVIDER_EGRESS_MAX_ERRORS:
            self.items.append({"code": code, "path": path, "message": message})


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _closed(
    value: object,
    *,
    path: str,
    fields: set[str],
    issues: _Issues,
) -> dict[str, Any] | None:
    if type(value) is not dict:
        issues.add("schema_type", path, "Value must be an object.")
        return None
    if any(type(key) is not str for key in value):
        issues.add("invalid_object_key", path, "Object keys must be strings.")
        return None
    row = value
    for field in sorted(fields - set(row)):
        issues.add("required_field_missing", f"{path}/{field}", "A required field is missing.")
    if set(row) - fields:
        issues.add("unknown_field", f"{path}/*", "Object contains an unsupported field.")
    return row


def _text(
    value: object,
    path: str,
    issues: _Issues,
    *,
    pattern: re.Pattern[str] = _IDENTIFIER,
    minimum: int = 1,
    maximum: int = 200,
    code: str = "invalid_string",
) -> bool:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        issues.add(code, path, "Value must be a bounded string.")
        return False
    if not unicodedata.is_normalized("NFC", value):
        issues.add(code, path, "String must use Unicode NFC normalization.")
        return False
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        issues.add(code, path, "String contains a forbidden control character.")
        return False
    if pattern.fullmatch(value) is None:
        issues.add(code, path, "String does not satisfy its closed format.")
        return False
    return True


def _sha(value: object, path: str, issues: _Issues) -> bool:
    return _text(
        value,
        path,
        issues,
        pattern=_SHA256,
        minimum=64,
        maximum=64,
        code="invalid_sha256",
    )


def _integer(value: object, path: str, issues: _Issues, *, minimum: int, maximum: int) -> bool:
    if type(value) is not int or not minimum <= value <= maximum:
        issues.add("invalid_integer", path, "Value must be a bounded integer.")
        return False
    return True


def _validate_bounds(value: object, issues: _Issues) -> None:
    nodes = 0
    active: set[int] = set()

    def visit(current: object, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > PROVIDER_EGRESS_MAX_NODES:
            issues.add("resource_too_large", path, "Document exceeds the node limit.")
            return
        if depth > PROVIDER_EGRESS_MAX_DEPTH:
            issues.add("resource_too_deep", path, "Document exceeds the nesting limit.")
            return
        if type(current) in {dict, list}:
            identity = id(current)
            if identity in active:
                issues.add("cyclic_value", path, "Cyclic values are forbidden.")
                return
            active.add(identity)
            children = current.items() if type(current) is dict else enumerate(current)
            for key, child in children:
                visit(child, f"{path}/{key}", depth + 1)
            active.remove(identity)
        elif type(current) is float and not math.isfinite(current):
            issues.add("invalid_number", path, "Non-finite numbers are forbidden.")
        elif current is not None and type(current) not in {str, int, float, bool}:
            issues.add("invalid_json_type", path, "Value is not a built-in JSON type.")

    visit(value, "", 0)
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        issues.add("invalid_json", "", "Document cannot be represented as canonical JSON.")
        return
    if len(encoded) > PROVIDER_EGRESS_MAX_CANONICAL_BYTES:
        issues.add("document_too_large", "", "Document exceeds its canonical byte limit.")


def _validate_database(value: object, path: str, issues: _Issues) -> None:
    row = _closed(value, path=path, fields=_DATABASE_FIELDS, issues=issues)
    if row is None:
        return
    _text(
        row.get("database_uuid"),
        f"{path}/database_uuid",
        issues,
        pattern=_DATABASE_UUID,
        minimum=36,
        maximum=36,
        code="invalid_database_uuid",
    )
    _integer(
        row.get("database_device"),
        f"{path}/database_device",
        issues,
        minimum=0,
        maximum=_SQLITE_INT64_MAX,
    )
    _integer(
        row.get("database_inode"),
        f"{path}/database_inode",
        issues,
        minimum=1,
        maximum=_SQLITE_INT64_MAX,
    )


def _validate_profile(value: object, path: str, issues: _Issues) -> None:
    row = _closed(value, path=path, fields=_PROFILE_FIELDS, issues=issues)
    if row is None:
        return
    _text(row.get("profile_id"), f"{path}/profile_id", issues)
    _text(row.get("profile_version"), f"{path}/profile_version", issues)
    _sha(row.get("profile_sha256"), f"{path}/profile_sha256", issues)


def _validate_adapter(value: object, path: str, issues: _Issues) -> None:
    row = _closed(value, path=path, fields=_ADAPTER_FIELDS, issues=issues)
    if row is None:
        return
    _text(row.get("adapter_id"), f"{path}/adapter_id", issues)
    _text(row.get("adapter_version"), f"{path}/adapter_version", issues)
    _sha(row.get("contract_sha256"), f"{path}/contract_sha256", issues)


def _parse_timestamp(value: object, path: str, issues: _Issues) -> datetime | None:
    if not _text(
        value,
        path,
        issues,
        pattern=_UTC_TIMESTAMP,
        minimum=20,
        maximum=27,
        code="invalid_timestamp",
    ):
        return None
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        issues.add("invalid_timestamp", path, "Timestamp must be a real UTC instant.")
        return None
    return parsed.astimezone(timezone.utc)


def _operation_material(value: Mapping[str, object]) -> dict[str, object]:
    material = deepcopy(dict(value))
    material.pop("grant_intent_sha256", None)
    confirmation = material.get("native_confirmation")
    if type(confirmation) is dict:
        confirmation.pop("operation_sha256", None)
    return material


def _validate_grant(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = set(_GRANT_FIELDS)
    if sealed:
        fields.add("grant_intent_sha256")
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is not None:
        if row.get("object") != PROVIDER_EGRESS_GRANT_INTENT_OBJECT:
            issues.add("invalid_object", "/object", "Grant object discriminator is invalid.")
        if row.get("schema_version") != PROVIDER_EGRESS_SCHEMA_VERSION:
            issues.add("unsupported_schema_version", "/schema_version", "Grant schema version is unsupported.")
        _text(row.get("grant_id"), "/grant_id", issues, pattern=_GRANT_ID, minimum=39, maximum=39)
        if row.get("capability") != PROVIDER_EGRESS_CAPABILITY:
            issues.add("unsupported_capability", "/capability", "Only image_vision is supported by v1.")
        _validate_database(row.get("database_binding"), "/database_binding", issues)
        _text(row.get("job_id"), "/job_id", issues, pattern=_JOB_ID, minimum=36, maximum=36)
        _sha(row.get("request_sha256"), "/request_sha256", issues)
        _text(row.get("asset_id"), "/asset_id", issues, pattern=_ASSET_ID, minimum=30, maximum=30)
        _text(row.get("source_id"), "/source_id", issues, pattern=_SOURCE_ID, minimum=28, maximum=28)
        _sha(row.get("input_asset_sha256"), "/input_asset_sha256", issues)
        _sha(row.get("source_binding_sha256"), "/source_binding_sha256", issues)
        _validate_profile(row.get("analysis_profile"), "/analysis_profile", issues)
        _validate_profile(row.get("provider_profile"), "/provider_profile", issues)
        _text(row.get("provider"), "/provider", issues)
        _text(row.get("model"), "/model", issues)
        if row.get("purpose") != PROVIDER_EGRESS_PURPOSE:
            issues.add("unsupported_purpose", "/purpose", "Provider purpose is outside the closed registry.")
        if row.get("payload_class") != PROVIDER_EGRESS_PAYLOAD_CLASS:
            issues.add("unsupported_payload_class", "/payload_class", "Payload class is outside the closed registry.")
        _sha(row.get("wire_payload_sha256"), "/wire_payload_sha256", issues)
        _integer(
            row.get("wire_payload_bytes"),
            "/wire_payload_bytes",
            issues,
            minimum=2,
            maximum=PROVIDER_EGRESS_MAX_WIRE_BYTES,
        )
        _validate_adapter(row.get("adapter_contract"), "/adapter_contract", issues)
        issued = _parse_timestamp(row.get("issued_at"), "/issued_at", issues)
        expires = _parse_timestamp(row.get("expires_at"), "/expires_at", issues)
        if issued is not None and expires is not None:
            ttl = (expires - issued).total_seconds()
            if not 0 < ttl <= PROVIDER_EGRESS_MAX_GRANT_TTL_SECONDS:
                issues.add("invalid_expiry", "/expires_at", "Grant expiry must be after issue and within ten minutes.")
        confirmation = _closed(
            row.get("native_confirmation"),
            path="/native_confirmation",
            fields=_CONFIRMATION_FIELDS,
            issues=issues,
        )
        if confirmation is not None:
            _text(
                confirmation.get("confirmation_id"),
                "/native_confirmation/confirmation_id",
                issues,
                pattern=_CONFIRMATION_ID,
                minimum=40,
                maximum=40,
            )
            if confirmation.get("operation") != PROVIDER_EGRESS_NATIVE_OPERATION:
                issues.add("invalid_native_operation", "/native_confirmation/operation", "Native operation is not image vision egress.")
            if _sha(confirmation.get("operation_sha256"), "/native_confirmation/operation_sha256", issues):
                try:
                    expected_operation = canonical_sha256(_operation_material(row))
                except (TypeError, ValueError, UnicodeError, RecursionError):
                    expected_operation = None
                if confirmation.get("operation_sha256") != expected_operation:
                    issues.add("native_operation_mismatch", "/native_confirmation/operation_sha256", "Native confirmation does not bind this exact operation.")
        if sealed and _sha(row.get("grant_intent_sha256"), "/grant_intent_sha256", issues):
            try:
                expected = canonical_sha256({key: deepcopy(item) for key, item in row.items() if key != "grant_intent_sha256"})
            except (TypeError, ValueError, UnicodeError, RecursionError):
                expected = None
            if row.get("grant_intent_sha256") != expected:
                issues.add("digest_mismatch", "/grant_intent_sha256", "Grant digest does not match canonical content.")
    _validate_bounds(value, issues)
    return issues.items


def validate_provider_egress_grant_intent(value: object) -> list[dict[str, str]]:
    return _validate_grant(value, sealed=True)


def require_valid_provider_egress_grant_intent(value: object) -> dict[str, Any]:
    errors = validate_provider_egress_grant_intent(value)
    if errors or type(value) is not dict:
        raise ProviderEgressContractError(errors)
    return deepcopy(value)


def provider_egress_grant_intent_sha256(value: Mapping[str, object]) -> str:
    normalized = deepcopy(dict(value))
    normalized.pop("grant_intent_sha256", None)
    errors = _validate_grant(normalized, sealed=False)
    if errors:
        raise ProviderEgressContractError(errors)
    return canonical_sha256(normalized)


def seal_provider_egress_grant_intent(value: Mapping[str, object]) -> dict[str, object]:
    normalized = deepcopy(dict(value))
    normalized.pop("grant_intent_sha256", None)
    errors = _validate_grant(normalized, sealed=False)
    if errors:
        raise ProviderEgressContractError(errors)
    normalized["grant_intent_sha256"] = canonical_sha256(normalized)
    return require_valid_provider_egress_grant_intent(normalized)


def _validate_manifest(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = set(_MANIFEST_FIELDS)
    if sealed:
        fields.add("manifest_plan_sha256")
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is not None:
        if row.get("object") != PROVIDER_PAYLOAD_MANIFEST_PLAN_OBJECT:
            issues.add("invalid_object", "/object", "Manifest object discriminator is invalid.")
        if row.get("schema_version") != PROVIDER_EGRESS_SCHEMA_VERSION:
            issues.add("unsupported_schema_version", "/schema_version", "Manifest schema version is unsupported.")
        _text(row.get("manifest_id"), "/manifest_id", issues, pattern=_MANIFEST_ID, minimum=42, maximum=42)
        _text(row.get("grant_id"), "/grant_id", issues, pattern=_GRANT_ID, minimum=39, maximum=39)
        _sha(row.get("grant_intent_sha256"), "/grant_intent_sha256", issues)
        _validate_database(row.get("database_binding"), "/database_binding", issues)
        _text(row.get("job_id"), "/job_id", issues, pattern=_JOB_ID, minimum=36, maximum=36)
        _sha(row.get("request_sha256"), "/request_sha256", issues)
        _text(row.get("asset_id"), "/asset_id", issues, pattern=_ASSET_ID, minimum=30, maximum=30)
        _text(row.get("source_id"), "/source_id", issues, pattern=_SOURCE_ID, minimum=28, maximum=28)
        _sha(row.get("input_asset_sha256"), "/input_asset_sha256", issues)
        _sha(row.get("source_binding_sha256"), "/source_binding_sha256", issues)
        _validate_profile(row.get("analysis_profile"), "/analysis_profile", issues)
        _validate_profile(row.get("provider_profile"), "/provider_profile", issues)
        _text(row.get("provider"), "/provider", issues)
        _text(row.get("model"), "/model", issues)
        if row.get("capability") != PROVIDER_EGRESS_CAPABILITY:
            issues.add("unsupported_capability", "/capability", "Only image_vision is supported by v1.")
        if row.get("purpose") != PROVIDER_EGRESS_PURPOSE:
            issues.add("unsupported_purpose", "/purpose", "Provider purpose is outside the closed registry.")
        if row.get("payload_class") != PROVIDER_EGRESS_PAYLOAD_CLASS:
            issues.add("unsupported_payload_class", "/payload_class", "Payload class is outside the closed registry.")
        _sha(row.get("wire_payload_sha256"), "/wire_payload_sha256", issues)
        _integer(row.get("wire_payload_bytes"), "/wire_payload_bytes", issues, minimum=2, maximum=PROVIDER_EGRESS_MAX_WIRE_BYTES)
        _validate_adapter(row.get("adapter_contract"), "/adapter_contract", issues)
        _text(
            row.get("runtime_generation"),
            "/runtime_generation",
            issues,
            pattern=_RUNTIME_GENERATION,
            minimum=83,
            maximum=83,
            code="invalid_runtime_generation",
        )
        _integer(row.get("attempt"), "/attempt", issues, minimum=1, maximum=100)
        _sha(row.get("attempt_authority_sha256"), "/attempt_authority_sha256", issues)
        _sha(row.get("permit_sha256"), "/permit_sha256", issues)
        if row.get("outcome") != "planned":
            issues.add("invalid_manifest_outcome", "/outcome", "A manifest plan must have the closed planned outcome.")
        if sealed and _sha(row.get("manifest_plan_sha256"), "/manifest_plan_sha256", issues):
            try:
                expected = canonical_sha256({key: deepcopy(item) for key, item in row.items() if key != "manifest_plan_sha256"})
            except (TypeError, ValueError, UnicodeError, RecursionError):
                expected = None
            if row.get("manifest_plan_sha256") != expected:
                issues.add("digest_mismatch", "/manifest_plan_sha256", "Manifest digest does not match canonical content.")
    _validate_bounds(value, issues)
    return issues.items


def validate_provider_payload_manifest_plan(value: object) -> list[dict[str, str]]:
    return _validate_manifest(value, sealed=True)


def require_valid_provider_payload_manifest_plan(value: object) -> dict[str, Any]:
    errors = validate_provider_payload_manifest_plan(value)
    if errors or type(value) is not dict:
        raise ProviderEgressContractError(errors)
    return deepcopy(value)


def provider_payload_manifest_plan_sha256(value: Mapping[str, object]) -> str:
    normalized = deepcopy(dict(value))
    normalized.pop("manifest_plan_sha256", None)
    errors = _validate_manifest(normalized, sealed=False)
    if errors:
        raise ProviderEgressContractError(errors)
    return canonical_sha256(normalized)


def require_manifest_matches_grant_intent(
    manifest_plan: object,
    grant_intent: object,
) -> dict[str, Any]:
    plan = require_valid_provider_payload_manifest_plan(manifest_plan)
    intent = require_valid_provider_egress_grant_intent(grant_intent)
    mismatches = [field for field in _SHARED_SCOPE_FIELDS if plan.get(field) != intent.get(field)]
    if plan.get("grant_id") != intent.get("grant_id"):
        mismatches.append("grant_id")
    if plan.get("grant_intent_sha256") != intent.get("grant_intent_sha256"):
        mismatches.append("grant_intent_sha256")
    if mismatches:
        raise ProviderEgressContractError(
            [
                {
                    "code": "manifest_grant_scope_mismatch",
                    "path": f"/{mismatches[0]}",
                    "message": "Manifest plan does not match the exact sealed grant intent.",
                }
            ]
        )
    return plan


def seal_provider_payload_manifest_plan(
    value: Mapping[str, object],
    *,
    permit_sha256: str | None = None,
    grant_intent: Mapping[str, object] | None = None,
) -> dict[str, object]:
    normalized = deepcopy(dict(value))
    normalized.pop("manifest_plan_sha256", None)
    if permit_sha256 is not None:
        current = normalized.get("permit_sha256")
        if current is not None and current != permit_sha256:
            raise ProviderEgressContractError(
                [{"code": "permit_digest_mismatch", "path": "/permit_sha256", "message": "Permit digest conflicts with the manifest plan."}]
            )
        normalized["permit_sha256"] = permit_sha256
    normalized.setdefault("outcome", "planned")
    errors = _validate_manifest(normalized, sealed=False)
    if errors:
        raise ProviderEgressContractError(errors)
    normalized["manifest_plan_sha256"] = canonical_sha256(normalized)
    sealed = require_valid_provider_payload_manifest_plan(normalized)
    if grant_intent is not None:
        require_manifest_matches_grant_intent(sealed, grant_intent)
    return sealed


__all__ = [
    "PROVIDER_EGRESS_CAPABILITY",
    "PROVIDER_EGRESS_GRANT_INTENT_OBJECT",
    "PROVIDER_EGRESS_MAX_WIRE_BYTES",
    "PROVIDER_EGRESS_PAYLOAD_CLASS",
    "PROVIDER_EGRESS_PURPOSE",
    "PROVIDER_EGRESS_SCHEMA_VERSION",
    "PROVIDER_PAYLOAD_MANIFEST_PLAN_OBJECT",
    "ProviderEgressContractError",
    "canonical_json",
    "canonical_sha256",
    "provider_egress_grant_intent_sha256",
    "provider_payload_manifest_plan_sha256",
    "require_manifest_matches_grant_intent",
    "require_valid_provider_egress_grant_intent",
    "require_valid_provider_payload_manifest_plan",
    "seal_provider_egress_grant_intent",
    "seal_provider_payload_manifest_plan",
    "validate_provider_egress_grant_intent",
    "validate_provider_payload_manifest_plan",
]
