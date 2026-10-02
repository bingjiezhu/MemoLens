"""Exact one-shot provider egress for canonical image vision.

The service never creates consent.  A native policy owner must supply an
already sealed grant intent and its process-private token through the grant
resolver.  Only a bounded, metadata-free JPEG derivative can enter the wire
payload, and the canonical bytes hashed by the manifest are handed unchanged
to the transport.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import base64
import hashlib
import hmac
from io import BytesIO
import json
import os
import secrets
import stat
import threading
import urllib.error
import urllib.request
import uuid
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from core.image_analysis_contract import canonical_json
from core.image_analysis_persistence import ExactImageSource, ImageAnalysisPersistenceError
from core.network_policy import NetworkPolicyError, require_offline_safe_url
from core.provider_egress_contract import (
    ProviderEgressContractError,
    require_valid_provider_egress_grant_intent,
    seal_provider_payload_manifest_plan,
)
from core.provider_egress_persistence import ProviderEgressPersistenceError


PROVIDER_WIRE_PROMPT = (
    "Describe this image factually. Return strict JSON with exactly description, "
    "tags, and location_hint. Tags must be unique strings. Use null when a "
    "location cannot be supported by visible evidence."
)
PROVIDER_ADAPTER_ID = "memolens.provider.image-vision"
PROVIDER_ADAPTER_VERSION = "1"
PROVIDER_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"memolens.provider.image-vision/openai-compatible-canonical-json/v1"
).hexdigest()
PROVIDER_MAX_DERIVATIVE_DIMENSION = 1_536
PROVIDER_MAX_DERIVATIVE_BYTES = 4 * 1024 * 1024
PROVIDER_MAX_RESPONSE_BYTES = 1024 * 1024
SOURCE_READ_CHUNK_BYTES = 1024 * 1024


def _disabled_stage(reason: str) -> dict[str, object]:
    return {
        "status": "disabled",
        "provenance": {
            "producer_id": "memolens.image-worker",
            "producer_version": "1",
            "model_id": None,
            "model_version": None,
            "rule_id": "provider-policy",
            "rule_version": "1",
        },
        "output": None,
        "artifact_sha256": None,
        "reason_code": reason,
    }


def _failed_stage(
    reason: str,
    *,
    provider: str,
    model: str,
    profile_version: str,
) -> dict[str, object]:
    return {
        "status": "failed",
        "provenance": {
            "producer_id": f"memolens.provider.{provider}",
            "producer_version": PROVIDER_ADAPTER_VERSION,
            "model_id": model,
            "model_version": profile_version,
            "rule_id": PROVIDER_ADAPTER_ID,
            "rule_version": PROVIDER_ADAPTER_VERSION,
        },
        "output": None,
        "artifact_sha256": None,
        "reason_code": reason,
    }


class ProviderEgressServiceError(RuntimeError):
    """Stable provider service failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ProviderTransportKnownFailure(RuntimeError):
    """The request crossed the send boundary and the provider rejected it."""

    def __init__(self, code: str, *, bytes_sent: int):
        super().__init__(code)
        self.code = code
        self.bytes_sent = bytes_sent


class ProviderTransportOutcomeUnknown(RuntimeError):
    """The caller cannot prove whether the provider received the payload."""


@dataclass(frozen=True)
class ProviderTransportResponse:
    payload: Mapping[str, object]
    bytes_sent: int


class ProviderSendPermitAuthority:
    """Process-private, one-shot authorization for one reserved manifest."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}

    def issue(self, manifest_id: str) -> tuple[str, str]:
        if not isinstance(manifest_id, str) or not manifest_id:
            raise ProviderEgressServiceError("provider_egress_manifest_invalid")
        permit = secrets.token_urlsafe(48)
        digest = hashlib.sha256(permit.encode("utf-8")).hexdigest()
        with self._lock:
            if digest in self._active:
                raise ProviderEgressServiceError("provider_send_permit_conflict")
            self._active[digest] = manifest_id
        return permit, digest

    def consume(self, manifest_id: str, permit: str) -> str:
        if not isinstance(permit, str):
            raise ProviderEgressServiceError("provider_send_permit_invalid")
        digest = hashlib.sha256(permit.encode("utf-8")).hexdigest()
        with self._lock:
            expected = self._active.get(digest)
            if expected is None or not hmac.compare_digest(expected, manifest_id):
                raise ProviderEgressServiceError("provider_send_permit_unavailable")
            del self._active[digest]
        return digest

    def discard(self, manifest_id: str, permit: str) -> bool:
        if not isinstance(permit, str):
            return False
        digest = hashlib.sha256(permit.encode("utf-8")).hexdigest()
        with self._lock:
            expected = self._active.get(digest)
            if expected is None or not hmac.compare_digest(expected, manifest_id):
                return False
            del self._active[digest]
            return True

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)


class OpenAICompatibleProviderTransport:
    """Minimal transport that sends caller-owned canonical JSON bytes verbatim."""

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        if not 0.1 <= timeout_seconds <= 300.0:
            raise ValueError("Provider timeout is outside the supported range.")
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def preflight(
        *,
        endpoint_url: str,
        credential: str,
        wire_payload: bytes,
        provider: str,
        model: str,
    ) -> None:
        del provider, model
        require_offline_safe_url(endpoint_url)
        if not isinstance(credential, str) or not 1 <= len(credential) <= 8_192:
            raise ProviderEgressServiceError("provider_credential_unavailable")
        if not isinstance(wire_payload, bytes) or not 2 <= len(wire_payload) <= 16_777_216:
            raise ProviderEgressServiceError("provider_wire_payload_invalid")

    def send(
        self,
        *,
        endpoint_url: str,
        credential: str,
        wire_payload: bytes,
        provider: str,
        model: str,
    ) -> ProviderTransportResponse:
        del provider, model
        request = urllib.request.Request(
            endpoint_url,
            data=wire_payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(PROVIDER_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise ProviderTransportKnownFailure(
                "provider_http_rejected",
                bytes_sent=len(wire_payload),
            ) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise ProviderTransportOutcomeUnknown("transport_outcome_unknown") from exc
        if len(raw) > PROVIDER_MAX_RESPONSE_BYTES:
            raise ProviderTransportOutcomeUnknown("provider_response_too_large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderTransportOutcomeUnknown("provider_response_invalid") from exc
        if type(payload) is not dict:
            raise ProviderTransportOutcomeUnknown("provider_response_invalid")
        return ProviderTransportResponse(payload=payload, bytes_sent=len(wire_payload))


def _verify_held_source(
    source: ExactImageSource,
    binding: Mapping[str, object],
) -> None:
    before = os.fstat(source.handle.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise ImageAnalysisPersistenceError("image_source_changed")
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(source.handle.fileno(), SOURCE_READ_CHUNK_BYTES, offset):
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(source.handle.fileno())
    observed = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    )
    if (
        observed
        != (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        or observed
        != (
            int(binding["source_device"]),
            int(binding["source_inode"]),
            int(binding["observed_size"]),
            int(binding["observed_mtime_ns"]),
            int(binding["observed_ctime_ns"]),
        )
        or digest.hexdigest() != binding["input_asset_sha256"]
    ):
        raise ImageAnalysisPersistenceError("image_source_changed")


def _bounded_jpeg_derivative(
    source: ExactImageSource,
    binding: Mapping[str, object],
) -> bytes:
    _verify_held_source(source, binding)
    try:
        with os.fdopen(os.dup(source.handle.fileno()), "rb") as handle:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(handle) as opened:
                    opened.load()
                    image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail(
            (PROVIDER_MAX_DERIVATIVE_DIMENSION, PROVIDER_MAX_DERIVATIVE_DIMENSION),
            Image.Resampling.LANCZOS,
        )
        output = BytesIO()
        image.save(output, format="JPEG", quality=85, optimize=True, progressive=False)
        derivative = output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ProviderEgressServiceError("provider_image_dimensions_unsupported") from exc
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
        raise ProviderEgressServiceError("provider_image_decode_failed") from exc
    if not 1 <= len(derivative) <= PROVIDER_MAX_DERIVATIVE_BYTES:
        raise ProviderEgressServiceError("provider_image_derivative_too_large")
    _verify_held_source(source, binding)
    return derivative


def _wire_payload(*, derivative: bytes, model: str) -> bytes:
    data_url = "data:image/jpeg;base64," + base64.b64encode(derivative).decode("ascii")
    document = {
        "max_tokens": 1_024,
        "messages": [
            {"content": PROVIDER_WIRE_PROMPT, "role": "system"},
            {
                "content": [
                    {"text": "Analyze this exact image derivative.", "type": "text"},
                    {"image_url": {"detail": "low", "url": data_url}, "type": "image_url"},
                ],
                "role": "user",
            },
        ],
        "model": model,
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    return canonical_json(document).encode("utf-8")


def _profile(value: Mapping[str, object] | None) -> dict[str, str] | None:
    if value is None:
        return None
    expected = {
        "provider",
        "model",
        "endpoint_url",
        "profile_id",
        "profile_version",
        "profile_sha256",
    }
    if type(value) is not dict or set(value) != expected:
        raise ProviderEgressServiceError("provider_profile_invalid")
    normalized = {key: str(value[key]) for key in expected}
    if any(not item or len(item) > 8_192 for item in normalized.values()):
        raise ProviderEgressServiceError("provider_profile_invalid")
    digest = normalized["profile_sha256"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ProviderEgressServiceError("provider_profile_invalid")
    return normalized


def _extract_vision_output(payload: Mapping[str, object]) -> dict[str, object]:
    candidate: object = payload
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        message = choices[0].get("message")
        if isinstance(message, Mapping):
            candidate = message.get("content")
    if isinstance(candidate, str):
        if len(candidate.encode("utf-8")) > PROVIDER_MAX_RESPONSE_BYTES:
            raise ProviderEgressServiceError("provider_response_invalid")
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ProviderEgressServiceError("provider_response_invalid") from exc
    if not isinstance(candidate, Mapping):
        raise ProviderEgressServiceError("provider_response_invalid")

    description_value = candidate.get("description")
    description = (
        " ".join(description_value.split())[:32_768]
        if isinstance(description_value, str)
        else ""
    )
    tags_value = candidate.get("tags")
    if not isinstance(tags_value, list):
        tags_value = []
    tags = sorted(
        {
            " ".join(tag.split())[:256]
            for tag in tags_value[:512]
            if isinstance(tag, str) and " ".join(tag.split())
        }
    )[:128]
    location_value = candidate.get("location_hint")
    location_hint = (
        " ".join(location_value.split())[:2_048]
        if isinstance(location_value, str) and " ".join(location_value.split())
        else None
    )
    if not description and not tags and location_hint is None:
        raise ProviderEgressServiceError("provider_response_invalid")
    return {
        "description": description,
        "tags": tags,
        "location_hint": location_hint,
    }


class ProviderImageVisionEgress:
    """Run one exact image-vision send or return an explicit local outcome."""

    def __init__(
        self,
        repository: object,
        *,
        profile: Mapping[str, object] | None = None,
        grant_resolver: Callable[[Mapping[str, object]], Mapping[str, object] | None]
        | None = None,
        credential_resolver: Callable[[], str | None] | None = None,
        transport: object | None = None,
        permit_authority: ProviderSendPermitAuthority | None = None,
    ) -> None:
        self.repository = repository
        self.profile = _profile(profile)
        self.grant_resolver = grant_resolver
        self.credential_resolver = credential_resolver
        self.transport = transport or OpenAICompatibleProviderTransport()
        self.permit_authority = permit_authority or ProviderSendPermitAuthority()

    def run(
        self,
        source: ExactImageSource,
        binding: Mapping[str, object],
        runtime_generation: str,
        expected_attempt: int,
    ) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
        if self.profile is None or self.grant_resolver is None:
            return _disabled_stage("provider_not_authorized"), ()
        profile = self.profile
        credential = self.credential_resolver() if self.credential_resolver else None
        if not isinstance(credential, str) or not credential:
            return _disabled_stage("provider_unavailable"), ()

        try:
            require_offline_safe_url(profile["endpoint_url"])
        except NetworkPolicyError:
            return _disabled_stage("network_not_authorized"), ()

        derivative = _bounded_jpeg_derivative(source, binding)
        wire_payload = _wire_payload(derivative=derivative, model=profile["model"])
        wire_sha256 = hashlib.sha256(wire_payload).hexdigest()
        request = {
            "object": "memolens.provider_egress_grant_request",
            "schema_version": "1",
            "capability": "image_vision",
            "database_binding": {
                "database_uuid": binding["database_uuid"],
                "database_device": binding["database_device"],
                "database_inode": binding["database_inode"],
            },
            "job_id": binding["job_id"],
            "request_sha256": binding["request_sha256"],
            "asset_id": binding["asset_id"],
            "source_id": binding["source_id"],
            "input_asset_sha256": binding["input_asset_sha256"],
            "source_binding_sha256": binding["source_binding_sha256"],
            "analysis_profile": {
                "profile_id": binding["analysis_profile_id"],
                "profile_version": binding["analysis_profile_version"],
                "profile_sha256": binding["analysis_profile_sha256"],
            },
            "provider_profile": {
                "profile_id": profile["profile_id"],
                "profile_version": profile["profile_version"],
                "profile_sha256": profile["profile_sha256"],
            },
            "provider": profile["provider"],
            "model": profile["model"],
            "purpose": "canonical_image_analysis",
            "payload_class": "derived_image_bytes",
            "wire_payload_sha256": wire_sha256,
            "wire_payload_bytes": len(wire_payload),
            "adapter_contract": {
                "adapter_id": PROVIDER_ADAPTER_ID,
                "adapter_version": PROVIDER_ADAPTER_VERSION,
                "contract_sha256": PROVIDER_ADAPTER_CONTRACT_SHA256,
            },
        }
        envelope = self.grant_resolver(request)
        if envelope is None:
            return _disabled_stage("provider_not_authorized"), ()
        if type(envelope) is not dict or set(envelope) != {"grant_intent", "grant_token"}:
            raise ProviderEgressServiceError("provider_grant_envelope_invalid")
        grant_intent = envelope["grant_intent"]
        grant_token = envelope["grant_token"]
        if type(grant_intent) is not dict or not isinstance(grant_token, str):
            raise ProviderEgressServiceError("provider_grant_envelope_invalid")
        try:
            sealed_intent = require_valid_provider_egress_grant_intent(grant_intent)
        except ProviderEgressContractError as exc:
            raise ProviderEgressServiceError("provider_grant_invalid") from exc
        for field in request:
            if field not in {"object", "schema_version"} and sealed_intent.get(field) != request[field]:
                raise ProviderEgressServiceError("provider_grant_scope_mismatch")

        record = getattr(self.repository, "record_provider_egress_grant", None)
        if not callable(record):
            raise ProviderEgressServiceError("provider_egress_persistence_unavailable")
        record(grant_intent=sealed_intent, grant_token=grant_token)

        manifest_id = f"pmanifest_{uuid.uuid4().hex}"
        permit, permit_sha256 = self.permit_authority.issue(manifest_id)
        manifest: dict[str, object] | None = None
        send_started = False
        try:
            manifest_plan = seal_provider_payload_manifest_plan(
                {
                    "object": "memolens.provider_payload_manifest_plan",
                    "schema_version": "1",
                    "manifest_id": manifest_id,
                    "grant_id": sealed_intent["grant_id"],
                    "grant_intent_sha256": sealed_intent["grant_intent_sha256"],
                    **{
                        key: request[key]
                        for key in (
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
                    },
                    "runtime_generation": runtime_generation,
                    "attempt": expected_attempt,
                    "attempt_authority_sha256": binding["attempt_authority_sha256"],
                },
                permit_sha256=permit_sha256,
                grant_intent=sealed_intent,
            )
            manifest = self.repository.reserve_provider_egress(
                grant_token=grant_token,
                manifest_plan=manifest_plan,
            )
            preflight = getattr(self.transport, "preflight", None)
            send = getattr(self.transport, "send", None)
            if not callable(preflight) or not callable(send):
                raise ProviderEgressServiceError("provider_transport_invalid")
            preflight(
                endpoint_url=profile["endpoint_url"],
                credential=credential,
                wire_payload=wire_payload,
                provider=profile["provider"],
                model=profile["model"],
            )
            _verify_held_source(source, binding)
            consumed_digest = self.permit_authority.consume(manifest_id, permit)
            if consumed_digest != permit_sha256:
                raise ProviderEgressServiceError("provider_send_permit_invalid")
            self.repository.mark_provider_egress_send_started(
                manifest_id=manifest_id,
                permit_sha256=permit_sha256,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                attempt_authority_sha256=str(binding["attempt_authority_sha256"]),
            )
            send_started = True
            response = send(
                endpoint_url=profile["endpoint_url"],
                credential=credential,
                wire_payload=wire_payload,
                provider=profile["provider"],
                model=profile["model"],
            )
        except ProviderTransportKnownFailure as exc:
            if not send_started:
                self.permit_authority.discard(manifest_id, permit)
                if manifest is not None:
                    self.repository.fail_provider_egress_before_send(
                        manifest_id=manifest_id,
                        permit_sha256=permit_sha256,
                        failure_code="provider_preflight_failed",
                    )
                raise ProviderEgressServiceError("provider_preflight_failed") from exc
            self.repository.finish_provider_egress_send(
                manifest_id=manifest_id,
                permit_sha256=permit_sha256,
                outcome="failed_after_send",
                bytes_sent=exc.bytes_sent,
                failure_code=exc.code,
            )
            return (
                _failed_stage(
                    exc.code,
                    provider=profile["provider"],
                    model=profile["model"],
                    profile_version=profile["profile_version"],
                ),
                (),
            )
        except ProviderTransportOutcomeUnknown as exc:
            if not send_started:
                self.permit_authority.discard(manifest_id, permit)
                if manifest is not None:
                    self.repository.fail_provider_egress_before_send(
                        manifest_id=manifest_id,
                        permit_sha256=permit_sha256,
                        failure_code="provider_preflight_failed",
                    )
                raise ProviderEgressServiceError("provider_preflight_failed") from exc
            self.repository.finish_provider_egress_send(
                manifest_id=manifest_id,
                permit_sha256=permit_sha256,
                outcome="ambiguous",
                bytes_sent=None,
                failure_code="transport_outcome_unknown",
            )
            return (
                _failed_stage(
                    "transport_outcome_unknown",
                    provider=profile["provider"],
                    model=profile["model"],
                    profile_version=profile["profile_version"],
                ),
                (),
            )
        except Exception:
            self.permit_authority.discard(manifest_id, permit)
            if send_started:
                self.repository.finish_provider_egress_send(
                    manifest_id=manifest_id,
                    permit_sha256=permit_sha256,
                    outcome="ambiguous",
                    bytes_sent=None,
                    failure_code="transport_outcome_unknown",
                )
                return (
                    _failed_stage(
                        "transport_outcome_unknown",
                        provider=profile["provider"],
                        model=profile["model"],
                        profile_version=profile["profile_version"],
                    ),
                    (),
                )
            if manifest is not None:
                try:
                    self.repository.fail_provider_egress_before_send(
                        manifest_id=manifest_id,
                        permit_sha256=permit_sha256,
                        failure_code="provider_preflight_failed",
                    )
                except ProviderEgressPersistenceError:
                    pass
            raise

        if not isinstance(response, ProviderTransportResponse):
            if send_started:
                self.repository.finish_provider_egress_send(
                    manifest_id=manifest_id,
                    permit_sha256=permit_sha256,
                    outcome="ambiguous",
                    bytes_sent=None,
                    failure_code="transport_accounting_invalid",
                )
            raise ProviderEgressServiceError("provider_transport_response_invalid")
        if response.bytes_sent != len(wire_payload):
            self.repository.finish_provider_egress_send(
                manifest_id=manifest_id,
                permit_sha256=permit_sha256,
                outcome="ambiguous",
                bytes_sent=None,
                failure_code="transport_accounting_invalid",
            )
            return (
                _failed_stage(
                    "transport_accounting_invalid",
                    provider=profile["provider"],
                    model=profile["model"],
                    profile_version=profile["profile_version"],
                ),
                (),
            )
        self.repository.finish_provider_egress_send(
            manifest_id=manifest_id,
            permit_sha256=permit_sha256,
            outcome="sent",
            bytes_sent=response.bytes_sent,
        )
        try:
            output = _extract_vision_output(response.payload)
        except ProviderEgressServiceError:
            return (
                _failed_stage(
                    "provider_response_invalid",
                    provider=profile["provider"],
                    model=profile["model"],
                    profile_version=profile["profile_version"],
                ),
                (),
            )
        output_bytes = canonical_json(output).encode("utf-8")
        output_sha256 = hashlib.sha256(output_bytes).hexdigest()
        stage = {
            "status": "succeeded",
            "provenance": {
                "producer_id": f"memolens.provider.{profile['provider']}",
                "producer_version": PROVIDER_ADAPTER_VERSION,
                "model_id": profile["model"],
                "model_version": profile["profile_version"],
                "rule_id": PROVIDER_ADAPTER_ID,
                "rule_version": PROVIDER_ADAPTER_VERSION,
            },
            "output": output,
            "artifact_sha256": output_sha256,
            "reason_code": None,
        }
        artifact = {
            "stage": "vision",
            "name": "stage_output",
            "media_type": "application/json",
            "signal": None,
            # Stage-output artifacts are bound by their stage result digest;
            # provider/model identity lives in the sealed stage provenance.
            # Vector artifacts use model_id, but duplicating it here violates
            # the canonical publication artifact contract.
            "model_id": None,
            "dimensions": None,
            "artifact_sha256": output_sha256,
            "bytes": output_bytes,
        }
        return stage, (artifact,)


__all__ = [
    "OpenAICompatibleProviderTransport",
    "ProviderEgressServiceError",
    "ProviderImageVisionEgress",
    "ProviderSendPermitAuthority",
    "ProviderTransportKnownFailure",
    "ProviderTransportOutcomeUnknown",
    "ProviderTransportResponse",
]
