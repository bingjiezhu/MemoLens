"""Runtime-only pairing and request-proof broker for scoped Agent writes.

The broker deliberately owns no durable authority.  It keeps the raw proof
secret, pending pairing requests, and one-shot command nonces in bounded
process memory.  Core remains responsible for issuing/revoking durable
capabilities and for consuming a capability in the same transaction as the
typed project mutation.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from core.media_db import canonical_json


AGENT_CAPABILITY_HEADER = "X-MemoLens-Agent-Capability"
AGENT_NONCE_HEADER = "X-MemoLens-Agent-Nonce"
AGENT_PROOF_HEADER = "X-MemoLens-Agent-Proof"
AGENT_STATUS_PROOF_HEADER = "X-MemoLens-Agent-Status-Proof"

PAIRING_ACTIONS = (
    "blueprint.commit_proposal",
    "blueprint.restore_revision",
    "timeline.apply_edit",
    "timeline.apply_structural_edit",
    "timeline.restore_revision",
    "timeline.preview_media",
)
COMMAND_ACTIONS = frozenset(
    {
        "blueprint.commit_proposal",
        "blueprint.restore_revision",
        "timeline.apply_edit",
        "timeline.apply_structural_edit",
        "timeline.restore_revision",
    }
)
DEFAULT_PAIRING_TTL_SECONDS = 15 * 60
MAX_PAIRING_TTL_SECONDS = 30 * 60
DEFAULT_PAIRING_OPERATIONS = 20
MAX_PAIRING_OPERATIONS = 32

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SECRET = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AgentProtocolError(ValueError):
    """Stable transport/pairing error safe to expose through the HTTP API."""

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            f"{field} is invalid.",
            status=400,
        )
    return value


def _label(value: object) -> str:
    if type(value) is not str:
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "claimed_client_label is invalid.",
            status=400,
        )
    normalized = value.strip()
    if not 1 <= len(normalized) <= 120 or any(ord(character) < 32 for character in normalized):
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "claimed_client_label is invalid.",
            status=400,
        )
    return normalized


def _proof_secret(value: object) -> str:
    if type(value) is not str or _SECRET.fullmatch(value) is None:
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "proof_secret must be a 256-bit lowercase hexadecimal secret.",
            status=400,
        )
    return value


def _actions(value: object) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "requested_actions must be a non-empty ordered list.",
            status=400,
        )
    if any(type(item) is not str for item in value):
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "requested_actions contains an invalid action.",
            status=400,
        )
    selected = tuple(value)
    canonical = tuple(action for action in PAIRING_ACTIONS if action in selected)
    if selected != canonical:
        raise AgentProtocolError(
            "agent_pairing_scope_denied",
            "requested_actions must be unique, supported, and in canonical order.",
            status=403,
        )
    return selected


def _bounded_int(
    value: object,
    *,
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            f"{field} must be between {minimum} and {maximum}.",
            status=400,
        )
    return value


def _pairing_request_fields(
    payload: object,
) -> tuple[str, str, str, tuple[str, ...], int, int, str]:
    """Validate the bounded caller-supplied portion before any repository read."""

    required = {
        "object",
        "schema_version",
        "project_id",
        "observed_head",
        "subject_id",
        "claimed_client_label",
        "proof_secret",
        "actions",
        "ttl_seconds",
        "max_operations",
    }
    if type(payload) is not dict or set(payload) != required:
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "Agent pairing intake has an unsupported shape.",
            status=400,
        )
    if (
        payload["object"] != "memolens.agent_pairing_request"
        or payload["schema_version"] != "1"
    ):
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "Agent pairing contract identity is invalid.",
            status=400,
        )
    project_id = _identifier(payload["project_id"], field="project_id")
    subject = _identifier(payload["subject_id"], field="subject_id")
    label = _label(payload["claimed_client_label"])
    actions = _actions(payload["actions"])
    ttl = _bounded_int(
        payload["ttl_seconds"],
        field="ttl_seconds",
        default=DEFAULT_PAIRING_TTL_SECONDS,
        minimum=60,
        maximum=MAX_PAIRING_TTL_SECONDS,
    )
    operations = _bounded_int(
        payload["max_operations"],
        field="max_operations",
        default=DEFAULT_PAIRING_OPERATIONS,
        minimum=1,
        maximum=MAX_PAIRING_OPERATIONS,
    )
    secret = _proof_secret(payload["proof_secret"])
    requested_head = payload["observed_head"]
    if not (
        type(requested_head) is dict
        and set(requested_head) == {"revision", "content_sha256"}
        and type(requested_head["revision"]) is int
        and 1 <= requested_head["revision"] <= 1_000_000
        and type(requested_head["content_sha256"]) is str
        and _SHA256.fullmatch(requested_head["content_sha256"]) is not None
    ):
        raise AgentProtocolError(
            "invalid_agent_pairing_request",
            "observed_head is invalid.",
            status=400,
        )
    return project_id, subject, label, actions, ttl, operations, secret


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def proof_secret_sha256(secret: str) -> str:
    return hashlib.sha256(bytes.fromhex(secret)).hexdigest()


def canonical_agent_proof_message(
    *,
    capability_id: str,
    nonce: str,
    method: str,
    canonical_path: str,
    database_uuid: str,
    project_id: str,
    body_sha256: str,
    idempotency_key: str,
) -> str:
    """Return the frozen MLCAP1 proof message from ML-015-B1."""

    return (
        "MLCAP1\n"
        f"{capability_id}\n{nonce}\n{method.upper()}\n{canonical_path}\n"
        f"{database_uuid}\n{project_id}\n{body_sha256}\n{idempotency_key}"
    )


def agent_request_proof(secret: str, **fields: str) -> str:
    message = canonical_agent_proof_message(**fields)
    return hmac.new(bytes.fromhex(secret), message.encode("utf-8"), hashlib.sha256).hexdigest()


def agent_status_proof(secret: str, *, kind: str, identifier: str) -> str:
    if kind not in {"pairing", "nonce"}:
        raise ValueError("unsupported_agent_status_proof_kind")
    message = f"MLSTATUS1\n{kind}\n{identifier}"
    return hmac.new(bytes.fromhex(secret), message.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class PendingPairing:
    pairing_id: str
    project_id: str
    database_uuid: str
    runtime_authority_epoch: str
    paired_subject_id: str
    claimed_client_label: str
    requested_actions: tuple[str, ...]
    requested_ttl_seconds: int
    requested_max_operations: int
    proof_secret: str = field(repr=False)
    proof_secret_sha256: str = field(repr=False)
    observed_head: dict[str, object]
    pairing_digest: str
    short_code: str
    created_at: datetime
    expires_at: datetime
    status: str = "pending"
    capability_id: str | None = None
    denied_at: datetime | None = None


@dataclass(frozen=True)
class RuntimeCapability:
    capability_id: str
    pairing_id: str
    project_id: str
    database_uuid: str
    runtime_authority_epoch: str
    paired_subject_id: str
    claimed_client_label: str
    actions: tuple[str, ...]
    proof_secret: str = field(repr=False)
    proof_secret_sha256: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    max_operations: int
    status: str = "active"


@dataclass(frozen=True)
class OperationNonce:
    nonce: str
    capability_id: str
    database_uuid: str
    project_id: str
    action: str
    issued_at: datetime
    expires_at: datetime


class AgentPairingBroker:
    """Bounded, thread-safe holder for runtime-only pairing proof material."""

    def __init__(
        self,
        runtime_authority_epoch: str,
        *,
        now: Callable[[], datetime] = _utc_now,
        max_pending: int = 32,
        max_capabilities: int = 64,
        max_nonces: int = 256,
        max_nonces_per_capability: int = 8,
        pending_ttl_seconds: int = 300,
        nonce_ttl_seconds: int = 45,
        preauth_rate_limit: int = 120,
        preauth_rate_window_seconds: int = 60,
        intake_rate_limit: int = 12,
        intake_rate_window_seconds: int = 60,
    ) -> None:
        epoch = _identifier(runtime_authority_epoch, field="runtime_authority_epoch")
        self.runtime_authority_epoch = epoch
        self._now = now
        self._max_pending = max_pending
        self._max_capabilities = max_capabilities
        self._max_nonces = max_nonces
        self._max_nonces_per_capability = max_nonces_per_capability
        self._pending_ttl_seconds = pending_ttl_seconds
        self._nonce_ttl_seconds = nonce_ttl_seconds
        self._preauth_rate_limit = preauth_rate_limit
        self._preauth_rate_window_seconds = preauth_rate_window_seconds
        self._intake_rate_limit = intake_rate_limit
        self._intake_rate_window_seconds = intake_rate_window_seconds
        self._pending: OrderedDict[str, PendingPairing] = OrderedDict()
        self._capabilities: OrderedDict[str, RuntimeCapability] = OrderedDict()
        self._nonces: OrderedDict[str, OperationNonce] = OrderedDict()
        self._preauth_times: deque[datetime] = deque()
        self._intake_times: deque[datetime] = deque()
        self._intake_reservations: set[str] = set()
        self._lock = threading.RLock()

    def _purge_locked(self, now: datetime) -> None:
        for nonce, record in list(self._nonces.items()):
            if record.expires_at <= now:
                self._nonces.pop(nonce, None)
        for capability_id, capability in list(self._capabilities.items()):
            if capability.expires_at <= now or capability.status != "active":
                self._capabilities.pop(capability_id, None)
                for nonce, record in list(self._nonces.items()):
                    if record.capability_id == capability_id:
                        self._nonces.pop(nonce, None)
        for pairing_id, pending in list(self._pending.items()):
            # Keep a processed request briefly so the CLI can observe the
            # outcome; never retain it beyond one pending TTL after expiry.
            retention_deadline = pending.expires_at + timedelta(seconds=self._pending_ttl_seconds)
            if retention_deadline <= now:
                self._pending.pop(pairing_id, None)

    @staticmethod
    def _consume_rate_window_locked(
        attempts: deque[datetime],
        *,
        now: datetime,
        limit: int,
        window_seconds: int,
    ) -> None:
        boundary = now - timedelta(seconds=window_seconds)
        while attempts and attempts[0] <= boundary:
            attempts.popleft()
        if len(attempts) >= limit:
            raise AgentProtocolError(
                "agent_pairing_rate_limited",
                "Too many unauthenticated Agent requests were submitted in this runtime window.",
                status=429,
            )
        attempts.append(now)

    def consume_pre_auth_request(self) -> None:
        """Bound all Agent requests before proof checks or repository work."""

        now = self._now()
        with self._lock:
            self._purge_locked(now)
            self._consume_rate_window_locked(
                self._preauth_times,
                now=now,
                limit=self._preauth_rate_limit,
                window_seconds=self._preauth_rate_window_seconds,
            )

    def reserve_pairing_intake(
        self,
        payload: object,
        *,
        preauth_already_consumed: bool = False,
    ) -> str:
        """Reserve one globally limited queue slot before reading project state."""

        if not preauth_already_consumed:
            self.consume_pre_auth_request()
        _pairing_request_fields(payload)
        now = self._now()
        reservation = f"intake_{secrets.token_urlsafe(18)}"
        with self._lock:
            self._purge_locked(now)
            active_pending = sum(
                1 for item in self._pending.values() if item.status == "pending"
            )
            if active_pending + len(self._intake_reservations) >= self._max_pending:
                raise AgentProtocolError(
                    "agent_pairing_rate_limited",
                    "The runtime pairing queue is full.",
                    status=429,
                )
            self._consume_rate_window_locked(
                self._intake_times,
                now=now,
                limit=self._intake_rate_limit,
                window_seconds=self._intake_rate_window_seconds,
            )
            self._intake_reservations.add(reservation)
        return reservation

    def release_pairing_intake(self, reservation: str) -> None:
        with self._lock:
            self._intake_reservations.discard(reservation)

    def create_pairing(
        self,
        payload: object,
        *,
        database_uuid: str,
        observed_head: Mapping[str, object],
        intake_reservation: str | None = None,
    ) -> dict[str, object]:
        reservation = intake_reservation or self.reserve_pairing_intake(payload)
        try:
            return self._create_pairing_reserved(
                payload,
                database_uuid=database_uuid,
                observed_head=observed_head,
                intake_reservation=reservation,
            )
        finally:
            self.release_pairing_intake(reservation)

    def _create_pairing_reserved(
        self,
        payload: object,
        *,
        database_uuid: str,
        observed_head: Mapping[str, object],
        intake_reservation: str,
    ) -> dict[str, object]:
        reservation = intake_reservation
        project_id, subject, label, actions, ttl, operations, secret = (
            _pairing_request_fields(payload)
        )
        db_uuid = _identifier(database_uuid, field="database_uuid")
        safe_head = {
            key: observed_head.get(key)
            for key in ("revision", "content_sha256", "semantic_sha256", "operation_id")
        }
        if (
            type(safe_head["revision"]) is not int
            or type(safe_head["content_sha256"]) is not str
            or _SHA256.fullmatch(str(safe_head["content_sha256"])) is None
        ):
            raise AgentProtocolError(
                "blueprint_integrity_error",
                "The observed Creative Blueprint head is invalid.",
                status=409,
            )
        requested_head = payload["observed_head"]
        if (
            type(requested_head) is not dict
            or set(requested_head) != {"revision", "content_sha256"}
            or requested_head.get("revision") != safe_head["revision"]
            or requested_head.get("content_sha256") != safe_head["content_sha256"]
        ):
            raise AgentProtocolError(
                "blueprint_head_conflict",
                "The observed Creative Blueprint head is stale or does not match.",
                status=409,
                details={
                    "current": {
                        "revision": safe_head["revision"],
                        "content_sha256": safe_head["content_sha256"],
                    }
                },
            )
        secret_digest = proof_secret_sha256(secret)
        now = self._now()
        pairing_id = f"pair_{secrets.token_urlsafe(18)}"
        digest_payload = {
            "object": "memolens.agent_pairing_request",
            "schema_version": "1",
            "pairing_id": pairing_id,
            "database_uuid": db_uuid,
            "runtime_authority_epoch": self.runtime_authority_epoch,
            "project_id": project_id,
            "paired_subject_id": subject,
            "claimed_client_label": label,
            "vendor_identity_verified": False,
            "user_authority_verified": False,
            "requested_actions": list(actions),
            "requested_ttl_seconds": ttl,
            "requested_max_operations": operations,
            "proof_secret_sha256": secret_digest,
            "observed_head": safe_head,
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=self._pending_ttl_seconds)),
        }
        pairing_digest = sha256_text(canonical_json(digest_payload))
        short_code = f"{pairing_digest[:4]}-{pairing_digest[4:8]}".upper()
        pending = PendingPairing(
            pairing_id=pairing_id,
            project_id=project_id,
            database_uuid=db_uuid,
            runtime_authority_epoch=self.runtime_authority_epoch,
            paired_subject_id=subject,
            claimed_client_label=label,
            requested_actions=actions,
            requested_ttl_seconds=ttl,
            requested_max_operations=operations,
            proof_secret=secret,
            proof_secret_sha256=secret_digest,
            observed_head=safe_head,
            pairing_digest=pairing_digest,
            short_code=short_code,
            created_at=now,
            expires_at=now + timedelta(seconds=self._pending_ttl_seconds),
        )
        with self._lock:
            self._purge_locked(now)
            if reservation not in self._intake_reservations:
                raise AgentProtocolError(
                    "agent_pairing_rate_limited",
                    "The pairing intake reservation is unavailable.",
                    status=429,
                )
            self._intake_reservations.remove(reservation)
            self._pending[pairing_id] = pending
        return self._pairing_status(pending, include_database=True)

    @staticmethod
    def _pairing_status(pending: PendingPairing, *, include_database: bool) -> dict[str, object]:
        value: dict[str, object] = {
            "object": "agent.pairing",
            "schema_version": "1",
            "pairing_id": pending.pairing_id,
            "project_id": pending.project_id,
            "paired_subject_id": pending.paired_subject_id,
            "claimed_client_label": pending.claimed_client_label,
            "vendor_identity_verified": False,
            "user_authority_verified": False,
            "requested_actions": list(pending.requested_actions),
            "requested_ttl_seconds": pending.requested_ttl_seconds,
            "requested_max_operations": pending.requested_max_operations,
            "short_code": pending.short_code,
            "display_code": pending.short_code,
            "observed_head": dict(pending.observed_head),
            "created_at": _iso(pending.created_at),
            "expires_at": _iso(pending.expires_at),
            "status": pending.status,
        }
        if include_database:
            value["database_uuid"] = pending.database_uuid
        if pending.capability_id is not None:
            value["capability_id"] = pending.capability_id
        return value

    def _pending_locked(self, pairing_id: str, *, allow_processed: bool = False) -> PendingPairing:
        pending = self._pending.get(pairing_id)
        if pending is None:
            raise AgentProtocolError(
                "agent_pairing_required",
                "The pairing request is unavailable in this runtime.",
                status=401,
            )
        now = self._now()
        if pending.expires_at <= now and pending.status == "pending":
            expired = replace(pending, status="expired")
            self._pending[pairing_id] = expired
            raise AgentProtocolError(
                "agent_pairing_expired",
                "The pairing request expired.",
                status=403,
            )
        if not allow_processed:
            if pending.status == "rejected":
                raise AgentProtocolError(
                    "agent_pairing_denied",
                    "The pairing request was rejected.",
                    status=403,
                )
            if pending.status != "pending":
                raise AgentProtocolError(
                    "agent_pairing_pending",
                    "The pairing request is no longer pending.",
                    status=409,
                )
        return pending

    def agent_pairing_status(self, pairing_id: str, proof: str) -> dict[str, object]:
        with self._lock:
            self._purge_locked(self._now())
            pending = self._pending_locked(pairing_id, allow_processed=True)
            expected = agent_status_proof(
                pending.proof_secret,
                kind="pairing",
                identifier=pairing_id,
            )
            if not proof or not hmac.compare_digest(proof, expected):
                raise AgentProtocolError(
                    "agent_pairing_proof_invalid",
                    "The pairing status proof is invalid.",
                    status=401,
                )
            if pending.status == "rejected":
                raise AgentProtocolError(
                    "agent_pairing_denied",
                    "The pairing request was rejected.",
                    status=403,
                )
            if pending.status == "expired":
                raise AgentProtocolError(
                    "agent_pairing_expired",
                    "The pairing request expired.",
                    status=403,
                )
            return self._pairing_status(pending, include_database=True)

    def main_list_pairings(self, *, database_uuid: str) -> list[dict[str, object]]:
        with self._lock:
            self._purge_locked(self._now())
            return [
                self._pairing_status(item, include_database=False)
                for item in reversed(self._pending.values())
                if item.database_uuid == database_uuid and item.status == "pending"
            ]

    def main_pending_pairing(
        self,
        pairing_id: str,
        *,
        database_uuid: str,
    ) -> PendingPairing:
        """Return exact pending facts to the main-only Core adapter."""

        with self._lock:
            self._purge_locked(self._now())
            pending = self._pending_locked(pairing_id)
            if pending.database_uuid != database_uuid:
                raise AgentProtocolError(
                    "agent_pairing_scope_denied",
                    "The pairing request belongs to a different database.",
                    status=403,
                )
            return pending

    def activate_capability(
        self,
        pairing_id: str,
        *,
        capability_id: str,
        issued_at: datetime,
        expires_at: datetime,
        max_operations: int,
    ) -> RuntimeCapability:
        with self._lock:
            pending = self._pending_locked(pairing_id, allow_processed=True)
            if pending.status == "approved":
                if pending.capability_id != capability_id:
                    raise AgentProtocolError(
                        "agent_operation_request_mismatch",
                        "The pairing is already bound to another capability.",
                        status=409,
                    )
                existing = self._capabilities.get(capability_id)
                if existing is not None:
                    return existing
            elif pending.status != "pending":
                code = (
                    "agent_pairing_denied"
                    if pending.status == "rejected"
                    else "agent_pairing_expired"
                )
                raise AgentProtocolError(
                    code,
                    "The pairing can no longer activate a capability.",
                    status=403,
                )
            if len(self._capabilities) >= self._max_capabilities:
                raise AgentProtocolError(
                    "agent_pairing_rate_limited",
                    "The runtime capability store is full.",
                    status=429,
                )
            capability = RuntimeCapability(
                capability_id=_identifier(capability_id, field="capability_id"),
                pairing_id=pairing_id,
                project_id=pending.project_id,
                database_uuid=pending.database_uuid,
                runtime_authority_epoch=pending.runtime_authority_epoch,
                paired_subject_id=pending.paired_subject_id,
                claimed_client_label=pending.claimed_client_label,
                actions=pending.requested_actions,
                proof_secret=pending.proof_secret,
                proof_secret_sha256=pending.proof_secret_sha256,
                issued_at=issued_at,
                expires_at=expires_at,
                max_operations=max_operations,
            )
            self._capabilities[capability_id] = capability
            self._pending[pairing_id] = replace(
                pending,
                status="approved",
                capability_id=capability_id,
                expires_at=expires_at,
            )
            return capability

    def require_activation_capacity(self, capability_id: str) -> None:
        """Fail before durable issue if runtime proof material cannot be held."""

        with self._lock:
            self._purge_locked(self._now())
            if (
                capability_id not in self._capabilities
                and len(self._capabilities) >= self._max_capabilities
            ):
                raise AgentProtocolError(
                    "agent_pairing_rate_limited",
                    "The runtime capability store is full.",
                    status=429,
                )

    def reject_pairing(
        self,
        pairing_id: str,
        *,
        database_uuid: str,
    ) -> dict[str, object]:
        # The caller must first validate Core's exact presentation using the
        # main-only channel.  The broker only records the resulting runtime
        # disposition and never creates a second presentation truth.
        pending = self.main_pending_pairing(
            pairing_id,
            database_uuid=database_uuid,
        )
        with self._lock:
            rejected = replace(pending, status="rejected", denied_at=self._now())
            self._pending[pairing_id] = rejected
            return self._pairing_status(rejected, include_database=False)

    def runtime_capability(
        self,
        capability_id: str,
        *,
        database_uuid: str,
        project_id: str | None = None,
        action: str | None = None,
    ) -> RuntimeCapability:
        with self._lock:
            now = self._now()
            self._purge_locked(now)
            capability = self._capabilities.get(capability_id)
            if capability is None:
                raise AgentProtocolError(
                    "agent_pairing_required",
                    "No active runtime pairing exists for this capability.",
                    status=401,
                )
            if capability.expires_at <= now:
                raise AgentProtocolError(
                    "agent_pairing_expired",
                    "The Agent capability expired.",
                    status=403,
                )
            if capability.status == "revoked":
                raise AgentProtocolError(
                    "agent_pairing_revoked",
                    "The Agent capability was revoked.",
                    status=403,
                )
            if (
                capability.database_uuid != database_uuid
                or capability.runtime_authority_epoch != self.runtime_authority_epoch
                or (project_id is not None and capability.project_id != project_id)
                or (action is not None and action not in capability.actions)
            ):
                raise AgentProtocolError(
                    "agent_pairing_scope_denied",
                    "The Agent capability does not cover this database, project, or action.",
                    status=403,
                )
            return capability

    def issue_nonce(
        self,
        capability_id: str,
        *,
        action: str,
        database_uuid: str,
        status_proof: str,
    ) -> dict[str, object]:
        # Preview reads have their own MLPREVIEWMINT1/MLPREVIEWREAD1 nonce
        # domains and budgets.  They must never enter the canonical write
        # operation-nonce store even though they are a valid pairing action.
        if action not in COMMAND_ACTIONS:
            raise AgentProtocolError(
                "agent_pairing_scope_denied",
                "The requested action is unsupported.",
                status=403,
            )
        with self._lock:
            capability = self.runtime_capability(
                capability_id,
                database_uuid=database_uuid,
                action=action,
            )
            expected = agent_status_proof(
                capability.proof_secret,
                kind="nonce",
                identifier=f"{capability_id}:{action}",
            )
            if not status_proof or not hmac.compare_digest(status_proof, expected):
                raise AgentProtocolError(
                    "agent_pairing_proof_invalid",
                    "The nonce request proof is invalid.",
                    status=401,
                )
            outstanding = sum(
                1 for item in self._nonces.values() if item.capability_id == capability_id
            )
            if len(self._nonces) >= self._max_nonces or outstanding >= self._max_nonces_per_capability:
                raise AgentProtocolError(
                    "agent_pairing_rate_limited",
                    "Too many unconsumed Agent operation nonces exist.",
                    status=429,
                )
            now = self._now()
            nonce_value = secrets.token_urlsafe(24)
            nonce = OperationNonce(
                nonce=nonce_value,
                capability_id=capability_id,
                database_uuid=database_uuid,
                project_id=capability.project_id,
                action=action,
                issued_at=now,
                expires_at=now + timedelta(seconds=self._nonce_ttl_seconds),
            )
            self._nonces[nonce_value] = nonce
            return {
                "object": "agent.operation_nonce",
                "schema_version": "1",
                "capability_id": capability_id,
                "nonce": nonce_value,
                "action": action,
                "database_uuid": database_uuid,
                "project_id": capability.project_id,
                "expires_at": _iso(nonce.expires_at),
            }

    def claim_command_proof(
        self,
        *,
        capability_id: str,
        nonce: str,
        proof: str,
        method: str,
        canonical_path: str,
        database_uuid: str,
        project_id: str,
        action: str,
        body_sha256: str,
        idempotency_key: str,
    ) -> RuntimeCapability:
        if action not in COMMAND_ACTIONS:
            raise AgentProtocolError(
                "agent_pairing_scope_denied",
                "The requested action is not a canonical write command.",
                status=403,
            )
        with self._lock:
            capability = self.runtime_capability(
                capability_id,
                database_uuid=database_uuid,
                project_id=project_id,
                action=action,
            )
            record = self._nonces.get(nonce)
            now = self._now()
            if (
                record is None
                or record.expires_at <= now
                or record.capability_id != capability_id
                or record.database_uuid != database_uuid
                or record.project_id != project_id
                or record.action != action
            ):
                raise AgentProtocolError(
                    "agent_operation_nonce_invalid",
                    "The Agent operation nonce is missing, expired, already used, or out of scope.",
                    status=403,
                )
            message = canonical_agent_proof_message(
                capability_id=capability_id,
                nonce=nonce,
                method=method,
                canonical_path=canonical_path,
                database_uuid=database_uuid,
                project_id=project_id,
                body_sha256=body_sha256,
                idempotency_key=idempotency_key,
            )
            expected = hmac.new(
                bytes.fromhex(capability.proof_secret),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not proof or not hmac.compare_digest(proof, expected):
                raise AgentProtocolError(
                    "agent_pairing_proof_invalid",
                    "The Agent operation proof is invalid.",
                    status=401,
                )
            # A valid transport proof spends its nonce before Core dispatch.
            # Definitive durable capability use is still performed by Core in
            # the mutation transaction; a failed command obtains a fresh nonce.
            self._nonces.pop(nonce, None)
            return capability

    def revoke_runtime_capability(self, capability_id: str) -> None:
        with self._lock:
            capability = self._capabilities.pop(capability_id, None)
            if capability is None:
                return
            for nonce, record in list(self._nonces.items()):
                if record.capability_id == capability_id:
                    self._nonces.pop(nonce, None)


__all__ = [
    "AGENT_CAPABILITY_HEADER",
    "AGENT_NONCE_HEADER",
    "AGENT_PROOF_HEADER",
    "AGENT_STATUS_PROOF_HEADER",
    "DEFAULT_PAIRING_OPERATIONS",
    "DEFAULT_PAIRING_TTL_SECONDS",
    "MAX_PAIRING_OPERATIONS",
    "MAX_PAIRING_TTL_SECONDS",
    "COMMAND_ACTIONS",
    "PAIRING_ACTIONS",
    "AgentPairingBroker",
    "AgentProtocolError",
    "PendingPairing",
    "RuntimeCapability",
    "agent_request_proof",
    "agent_status_proof",
    "canonical_agent_proof_message",
    "proof_secret_sha256",
    "sha256_text",
]
