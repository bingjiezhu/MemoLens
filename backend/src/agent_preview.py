"""Runtime-only, budgeted playback leases for paired canonical editors.

This broker is a data-plane authority, not a canonical write ledger.  It uses
separate mint/read proof domains and separate request/byte/concurrency budgets;
it never consumes an Agent capability operation or emits a ``used`` event.
Durable capability, current Timeline head, and exact source revalidation remain
mandatory route/repository checks before the first response byte.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import threading
from typing import Any

from core.timeline_preview_contract import (
    TIMELINE_PREVIEW_ACTION,
    TIMELINE_PREVIEW_LEASE_TTL_SECONDS,
    TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES,
    TIMELINE_PREVIEW_MAX_CONCURRENCY,
    TIMELINE_PREVIEW_MAX_REQUESTS,
    TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
    TimelinePreviewByteRange,
    TimelinePreviewContractError,
    require_timeline_preview_lease_request,
    require_timeline_preview_range,
)
from .agent_authority import RuntimeCapability


PREVIEW_LEASE_HEADER = "X-MemoLens-Preview-Lease"
PREVIEW_NONCE_HEADER = "X-MemoLens-Preview-Nonce"
PREVIEW_PROOF_HEADER = "X-MemoLens-Preview-Proof"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_LEASE_ID = re.compile(r"^preview_[0-9a-f]{32}$")
_REQUEST_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^(?:asset|img)_[0-9a-f]{24}$")
_ASSET_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_SUPPORTED_MIME_TYPES = frozenset({"video/mp4"})
_SUPPORTED_VIDEO_CODECS = frozenset({"h264"})


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


class AgentPreviewError(ValueError):
    """Stable preview error safe for the loopback Agent API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _proof(secret: str, message: str) -> str:
    if type(secret) is not str or _SHA256.fullmatch(secret) is None:
        raise ValueError("invalid_preview_proof_secret")
    return hmac.new(
        bytes.fromhex(secret),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def canonical_preview_mint_proof_message(
    *,
    capability_id: str,
    method: str,
    canonical_path: str,
    database_uuid: str,
    body_sha256: str,
) -> str:
    return (
        "MLPREVIEWMINT1\n"
        f"{capability_id}\n{method.upper()}\n{canonical_path}\n"
        f"{database_uuid}\n{body_sha256}"
    )


def agent_preview_mint_proof(secret: str, **fields: str) -> str:
    return _proof(secret, canonical_preview_mint_proof_message(**fields))


def canonical_preview_read_proof_message(
    *,
    lease_id: str,
    request_nonce: str,
    method: str,
    canonical_path: str,
    range_header: str | None,
) -> str:
    normalized_range = "" if range_header is None else range_header.strip()
    return (
        "MLPREVIEWREAD1\n"
        f"{lease_id}\n{request_nonce}\n{method.upper()}\n{canonical_path}\n"
        f"{normalized_range}"
    )


def agent_preview_read_proof(secret: str, **fields: str | None) -> str:
    return _proof(secret, canonical_preview_read_proof_message(**fields))


@dataclass
class _PreviewLease:
    lease_id: str
    database_uuid: str
    runtime_authority_epoch: str
    runtime_generation_id: str
    capability_id: str
    paired_subject_id: str
    project_id: str
    observed_head: dict[str, object]
    source_bindings_sha256: str
    secret: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    grants: dict[str, dict[str, object]]
    response_clips: list[dict[str, object]]
    requests_used: int = 0
    bytes_used: int = 0
    active_reads: int = 0
    read_nonces: set[str] = field(default_factory=set, repr=False)


@dataclass(frozen=True)
class PreviewReadReservation:
    reservation_id: str
    lease_id: str
    clip_id: str
    method: str
    byte_range: TimelinePreviewByteRange
    grant: dict[str, object] = field(repr=False)


@dataclass(frozen=True)
class PreviewLeaseAuthority:
    """Server-only lease facts used to select the definitive Core admission."""

    lease_id: str
    database_uuid: str
    runtime_generation_id: str
    capability_id: str
    paired_subject_id: str
    project_id: str
    observed_head: dict[str, object]
    source_bindings_sha256: str


def _closed_scope(value: object) -> dict[str, Any]:
    fields = {
        "object",
        "schema_version",
        "project_id",
        "timeline_id",
        "observed_head",
        "source_bindings_sha256",
        "clips",
    }
    if type(value) is not dict or set(value) != fields:
        raise AgentPreviewError(
            "agent_preview_scope_denied",
            "Timeline preview scope has an unsupported shape.",
            status=403,
        )
    if (
        value.get("object") != "memolens.timeline_preview_scope"
        or value.get("schema_version") != "1"
        or type(value.get("project_id")) is not str
        or _IDENTIFIER.fullmatch(value["project_id"]) is None
        or type(value.get("timeline_id")) is not str
        or _IDENTIFIER.fullmatch(value["timeline_id"]) is None
        or type(value.get("source_bindings_sha256")) is not str
        or _SHA256.fullmatch(value["source_bindings_sha256"]) is None
    ):
        raise AgentPreviewError(
            "agent_preview_scope_denied",
            "Timeline preview scope identity is invalid.",
            status=403,
        )
    head = value.get("observed_head")
    if not (
        type(head) is dict
        and set(head) == {"revision", "content_sha256"}
        and type(head.get("revision")) is int
        and 1 <= head["revision"] <= 1_800_000
        and type(head.get("content_sha256")) is str
        and _SHA256.fullmatch(head["content_sha256"]) is not None
    ):
        raise AgentPreviewError(
            "agent_preview_scope_denied",
            "Timeline preview scope head is invalid.",
            status=403,
        )
    clips = value.get("clips")
    clip_fields = {
        "clip_id",
        "ordinal",
        "asset_id",
        "asset_sha256",
        "asset_source_id",
        "timeline_start_ms",
        "timeline_end_ms",
        "source_in_ms",
        "source_out_ms",
        "source_binding_sha256",
    }
    if type(clips) is not list or len(clips) > 256:
        raise AgentPreviewError(
            "agent_preview_scope_denied",
            "Timeline preview clip scope is invalid.",
            status=403,
        )
    seen: set[str] = set()
    for clip in clips:
        if type(clip) is not dict or set(clip) != clip_fields:
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview clip scope has an unsupported shape.",
                status=403,
            )
        clip_id = clip.get("clip_id")
        if (
            type(clip_id) is not str
            or _IDENTIFIER.fullmatch(clip_id) is None
            or clip_id in seen
            or type(clip.get("asset_id")) is not str
            or _ASSET_ID.fullmatch(clip["asset_id"]) is None
            or type(clip.get("asset_sha256")) is not str
            or _SHA256.fullmatch(clip["asset_sha256"]) is None
            or type(clip.get("asset_source_id")) is not str
            or _ASSET_SOURCE_ID.fullmatch(clip["asset_source_id"]) is None
            or type(clip.get("source_binding_sha256")) is not str
            or _SHA256.fullmatch(clip["source_binding_sha256"]) is None
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview clip identity is invalid.",
                status=403,
            )
        seen.add(clip_id)
        integers = (
            clip.get("ordinal"),
            clip.get("timeline_start_ms"),
            clip.get("timeline_end_ms"),
            clip.get("source_in_ms"),
            clip.get("source_out_ms"),
        )
        if (
            any(type(item) is not int or item < 0 for item in integers)
            or clip["timeline_end_ms"] <= clip["timeline_start_ms"]
            or clip["source_out_ms"] <= clip["source_in_ms"]
            or clip["timeline_end_ms"] - clip["timeline_start_ms"]
            != clip["source_out_ms"] - clip["source_in_ms"]
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview clip timing is invalid.",
                status=403,
            )
    return deepcopy(value)


def _closed_descriptors(
    value: object,
    *,
    clip_ids: set[str],
) -> dict[str, dict[str, object]]:
    if type(value) is not list or len(value) != len(clip_ids):
        raise AgentPreviewError(
            "agent_preview_source_changed",
            "Timeline preview media descriptors are incomplete.",
            status=409,
        )
    result: dict[str, dict[str, object]] = {}
    fields = {"clip_id", "mime_type", "size_bytes", "probe_status", "video_codec"}
    for raw in value:
        if type(raw) is not dict or set(raw) != fields:
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview media descriptor has an unsupported shape.",
                status=409,
            )
        clip_id = raw.get("clip_id")
        size = raw.get("size_bytes")
        if (
            type(clip_id) is not str
            or clip_id not in clip_ids
            or clip_id in result
            or type(raw.get("mime_type")) is not str
            or type(raw.get("probe_status")) is not str
            or type(raw.get("video_codec")) is not str
            or type(size) is not int
            or not 1 <= size <= 9_223_372_036_854_775_807
        ):
            raise AgentPreviewError(
                "agent_preview_source_changed",
                "Timeline preview media descriptor is invalid.",
                status=409,
            )
        result[clip_id] = deepcopy(raw)
    if set(result) != clip_ids:
        raise AgentPreviewError(
            "agent_preview_source_changed",
            "Timeline preview media descriptors do not match current clips.",
            status=409,
        )
    return result


class PreviewLeaseBroker:
    """Bounded process-local holder for server-only canonical media leases."""

    def __init__(
        self,
        runtime_authority_epoch: str,
        *,
        now: Callable[[], datetime] = _utc_now,
        max_leases: int = 64,
    ) -> None:
        if (
            type(runtime_authority_epoch) is not str
            or _IDENTIFIER.fullmatch(runtime_authority_epoch) is None
        ):
            raise ValueError("invalid_preview_runtime_authority_epoch")
        if type(max_leases) is not int or max_leases < 1:
            raise ValueError("invalid_preview_max_leases")
        self.runtime_authority_epoch = runtime_authority_epoch
        self._now = now
        self._max_leases = max_leases
        # Spent mint nonces outlive their short admission claims so stale
        # head/source failures cannot be replayed.  Bound that negative cache
        # independently of configurable HTTP rate limits.
        self._max_mint_nonces = max_leases * 4
        self._leases: OrderedDict[str, _PreviewLease] = OrderedDict()
        self._reservations: dict[str, str] = {}
        self._mint_nonces: OrderedDict[tuple[str, str], datetime] = OrderedDict()
        self._mint_nonce_claims: dict[
            str,
            tuple[tuple[str, str], datetime],
        ] = {}
        self._lock = threading.RLock()

    def _purge_locked(self, now: datetime) -> None:
        for key, expiry in list(self._mint_nonces.items()):
            if expiry <= now:
                self._mint_nonces.pop(key, None)
        for claim_id, (_key, expiry) in list(self._mint_nonce_claims.items()):
            if expiry <= now:
                self._mint_nonce_claims.pop(claim_id, None)
        for lease_id, lease in list(self._leases.items()):
            if lease.expires_at <= now and lease.active_reads == 0:
                self._leases.pop(lease_id, None)

    @staticmethod
    def _validate_capability(
        capability: RuntimeCapability,
        *,
        now: datetime,
        runtime_authority_epoch: str,
    ) -> None:
        if (
            capability.status != "active"
            or capability.expires_at <= now
            or capability.runtime_authority_epoch != runtime_authority_epoch
            or TIMELINE_PREVIEW_ACTION not in capability.actions
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "The paired capability does not grant current Timeline media preview.",
                status=403,
            )

    def claim_mint_nonce(
        self,
        *,
        capability: RuntimeCapability,
        request_payload: object,
        proof: str,
        canonical_path: str,
    ) -> str:
        """Consume a valid mint nonce before fallible Core scope admission."""

        try:
            request_row = require_timeline_preview_lease_request(request_payload)
        except TimelinePreviewContractError as exc:
            raise AgentPreviewError(exc.code, exc.message, status=403) from exc
        now = self._now()
        self._validate_capability(
            capability,
            now=now,
            runtime_authority_epoch=self.runtime_authority_epoch,
        )
        if (
            canonical_path
            != (
                f"/v1/agent/creative/projects/{capability.project_id}/timeline/"
                "preview-leases"
            )
            or request_row["project_id"] != capability.project_id
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview lease is outside the paired project scope.",
                status=403,
            )
        expected_proof = agent_preview_mint_proof(
            capability.proof_secret,
            capability_id=capability.capability_id,
            method="POST",
            canonical_path=canonical_path,
            database_uuid=capability.database_uuid,
            body_sha256=_sha256_text(_canonical_json(request_row)),
        )
        if not proof or not hmac.compare_digest(proof, expected_proof):
            raise AgentPreviewError(
                "agent_preview_proof_invalid",
                "Timeline preview mint proof is invalid.",
                status=401,
            )
        nonce_key = (capability.capability_id, str(request_row["request_nonce"]))
        with self._lock:
            self._purge_locked(now)
            if nonce_key in self._mint_nonces:
                raise AgentPreviewError(
                    "agent_preview_nonce_invalid",
                    "Timeline preview mint nonce is missing, expired, or already used.",
                    status=403,
                )
            if len(self._mint_nonces) >= self._max_mint_nonces:
                raise AgentPreviewError(
                    "agent_preview_rate_limited",
                    "The runtime Timeline preview mint nonce store is full.",
                    status=429,
                )
            if len(self._mint_nonce_claims) >= self._max_leases:
                raise AgentPreviewError(
                    "agent_preview_rate_limited",
                    "The runtime Timeline preview mint admission store is full.",
                    status=429,
                )
            expires_at = min(
                capability.expires_at,
                now + timedelta(seconds=TIMELINE_PREVIEW_LEASE_TTL_SECONDS),
            )
            claim_id = f"preview_mint_{secrets.token_hex(16)}"
            self._mint_nonces[nonce_key] = expires_at
            self._mint_nonce_claims[claim_id] = (nonce_key, expires_at)
            return claim_id

    def abandon_mint_nonce_claim(self, claim_id: str) -> None:
        """Release transient claim storage without refunding the spent nonce."""

        with self._lock:
            self._mint_nonce_claims.pop(claim_id, None)

    def mint_lease(
        self,
        *,
        capability: RuntimeCapability,
        request_payload: object,
        proof: str,
        canonical_path: str,
        runtime_generation_id: str,
        scope: object,
        media_descriptors: object,
        mint_nonce_claim: str | None = None,
    ) -> dict[str, object]:
        try:
            request_row = require_timeline_preview_lease_request(request_payload)
        except TimelinePreviewContractError as exc:
            raise AgentPreviewError(exc.code, exc.message, status=403) from exc
        scope_row = _closed_scope(scope)
        now = self._now()
        self._validate_capability(
            capability,
            now=now,
            runtime_authority_epoch=self.runtime_authority_epoch,
        )
        if (
            type(runtime_generation_id) is not str
            or _IDENTIFIER.fullmatch(runtime_generation_id) is None
            or canonical_path
            != (
                f"/v1/agent/creative/projects/{capability.project_id}/timeline/"
                "preview-leases"
            )
            or request_row["project_id"] != capability.project_id
            or scope_row["project_id"] != capability.project_id
            or request_row["observed_head"] != scope_row["observed_head"]
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview lease is outside the paired project or head scope.",
                status=403,
            )
        expected_proof = agent_preview_mint_proof(
            capability.proof_secret,
            capability_id=capability.capability_id,
            method="POST",
            canonical_path=canonical_path,
            database_uuid=capability.database_uuid,
            body_sha256=_sha256_text(_canonical_json(request_row)),
        )
        if not proof or not hmac.compare_digest(proof, expected_proof):
            raise AgentPreviewError(
                "agent_preview_proof_invalid",
                "Timeline preview mint proof is invalid.",
                status=401,
            )
        descriptors = _closed_descriptors(
            media_descriptors,
            clip_ids={str(item["clip_id"]) for item in scope_row["clips"]},
        )
        nonce_key = (capability.capability_id, str(request_row["request_nonce"]))
        with self._lock:
            self._purge_locked(now)
            if mint_nonce_claim is None:
                if nonce_key in self._mint_nonces:
                    raise AgentPreviewError(
                        "agent_preview_nonce_invalid",
                        "Timeline preview mint nonce is missing, expired, or already used.",
                        status=403,
                    )
                if len(self._mint_nonces) >= self._max_mint_nonces:
                    raise AgentPreviewError(
                        "agent_preview_rate_limited",
                        "The runtime Timeline preview mint nonce store is full.",
                        status=429,
                    )
            else:
                claimed = self._mint_nonce_claims.pop(mint_nonce_claim, None)
                if claimed is None or claimed[0] != nonce_key:
                    raise AgentPreviewError(
                        "agent_preview_nonce_invalid",
                        "Timeline preview mint nonce claim is unavailable or mismatched.",
                        status=403,
                    )
            if len(self._leases) >= self._max_leases:
                raise AgentPreviewError(
                    "agent_preview_rate_limited",
                    "The runtime Timeline preview lease store is full.",
                    status=429,
                )
            if mint_nonce_claim is None:
                self._mint_nonces[nonce_key] = min(
                    capability.expires_at,
                    now + timedelta(seconds=TIMELINE_PREVIEW_LEASE_TTL_SECONDS),
                )
            lease_id = f"preview_{secrets.token_hex(16)}"
            lease_secret = secrets.token_hex(32)
            expires_at = min(
                capability.expires_at,
                now + timedelta(seconds=TIMELINE_PREVIEW_LEASE_TTL_SECONDS),
            )
            grants: dict[str, dict[str, object]] = {}
            response_clips: list[dict[str, object]] = []
            for raw in scope_row["clips"]:
                clip = deepcopy(raw)
                descriptor = descriptors[str(clip["clip_id"])]
                supported = (
                    descriptor["probe_status"] == "ready"
                    and str(descriptor["mime_type"]).casefold()
                    in _SUPPORTED_MIME_TYPES
                    and str(descriptor["video_codec"]).casefold()
                    in _SUPPORTED_VIDEO_CODECS
                )
                clip.update(descriptor)
                clip["status"] = "playable" if supported else "unsupported"
                clip["opaque_etag"] = hmac.new(
                    bytes.fromhex(lease_secret),
                    (
                        "MLPREVIEWETAG1\n"
                        f"{lease_id}\n{clip['clip_id']}\n"
                        f"{clip['source_binding_sha256']}"
                    ).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                grants[str(clip["clip_id"])] = clip
                public_clip = {
                    "clip_id": clip["clip_id"],
                    "timeline_start_ms": clip["timeline_start_ms"],
                    "timeline_end_ms": clip["timeline_end_ms"],
                    "source_in_ms": clip["source_in_ms"],
                    "source_out_ms": clip["source_out_ms"],
                    "status": clip["status"],
                    "reason_code": (
                        None
                        if supported
                        else "agent_preview_media_unsupported"
                    ),
                }
                response_clips.append(public_clip)
            lease = _PreviewLease(
                lease_id=lease_id,
                database_uuid=capability.database_uuid,
                runtime_authority_epoch=capability.runtime_authority_epoch,
                runtime_generation_id=runtime_generation_id,
                capability_id=capability.capability_id,
                paired_subject_id=capability.paired_subject_id,
                project_id=capability.project_id,
                observed_head=deepcopy(scope_row["observed_head"]),
                source_bindings_sha256=str(scope_row["source_bindings_sha256"]),
                secret=lease_secret,
                issued_at=now,
                expires_at=expires_at,
                grants=grants,
                response_clips=response_clips,
            )
            self._leases[lease_id] = lease
        return {
            "object": "memolens.timeline_preview_lease",
            "schema_version": "1",
            "lease_id": lease_id,
            "lease_secret": lease_secret,
            "project_id": capability.project_id,
            "observed_head": deepcopy(scope_row["observed_head"]),
            "issued_at": _iso(now),
            "expires_at": _iso(expires_at),
            "budgets": {
                "max_requests": TIMELINE_PREVIEW_MAX_REQUESTS,
                "max_response_bytes": TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
                "max_aggregate_bytes": TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES,
                "max_concurrency": TIMELINE_PREVIEW_MAX_CONCURRENCY,
            },
            "clips": deepcopy(response_clips),
        }

    def lease_authority(self, lease_id: str) -> PreviewLeaseAuthority:
        """Return only server-held identity facts; never serialize this object."""

        now = self._now()
        with self._lock:
            self._purge_locked(now)
            lease = self._leases.get(lease_id)
            if lease is None or lease.expires_at <= now:
                raise AgentPreviewError(
                    "agent_preview_lease_expired",
                    "Timeline preview lease is unavailable or expired.",
                    status=403,
                )
            return PreviewLeaseAuthority(
                lease_id=lease.lease_id,
                database_uuid=lease.database_uuid,
                runtime_generation_id=lease.runtime_generation_id,
                capability_id=lease.capability_id,
                paired_subject_id=lease.paired_subject_id,
                project_id=lease.project_id,
                observed_head=deepcopy(lease.observed_head),
                source_bindings_sha256=lease.source_bindings_sha256,
            )

    def begin_read(
        self,
        *,
        lease_id: str,
        request_nonce: str,
        proof: str,
        method: str,
        canonical_path: str,
        range_header: str | None,
        current_database_uuid: str,
        current_runtime_generation_id: str,
        current_capability_id: str,
        current_paired_subject_id: str,
        current_project_id: str,
        current_head: Mapping[str, object],
        current_source_bindings_sha256: str,
    ) -> PreviewReadReservation:
        normalized_method = method.upper()
        expected_prefix = f"/v1/agent/preview-leases/{lease_id}/clips/"
        expected_suffix = "/media"
        if (
            type(lease_id) is not str
            or _LEASE_ID.fullmatch(lease_id) is None
            or type(request_nonce) is not str
            or _REQUEST_NONCE.fullmatch(request_nonce) is None
            or normalized_method not in {"GET", "HEAD"}
            or type(canonical_path) is not str
            or not canonical_path.startswith(expected_prefix)
            or not canonical_path.endswith(expected_suffix)
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview read request is invalid.",
                status=403,
            )
        clip_id = canonical_path[len(expected_prefix) : -len(expected_suffix)]
        if _IDENTIFIER.fullmatch(clip_id) is None:
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview clip path is invalid.",
                status=403,
            )
        now = self._now()
        with self._lock:
            self._purge_locked(now)
            lease = self._leases.get(lease_id)
            if lease is None or lease.expires_at <= now:
                raise AgentPreviewError(
                    "agent_preview_lease_expired",
                    "Timeline preview lease is unavailable or expired.",
                    status=403,
                )
            expected = agent_preview_read_proof(
                lease.secret,
                lease_id=lease_id,
                request_nonce=request_nonce,
                method=normalized_method,
                canonical_path=canonical_path,
                range_header=range_header,
            )
            if not proof or not hmac.compare_digest(proof, expected):
                raise AgentPreviewError(
                    "agent_preview_proof_invalid",
                    "Timeline preview read proof is invalid.",
                    status=401,
                )
            if request_nonce in lease.read_nonces:
                raise AgentPreviewError(
                    "agent_preview_nonce_invalid",
                    "Timeline preview read nonce is missing, expired, or already used.",
                    status=403,
                )
            # A valid proof spends its nonce even if a later definitive scope
            # or source check fails.
            lease.read_nonces.add(request_nonce)
            if (
                lease.database_uuid != current_database_uuid
                or lease.runtime_generation_id != current_runtime_generation_id
                or lease.runtime_authority_epoch != self.runtime_authority_epoch
                or lease.capability_id != current_capability_id
                or lease.paired_subject_id != current_paired_subject_id
            ):
                raise AgentPreviewError(
                    "agent_preview_lease_expired",
                    "Timeline preview runtime authority changed.",
                    status=403,
                )
            if lease.project_id != current_project_id:
                raise AgentPreviewError(
                    "agent_preview_scope_denied",
                    "Timeline preview project scope changed.",
                    status=403,
                )
            if dict(current_head) != lease.observed_head:
                raise AgentPreviewError(
                    "agent_preview_head_changed",
                    "Canonical Timeline head changed before media read.",
                    status=409,
                )
            if current_source_bindings_sha256 != lease.source_bindings_sha256:
                raise AgentPreviewError(
                    "agent_preview_source_changed",
                    "Canonical Timeline source bindings changed before media read.",
                    status=409,
                )
            grant = lease.grants.get(clip_id)
            if grant is None:
                raise AgentPreviewError(
                    "agent_preview_scope_denied",
                    "Timeline preview clip is outside the lease scope.",
                    status=403,
                )
            if grant["status"] != "playable":
                raise AgentPreviewError(
                    "agent_preview_media_unsupported",
                    "Timeline preview media is unsupported by the first playback profile.",
                    status=415,
                )
            try:
                # HEAD transfers no representation bytes.  Permit a range-less
                # metadata probe for a large source while keeping every GET
                # and ranged HEAD bounded by the regular 8 MiB parser.
                byte_range = (
                    TimelinePreviewByteRange(
                        start=0,
                        end=int(grant["size_bytes"]) - 1,
                        length=int(grant["size_bytes"]),
                        status_code=200,
                    )
                    if normalized_method == "HEAD" and range_header is None
                    else require_timeline_preview_range(
                        range_header,
                        source_size=int(grant["size_bytes"]),
                    )
                )
            except TimelinePreviewContractError as exc:
                raise AgentPreviewError(
                    exc.code,
                    exc.message,
                    status=416,
                    details={
                        # These server-only facts are consumed by the closed
                        # media error adapter.  They must never be serialized
                        # as an Agent JSON error body.
                        "source_size": int(grant["size_bytes"]),
                        "mime_type": str(grant["mime_type"]),
                        "opaque_etag": str(grant["opaque_etag"]),
                    },
                ) from exc
            reserved_bytes = 0 if normalized_method == "HEAD" else byte_range.length
            if (
                lease.requests_used >= TIMELINE_PREVIEW_MAX_REQUESTS
                or lease.bytes_used + reserved_bytes
                > TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES
            ):
                raise AgentPreviewError(
                    "agent_preview_lease_expired",
                    "Timeline preview request or byte budget is exhausted.",
                    status=403,
                )
            if lease.active_reads >= TIMELINE_PREVIEW_MAX_CONCURRENCY:
                raise AgentPreviewError(
                    "agent_preview_lease_expired",
                    "Timeline preview concurrency budget is exhausted.",
                    status=403,
                )
            reservation_id = f"preview_read_{secrets.token_hex(16)}"
            lease.requests_used += 1
            lease.bytes_used += reserved_bytes
            lease.active_reads += 1
            self._reservations[reservation_id] = lease_id
            return PreviewReadReservation(
                reservation_id=reservation_id,
                lease_id=lease_id,
                clip_id=clip_id,
                method=normalized_method,
                byte_range=byte_range,
                grant=deepcopy(grant),
            )

    def finish_read(self, reservation_id: str) -> None:
        with self._lock:
            lease_id = self._reservations.pop(reservation_id, None)
            if lease_id is None:
                return
            lease = self._leases.get(lease_id)
            if lease is not None and lease.active_reads > 0:
                lease.active_reads -= 1
            self._purge_locked(self._now())

    def require_active_read(self, reservation_id: str) -> None:
        """Let a streaming adapter stop promptly after drop/revoke/expiry."""

        now = self._now()
        with self._lock:
            self._purge_locked(now)
            lease_id = self._reservations.get(reservation_id)
            lease = self._leases.get(lease_id) if lease_id is not None else None
            if lease is None or lease.expires_at <= now:
                raise AgentPreviewError(
                    "agent_preview_lease_expired",
                    "Timeline preview read reservation is no longer active.",
                    status=403,
                )

    def drop_lease(self, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                return
            for reservation_id, owner in list(self._reservations.items()):
                if owner == lease_id:
                    self._reservations.pop(reservation_id, None)

    def revoke_capability(self, capability_id: str) -> None:
        with self._lock:
            for lease_id, lease in list(self._leases.items()):
                if lease.capability_id == capability_id:
                    self.drop_lease(lease_id)

    def drop_project(self, project_id: str) -> None:
        with self._lock:
            for lease_id, lease in list(self._leases.items()):
                if lease.project_id == project_id:
                    self.drop_lease(lease_id)

    def drop_all(self) -> None:
        with self._lock:
            self._leases.clear()
            self._reservations.clear()
            self._mint_nonces.clear()
            self._mint_nonce_claims.clear()


__all__ = [
    "PREVIEW_LEASE_HEADER",
    "PREVIEW_NONCE_HEADER",
    "PREVIEW_PROOF_HEADER",
    "AgentPreviewError",
    "PreviewLeaseBroker",
    "PreviewLeaseAuthority",
    "PreviewReadReservation",
    "agent_preview_mint_proof",
    "agent_preview_read_proof",
    "canonical_preview_mint_proof_message",
    "canonical_preview_read_proof_message",
]
