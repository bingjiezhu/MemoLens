"""Durable one-shot provider-egress reservation and send accounting.

The mixin in this module is deliberately separate from provider transports.
It turns a closed consent intent and a closed payload-manifest plan into a
small SQLite state machine.  The caller may build the actual wire body only
after reservation, and must atomically mark the send as started immediately
before handing bytes to a transport.

Only canonical digests and path-free authority bindings are persisted.  Raw
grant tokens, opaque permits, filesystem locators and wire payloads never
enter these tables.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any

from .image_analysis_contract import source_binding_sha256
from .provider_egress_contract import (
    ProviderEgressContractError,
    canonical_json,
    provider_egress_grant_intent_sha256,
    provider_payload_manifest_plan_sha256,
    require_manifest_matches_grant_intent,
    require_valid_provider_egress_grant_intent,
    require_valid_provider_payload_manifest_plan,
)


PROVIDER_EGRESS_GRANT_STATES = frozenset(
    {"active", "reserved", "consumed", "ambiguous", "expired", "revoked"}
)
PROVIDER_EGRESS_MANIFEST_STATES = frozenset(
    {
        "planned",
        "sending",
        "sent",
        "failed_before_send",
        "failed_after_send",
        "ambiguous",
        "cancelled",
    }
)
PROVIDER_EGRESS_AFTER_SEND_STATES = frozenset(
    {"sent", "failed_after_send", "ambiguous"}
)


class ProviderEgressPersistenceError(RuntimeError):
    """Stable fail-closed provider-egress persistence error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str:
        raise ProviderEgressPersistenceError("provider_egress_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderEgressPersistenceError("provider_egress_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ProviderEgressPersistenceError("provider_egress_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _parse_document(value: object, *, error_code: str) -> dict[str, object]:
    if type(value) is not str:
        raise ProviderEgressPersistenceError(error_code)
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderEgressPersistenceError(error_code) from exc
    if type(parsed) is not dict or canonical_json(parsed) != value:
        raise ProviderEgressPersistenceError(error_code)
    return parsed


def _secret_sha256(value: object, *, error_code: str) -> str:
    if type(value) is not str or not 32 <= len(value.encode("utf-8")) <= 4096:
        raise ProviderEgressPersistenceError(error_code)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: object, *, error_code: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProviderEgressPersistenceError(error_code)
    return value


def _row_value(row: Mapping[str, Any], key: str) -> object:
    try:
        return row[key]
    except (KeyError, IndexError) as exc:
        raise ProviderEgressPersistenceError("provider_egress_storage_corrupt") from exc


class ProviderEgressPersistenceMixin:
    """Add durable provider-egress methods to the canonical media repository.

    The host supplies ``transaction(immediate=...)``,
    ``_admit_image_database_identity(expected=None)`` and
    ``_image_meta_in_transaction(connection)``.  Production hosts may use the
    default image-job query below.  Small test hosts can override
    ``_provider_egress_current_image_context_in_transaction`` and
    ``_provider_egress_require_current_source_in_transaction``.
    """

    @staticmethod
    def _provider_egress_current_image_context_in_transaction(
        connection: sqlite3.Connection,
        job_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """SELECT binding.database_uuid,binding.database_device,
                      binding.database_inode,binding.job_id,binding.asset_id,
                      binding.source_id,binding.input_asset_sha256,
                      binding.source_binding_sha256,
                      binding.library_root_id,binding.root_permission_fingerprint,
                      binding.relative_path,binding.source_device,
                      binding.source_inode,binding.observed_size,
                      binding.observed_mtime_ns,binding.observed_ctime_ns,
                      binding.file_identity_sha256,
                      binding.analysis_profile_id,
                      binding.analysis_profile_version,
                      binding.analysis_profile_sha256,binding.request_sha256,
                      job.kind AS job_kind,job.status AS job_status,
                      job.stage AS job_stage,job.attempt AS job_attempt,
                      job.cancel_requested AS job_cancel_requested,
                      authority.runtime_generation,
                      authority.authority_sha256 AS attempt_authority_sha256,
                      state.state AS attempt_state
                 FROM image_analysis_job_bindings binding
                 JOIN media_jobs job ON job.id=binding.job_id
                 JOIN image_analysis_attempt_authorities authority
                   ON authority.job_id=job.id AND authority.attempt=job.attempt
                 JOIN image_analysis_attempt_states state
                   ON state.job_id=authority.job_id
                  AND state.attempt=authority.attempt
                  AND state.authority_sha256=authority.authority_sha256
                WHERE binding.job_id=?""",
            (job_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def _provider_egress_require_current_source_in_transaction(
        self,
        connection: sqlite3.Connection,
        context: Mapping[str, object],
    ) -> None:
        require_source = getattr(self, "_require_bound_image_source_in_transaction", None)
        if not callable(require_source):
            raise ProviderEgressPersistenceError("provider_egress_source_admission_unavailable")
        require_source(connection, context)
        open_source = getattr(self, "_open_admitted_image_source", None)
        if not callable(open_source):
            raise ProviderEgressPersistenceError("provider_egress_source_admission_unavailable")
        try:
            with open_source(
                source_id=str(context["source_id"]),
                expected_asset_id=str(context["asset_id"]),
                expected_input_sha256=str(context["input_asset_sha256"]),
            ) as exact:
                observed = exact.binding
                exact_digest = source_binding_sha256(
                    {
                        "source_id": observed["source_id"],
                        "library_root_id": observed["library_root_id"],
                        "observed_size": observed["observed_size"],
                        "observed_mtime_ns": observed["observed_mtime_ns"],
                        "file_identity_sha256": observed["file_identity_sha256"],
                    }
                )
                for key in (
                    "source_id",
                    "library_root_id",
                    "observed_size",
                    "observed_mtime_ns",
                    "observed_ctime_ns",
                    "source_device",
                    "source_inode",
                    "file_identity_sha256",
                    "root_permission_fingerprint",
                    "asset_id",
                    "input_asset_sha256",
                ):
                    if observed.get(key) != context.get(key):
                        raise ProviderEgressPersistenceError(
                            "provider_egress_source_scope_changed"
                        )
                if exact_digest != context.get("source_binding_sha256"):
                    raise ProviderEgressPersistenceError(
                        "provider_egress_source_scope_changed"
                    )
        except ProviderEgressPersistenceError:
            raise
        except Exception as exc:
            raise ProviderEgressPersistenceError(
                "provider_egress_source_scope_changed"
            ) from exc

    @staticmethod
    def _provider_egress_public_grant(row: Mapping[str, Any]) -> dict[str, object]:
        intent = _parse_document(
            _row_value(row, "grant_intent_json"),
            error_code="provider_egress_grant_corrupt",
        )
        try:
            require_valid_provider_egress_grant_intent(intent)
        except (ProviderEgressContractError, ValueError) as exc:
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt") from exc
        digest = provider_egress_grant_intent_sha256(intent)
        if digest != _row_value(row, "grant_intent_sha256"):
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        state = _row_value(row, "status")
        reserved_manifest_id = _row_value(row, "reserved_manifest_id")
        issued_at = _row_value(row, "issued_at")
        expires_at = _row_value(row, "expires_at")
        reserved_at = _row_value(row, "reserved_at")
        consumed_at = _row_value(row, "consumed_at")
        terminal_at = _row_value(row, "terminal_at")
        binding = ProviderEgressPersistenceMixin._provider_egress_binding(intent)
        if (
            state not in PROVIDER_EGRESS_GRANT_STATES
            or _row_value(row, "id") != intent.get("grant_id")
            or _row_value(row, "database_uuid") != binding.get("database_uuid")
            or issued_at != intent.get("issued_at")
            or expires_at != intent.get("expires_at")
            or not bool(_row_value(row, "single_use"))
        ):
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        if state == "active" and any(
            value is not None
            for value in (reserved_manifest_id, reserved_at, consumed_at, terminal_at)
        ):
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        if state == "reserved" and (
            type(reserved_manifest_id) is not str
            or reserved_at is None
            or consumed_at is not None
            or terminal_at is not None
        ):
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        if state == "consumed" and (
            type(reserved_manifest_id) is not str
            or reserved_at is None
            or consumed_at is None
        ):
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        if state == "ambiguous" and (
            type(reserved_manifest_id) is not str
            or reserved_at is None
            or consumed_at is None
            or terminal_at is None
        ):
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        if state in {"expired", "revoked"} and terminal_at is None:
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        return {
            "id": _row_value(row, "id"),
            "database_uuid": _row_value(row, "database_uuid"),
            "grant_intent": intent,
            "grant_intent_sha256": digest,
            "status": state,
            "reserved_manifest_id": reserved_manifest_id,
            "single_use": True,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "reserved_at": reserved_at,
            "consumed_at": consumed_at,
            "terminal_at": terminal_at,
        }

    @staticmethod
    def _provider_egress_public_manifest(row: Mapping[str, Any]) -> dict[str, object]:
        plan = _parse_document(
            _row_value(row, "manifest_plan_json"),
            error_code="provider_egress_manifest_corrupt",
        )
        try:
            require_valid_provider_payload_manifest_plan(plan)
        except (ProviderEgressContractError, ValueError) as exc:
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt") from exc
        digest = provider_payload_manifest_plan_sha256(plan)
        if digest != _row_value(row, "manifest_plan_sha256"):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        state = _row_value(row, "status")
        manifest_id = _row_value(row, "id")
        grant_id = _row_value(row, "grant_id")
        permit_sha256 = _row_value(row, "permit_sha256")
        bytes_sent = _row_value(row, "bytes_sent")
        failure_code = _row_value(row, "failure_code")
        send_started_at = _row_value(row, "send_started_at")
        completed_at = _row_value(row, "completed_at")
        if (
            state not in PROVIDER_EGRESS_MANIFEST_STATES
            or manifest_id != plan.get("manifest_id")
            or grant_id != plan.get("grant_id")
            or permit_sha256 != plan.get("permit_sha256")
            or plan.get("outcome") != "planned"
        ):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        if state == "planned" and any(
            value is not None
            for value in (bytes_sent, failure_code, send_started_at, completed_at)
        ):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        if state == "sending" and (
            send_started_at is None
            or completed_at is not None
            or bytes_sent is not None
            or failure_code is not None
        ):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        if state == "sent" and (
            send_started_at is None
            or completed_at is None
            or type(bytes_sent) is not int
            or failure_code is not None
        ):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        if state in {"failed_after_send", "ambiguous"} and (
            send_started_at is None
            or completed_at is None
            or type(failure_code) is not str
        ):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        if state in {"failed_before_send", "cancelled"} and (
            send_started_at is not None
            or completed_at is None
            or type(failure_code) is not str
            or bytes_sent is not None
        ):
            raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
        return {
            "id": manifest_id,
            "grant_id": grant_id,
            "manifest_plan": plan,
            "manifest_plan_sha256": digest,
            "status": state,
            "permit_sha256": permit_sha256,
            "bytes_sent": bytes_sent,
            "failure_code": failure_code,
            "created_at": _row_value(row, "created_at"),
            "reserved_at": _row_value(row, "reserved_at"),
            "send_started_at": send_started_at,
            "completed_at": completed_at,
        }

    @staticmethod
    def _provider_egress_binding(intent: Mapping[str, object]) -> Mapping[str, object]:
        binding = intent.get("database_binding")
        if type(binding) is not dict:
            raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
        return binding

    def _provider_egress_require_database_in_transaction(
        self,
        connection: sqlite3.Connection,
        binding: Mapping[str, object],
    ) -> tuple[int, int]:
        device = binding.get("database_device")
        inode = binding.get("database_inode")
        if type(device) is not int or type(inode) is not int:
            raise ProviderEgressPersistenceError("provider_egress_database_scope_changed")
        expected = (device, inode)
        try:
            current = self._admit_image_database_identity(expected)  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)  # type: ignore[attr-defined]
        except Exception as exc:
            if isinstance(exc, ProviderEgressPersistenceError):
                raise
            raise ProviderEgressPersistenceError("provider_egress_database_scope_changed") from exc
        if current != expected or meta.get("database_uuid") != binding.get("database_uuid"):
            raise ProviderEgressPersistenceError("provider_egress_database_scope_changed")
        return expected

    @staticmethod
    def _provider_egress_require_job_context(
        context: Mapping[str, object] | None,
        plan: Mapping[str, object],
    ) -> Mapping[str, object]:
        if context is None:
            raise ProviderEgressPersistenceError("provider_egress_job_missing")
        exact_fields = {
            "database_uuid": "database_uuid",
            "job_id": "job_id",
            "request_sha256": "request_sha256",
            "asset_id": "asset_id",
            "source_id": "source_id",
            "input_asset_sha256": "input_asset_sha256",
            "source_binding_sha256": "source_binding_sha256",
            "runtime_generation": "runtime_generation",
            "attempt": "job_attempt",
            "attempt_authority_sha256": "attempt_authority_sha256",
        }
        binding = ProviderEgressPersistenceMixin._provider_egress_binding(plan)
        for plan_key, context_key in exact_fields.items():
            planned = binding.get(plan_key) if plan_key == "database_uuid" else plan.get(plan_key)
            if planned != context.get(context_key):
                raise ProviderEgressPersistenceError("provider_egress_job_scope_changed")
        profile = plan.get("analysis_profile")
        if type(profile) is not dict or any(
            profile.get(profile_key) != context.get(context_key)
            for profile_key, context_key in (
                ("profile_id", "analysis_profile_id"),
                ("profile_version", "analysis_profile_version"),
                ("profile_sha256", "analysis_profile_sha256"),
            )
        ):
            raise ProviderEgressPersistenceError("provider_egress_job_scope_changed")
        if (
            context.get("job_kind") != "image_analysis"
            or context.get("job_status") != "running"
            or context.get("job_stage") not in {"geocode", "vision", "embedding"}
            or bool(context.get("job_cancel_requested"))
            or context.get("attempt_state") != "claimed"
        ):
            raise ProviderEgressPersistenceError("provider_egress_job_scope_changed")
        required_stage = {
            "image_geocode": "geocode",
            "image_vision": "vision",
            "image_embedding": "embedding",
        }.get(plan.get("capability"))
        if required_stage is None or context.get("job_stage") != required_stage:
            raise ProviderEgressPersistenceError("provider_egress_job_scope_changed")
        database_device = binding.get("database_device")
        database_inode = binding.get("database_inode")
        if (
            database_device != context.get("database_device")
            or database_inode != context.get("database_inode")
        ):
            raise ProviderEgressPersistenceError("provider_egress_database_scope_changed")
        return context

    def record_provider_egress_grant(
        self,
        *,
        grant_intent: Mapping[str, object],
        grant_token: str,
        now: str | None = None,
    ) -> dict[str, object]:
        """Record a sealed intent already issued by the native policy owner.

        This persistence boundary never creates consent or provider authority.
        It accepts the policy owner's closed intent plus opaque bearer token,
        stores only the token digest, and makes later one-shot use auditable.
        """

        intent = dict(grant_intent)
        try:
            require_valid_provider_egress_grant_intent(intent)
        except (ProviderEgressContractError, ValueError) as exc:
            raise ProviderEgressPersistenceError("provider_egress_grant_invalid") from exc
        grant_id = str(intent["grant_id"])
        binding = self._provider_egress_binding(intent)
        digest = provider_egress_grant_intent_sha256(intent)
        token_sha256 = _secret_sha256(
            grant_token,
            error_code="provider_egress_grant_token_invalid",
        )
        issued_at = str(intent["issued_at"])
        expires_at = str(intent["expires_at"])
        timestamp = now or _utc_now_iso()
        if _parse_timestamp(timestamp) < _parse_timestamp(issued_at):
            raise ProviderEgressPersistenceError("provider_egress_grant_not_yet_active")
        if _parse_timestamp(timestamp) >= _parse_timestamp(expires_at):
            raise ProviderEgressPersistenceError("provider_egress_grant_expired")

        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            self._provider_egress_require_database_in_transaction(connection, binding)
            existing = connection.execute(
                "SELECT * FROM provider_egress_grants WHERE id=? OR token_sha256=?",
                (grant_id, token_sha256),
            ).fetchall()
            if existing:
                if len(existing) != 1:
                    raise ProviderEgressPersistenceError("provider_egress_grant_conflict")
                current = self._provider_egress_public_grant(existing[0])
                if (
                    current["id"] != grant_id
                    or current["grant_intent_sha256"] != digest
                    or current["issued_at"] != issued_at
                    or current["expires_at"] != expires_at
                    or _row_value(existing[0], "token_sha256") != token_sha256
                ):
                    raise ProviderEgressPersistenceError("provider_egress_grant_conflict")
                return current
            connection.execute(
                """INSERT INTO provider_egress_grants(
                       id,database_uuid,grant_intent_json,grant_intent_sha256,
                       token_sha256,status,reserved_manifest_id,single_use,
                       issued_at,expires_at,reserved_at,consumed_at,terminal_at)
                   VALUES(?,?,?,?,?,'active',NULL,1,?,?,NULL,NULL,NULL)""",
                (
                    grant_id,
                    binding["database_uuid"],
                    canonical_json(intent),
                    digest,
                    token_sha256,
                    issued_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_egress_grants WHERE id=?",
                (grant_id,),
            ).fetchone()
            if row is None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            return self._provider_egress_public_grant(row)

    def issue_provider_egress_grant(
        self,
        *,
        grant_intent: Mapping[str, object],
        grant_token: str,
        now: str | None = None,
    ) -> dict[str, object]:
        """Compatibility alias for recording externally issued authority.

        Despite the historical method name, this method does not issue,
        broaden or sign authority.  New production callers should use
        :meth:`record_provider_egress_grant`.
        """

        return self.record_provider_egress_grant(
            grant_intent=grant_intent,
            grant_token=grant_token,
            now=now,
        )

    def reserve_provider_egress(
        self,
        *,
        grant_token: str,
        manifest_plan: Mapping[str, object],
        now: str | None = None,
    ) -> dict[str, object]:
        """Atomically reserve a grant and persist its planned payload manifest."""

        plan = dict(manifest_plan)
        try:
            require_valid_provider_payload_manifest_plan(plan)
        except (ProviderEgressContractError, ValueError) as exc:
            raise ProviderEgressPersistenceError("provider_egress_manifest_invalid") from exc
        timestamp = now or _utc_now_iso()
        self.expire_provider_egress_grants(now=timestamp)
        token_sha256 = _secret_sha256(
            grant_token,
            error_code="provider_egress_grant_token_invalid",
        )
        manifest_id = str(plan["manifest_id"])
        plan_digest = provider_payload_manifest_plan_sha256(plan)
        permit_sha256 = _require_sha256(
            plan.get("permit_sha256"),
            error_code="provider_egress_permit_invalid",
        )

        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            grant_row = connection.execute(
                "SELECT * FROM provider_egress_grants WHERE token_sha256=?",
                (token_sha256,),
            ).fetchone()
            if grant_row is None:
                raise ProviderEgressPersistenceError("provider_egress_grant_denied")
            grant = self._provider_egress_public_grant(grant_row)
            intent = grant["grant_intent"]
            if not isinstance(intent, dict):
                raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
            try:
                require_manifest_matches_grant_intent(plan, intent)
            except (ProviderEgressContractError, ValueError) as exc:
                raise ProviderEgressPersistenceError("provider_egress_grant_scope_mismatch") from exc
            binding = self._provider_egress_binding(plan)
            self._provider_egress_require_database_in_transaction(connection, binding)
            if grant["status"] == "expired":
                raise ProviderEgressPersistenceError("provider_egress_grant_expired")
            if grant["status"] != "active" or not grant["single_use"]:
                raise ProviderEgressPersistenceError("provider_egress_grant_unavailable")

            context = self._provider_egress_current_image_context_in_transaction(
                connection,
                str(plan["job_id"]),
            )
            exact_context = self._provider_egress_require_job_context(context, plan)
            self._provider_egress_require_current_source_in_transaction(
                connection,
                exact_context,
            )
            update = connection.execute(
                """UPDATE provider_egress_grants
                      SET status='reserved',reserved_manifest_id=?,reserved_at=?
                    WHERE id=? AND status='active'
                      AND reserved_manifest_id IS NULL AND token_sha256=?""",
                (manifest_id, timestamp, grant["id"], token_sha256),
            )
            if update.rowcount != 1:
                raise ProviderEgressPersistenceError("provider_egress_reservation_conflict")
            try:
                connection.execute(
                    """INSERT INTO provider_egress_manifests(
                           id,grant_id,manifest_plan_json,manifest_plan_sha256,
                           status,permit_sha256,bytes_sent,failure_code,
                           created_at,reserved_at,send_started_at,completed_at)
                       VALUES(?,?,?,?,'planned',?,NULL,NULL,?,?,NULL,NULL)""",
                    (
                        manifest_id,
                        grant["id"],
                        canonical_json(plan),
                        plan_digest,
                        permit_sha256,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ProviderEgressPersistenceError("provider_egress_reservation_conflict") from exc
            row = connection.execute(
                "SELECT * FROM provider_egress_manifests WHERE id=?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            return self._provider_egress_public_manifest(row)

    def fail_provider_egress_before_send(
        self,
        *,
        manifest_id: str,
        permit_sha256: str,
        failure_code: str,
        cancelled: bool = False,
        now: str | None = None,
    ) -> dict[str, object]:
        """Close a planned send and return its unconsumed grant to active."""

        permit_digest = _require_sha256(
            permit_sha256,
            error_code="provider_egress_permit_invalid",
        )
        if (
            type(failure_code) is not str
            or not 1 <= len(failure_code) <= 120
            or failure_code != failure_code.casefold()
            or not failure_code[0].isalpha()
            or not failure_code.replace("_", "a").isalnum()
        ):
            raise ProviderEgressPersistenceError("provider_egress_failure_code_invalid")
        outcome = "cancelled" if cancelled else "failed_before_send"
        timestamp = now or _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            manifest, grant = self._provider_egress_load_pair_in_transaction(
                connection,
                manifest_id=manifest_id,
                permit_sha256=permit_digest,
            )
            if manifest["status"] != "planned" or grant["status"] != "reserved":
                raise ProviderEgressPersistenceError("provider_egress_before_send_conflict")
            self._provider_egress_require_database_in_transaction(
                connection,
                self._provider_egress_binding(manifest["manifest_plan"]),  # type: ignore[arg-type]
            )
            manifest_update = connection.execute(
                """UPDATE provider_egress_manifests
                      SET status=?,failure_code=?,completed_at=?
                    WHERE id=? AND status='planned' AND permit_sha256=?""",
                (outcome, failure_code, timestamp, manifest_id, permit_digest),
            )
            grant_update = connection.execute(
                """UPDATE provider_egress_grants
                      SET status='active',reserved_manifest_id=NULL,
                          reserved_at=NULL
                    WHERE id=? AND status='reserved'
                      AND reserved_manifest_id=?""",
                (grant["id"], manifest_id),
            )
            if manifest_update.rowcount != 1 or grant_update.rowcount != 1:
                raise ProviderEgressPersistenceError("provider_egress_before_send_conflict")
            row = connection.execute(
                "SELECT * FROM provider_egress_manifests WHERE id=?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            return self._provider_egress_public_manifest(row)

    def mark_provider_egress_send_started(
        self,
        *,
        manifest_id: str,
        permit_sha256: str,
        runtime_generation: str,
        expected_attempt: int,
        attempt_authority_sha256: str,
        now: str | None = None,
    ) -> dict[str, object]:
        """Consume the grant and move planned -> sending in one transaction."""

        permit_digest = _require_sha256(
            permit_sha256,
            error_code="provider_egress_permit_invalid",
        )
        authority_digest = _require_sha256(
            attempt_authority_sha256,
            error_code="provider_egress_attempt_authority_invalid",
        )
        timestamp = now or _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            manifest, grant = self._provider_egress_load_pair_in_transaction(
                connection,
                manifest_id=manifest_id,
                permit_sha256=permit_digest,
            )
            plan = manifest["manifest_plan"]
            if not isinstance(plan, dict):
                raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
            if (
                manifest["status"] != "planned"
                or grant["status"] != "reserved"
                or plan.get("runtime_generation") != runtime_generation
                or plan.get("attempt") != expected_attempt
                or plan.get("attempt_authority_sha256") != authority_digest
            ):
                raise ProviderEgressPersistenceError("provider_egress_send_start_conflict")
            if _parse_timestamp(timestamp) >= _parse_timestamp(grant["expires_at"]):
                raise ProviderEgressPersistenceError("provider_egress_grant_expired")
            binding = self._provider_egress_binding(plan)
            self._provider_egress_require_database_in_transaction(connection, binding)
            context = self._provider_egress_current_image_context_in_transaction(
                connection,
                str(plan["job_id"]),
            )
            exact_context = self._provider_egress_require_job_context(context, plan)
            self._provider_egress_require_current_source_in_transaction(
                connection,
                exact_context,
            )
            grant_update = connection.execute(
                """UPDATE provider_egress_grants
                      SET status='consumed',consumed_at=?
                    WHERE id=? AND status='reserved'
                      AND reserved_manifest_id=?""",
                (timestamp, grant["id"], manifest_id),
            )
            manifest_update = connection.execute(
                """UPDATE provider_egress_manifests
                      SET status='sending',send_started_at=?
                    WHERE id=? AND status='planned' AND permit_sha256=?""",
                (timestamp, manifest_id, permit_digest),
            )
            if grant_update.rowcount != 1 or manifest_update.rowcount != 1:
                raise ProviderEgressPersistenceError("provider_egress_send_start_conflict")
            row = connection.execute(
                "SELECT * FROM provider_egress_manifests WHERE id=?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            return self._provider_egress_public_manifest(row)

    def finish_provider_egress_send(
        self,
        *,
        manifest_id: str,
        permit_sha256: str,
        outcome: str,
        bytes_sent: int | None,
        failure_code: str | None = None,
        now: str | None = None,
    ) -> dict[str, object]:
        """Record the certain or uncertain terminal outcome after send start."""

        if outcome not in PROVIDER_EGRESS_AFTER_SEND_STATES:
            raise ProviderEgressPersistenceError("provider_egress_outcome_invalid")
        permit_digest = _require_sha256(
            permit_sha256,
            error_code="provider_egress_permit_invalid",
        )
        if bytes_sent is not None and (type(bytes_sent) is not int or bytes_sent < 0):
            raise ProviderEgressPersistenceError("provider_egress_bytes_sent_invalid")
        if outcome == "sent" and failure_code is not None:
            raise ProviderEgressPersistenceError("provider_egress_failure_code_invalid")
        if outcome != "sent" and (
            type(failure_code) is not str
            or not 1 <= len(failure_code) <= 120
            or failure_code != failure_code.casefold()
            or not failure_code[0].isalpha()
            or not failure_code.replace("_", "a").isalnum()
        ):
            raise ProviderEgressPersistenceError("provider_egress_failure_code_invalid")
        timestamp = now or _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            manifest, grant = self._provider_egress_load_pair_in_transaction(
                connection,
                manifest_id=manifest_id,
                permit_sha256=permit_digest,
            )
            if manifest["status"] != "sending" or grant["status"] != "consumed":
                raise ProviderEgressPersistenceError("provider_egress_send_finish_conflict")
            plan = manifest["manifest_plan"]
            if not isinstance(plan, dict):
                raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
            planned_bytes = plan.get("wire_payload_bytes")
            if type(planned_bytes) is not int:
                raise ProviderEgressPersistenceError("provider_egress_manifest_corrupt")
            if bytes_sent is not None and bytes_sent > planned_bytes:
                raise ProviderEgressPersistenceError("provider_egress_bytes_sent_invalid")
            if outcome == "sent" and bytes_sent != planned_bytes:
                raise ProviderEgressPersistenceError("provider_egress_bytes_sent_invalid")
            manifest_update = connection.execute(
                """UPDATE provider_egress_manifests
                      SET status=?,bytes_sent=?,failure_code=?,completed_at=?
                    WHERE id=? AND status='sending' AND permit_sha256=?""",
                (
                    outcome,
                    bytes_sent,
                    failure_code,
                    timestamp,
                    manifest_id,
                    permit_digest,
                ),
            )
            grant_status = "ambiguous" if outcome == "ambiguous" else "consumed"
            grant_update = connection.execute(
                """UPDATE provider_egress_grants
                      SET status=?,terminal_at=?
                    WHERE id=? AND status='consumed'
                      AND reserved_manifest_id=?""",
                (grant_status, timestamp, grant["id"], manifest_id),
            )
            if manifest_update.rowcount != 1 or grant_update.rowcount != 1:
                raise ProviderEgressPersistenceError("provider_egress_send_finish_conflict")
            row = connection.execute(
                "SELECT * FROM provider_egress_manifests WHERE id=?",
                (manifest_id,),
            ).fetchone()
            if row is None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            return self._provider_egress_public_manifest(row)

    def recover_provider_egress_state(
        self,
        *,
        now: str | None = None,
    ) -> dict[str, int]:
        """Resolve interrupted reservations without ever retrying a started send."""

        timestamp = now or _utc_now_iso()
        recovered_before_send = 0
        recovered_ambiguous = 0
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            inconsistent = connection.execute(
                """SELECT grant_row.id
                     FROM provider_egress_grants grant_row
                     LEFT JOIN provider_egress_manifests manifest
                       ON manifest.id=grant_row.reserved_manifest_id
                    WHERE (grant_row.status='reserved' AND
                           (manifest.id IS NULL OR manifest.grant_id!=grant_row.id OR
                            manifest.status!='planned'))
                       OR (grant_row.status='consumed' AND
                           (manifest.id IS NULL OR manifest.grant_id!=grant_row.id OR
                            manifest.status NOT IN
                              ('sending','sent','failed_after_send')))
                       OR (grant_row.status='ambiguous' AND
                           (manifest.id IS NULL OR manifest.grant_id!=grant_row.id OR
                            manifest.status!='ambiguous'))
                    LIMIT 1"""
            ).fetchone()
            if inconsistent is not None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            rows = connection.execute(
                """SELECT id,permit_sha256
                     FROM provider_egress_manifests
                    WHERE status IN ('planned','sending')
                    ORDER BY created_at,id"""
            ).fetchall()
            for row in rows:
                manifest, grant = self._provider_egress_load_pair_in_transaction(
                    connection,
                    manifest_id=str(row["id"]),
                    permit_sha256=str(row["permit_sha256"]),
                )
                grant_status = grant["status"]
                if manifest["status"] == "planned":
                    if grant_status != "reserved":
                        raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
                    manifest_update = connection.execute(
                        """UPDATE provider_egress_manifests
                              SET status='failed_before_send',
                                  failure_code='process_interrupted_before_send',
                                  completed_at=?
                            WHERE id=? AND status='planned'""",
                        (timestamp, manifest["id"]),
                    )
                    grant_update = connection.execute(
                        """UPDATE provider_egress_grants
                              SET status='active',reserved_manifest_id=NULL,
                                  reserved_at=NULL
                            WHERE id=? AND status='reserved'
                              AND reserved_manifest_id=?""",
                        (grant["id"], manifest["id"]),
                    )
                    if manifest_update.rowcount != 1 or grant_update.rowcount != 1:
                        raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
                    recovered_before_send += 1
                else:
                    if grant_status != "consumed":
                        raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
                    manifest_update = connection.execute(
                        """UPDATE provider_egress_manifests
                              SET status='ambiguous',
                                  failure_code='process_interrupted_after_send_start',
                                  completed_at=?
                            WHERE id=? AND status='sending'""",
                        (timestamp, manifest["id"]),
                    )
                    grant_update = connection.execute(
                        """UPDATE provider_egress_grants
                              SET status='ambiguous',terminal_at=?
                            WHERE id=? AND status='consumed'
                              AND reserved_manifest_id=?""",
                        (timestamp, grant["id"], manifest["id"]),
                    )
                    if manifest_update.rowcount != 1 or grant_update.rowcount != 1:
                        raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
                    recovered_ambiguous += 1
        return {
            "failed_before_send": recovered_before_send,
            "ambiguous": recovered_ambiguous,
        }

    def revoke_provider_egress_grant(
        self,
        grant_id: str,
        *,
        now: str | None = None,
    ) -> dict[str, object]:
        """Revoke an unused grant; a started send cannot be retroactively revoked."""

        timestamp = now or _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                "SELECT * FROM provider_egress_grants WHERE id=?",
                (grant_id,),
            ).fetchone()
            if row is None:
                raise ProviderEgressPersistenceError("provider_egress_grant_missing")
            grant = self._provider_egress_public_grant(row)
            self._provider_egress_require_database_in_transaction(
                connection,
                self._provider_egress_binding(grant["grant_intent"]),  # type: ignore[arg-type]
            )
            if grant["status"] == "reserved":
                manifest_update = connection.execute(
                    """UPDATE provider_egress_manifests
                          SET status='cancelled',failure_code='grant_revoked',
                              completed_at=?
                        WHERE id=? AND status='planned'""",
                    (timestamp, grant["reserved_manifest_id"]),
                )
                if manifest_update.rowcount != 1:
                    raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            elif grant["status"] != "active":
                raise ProviderEgressPersistenceError("provider_egress_grant_unavailable")
            update = connection.execute(
                """UPDATE provider_egress_grants
                      SET status='revoked',terminal_at=?
                    WHERE id=? AND status IN ('active','reserved')""",
                (timestamp, grant_id),
            )
            if update.rowcount != 1:
                raise ProviderEgressPersistenceError("provider_egress_grant_unavailable")
            revised = connection.execute(
                "SELECT * FROM provider_egress_grants WHERE id=?",
                (grant_id,),
            ).fetchone()
            if revised is None:
                raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
            return self._provider_egress_public_grant(revised)

    def expire_provider_egress_grants(
        self,
        *,
        now: str | None = None,
    ) -> dict[str, int]:
        """Expire unused grants, cancelling a reservation that never started."""

        timestamp = now or _utc_now_iso()
        observed_now = _parse_timestamp(timestamp)
        expired_active = 0
        expired_reserved = 0
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            rows = connection.execute(
                """SELECT * FROM provider_egress_grants
                    WHERE status IN ('active','reserved')
                    ORDER BY issued_at,id"""
            ).fetchall()
            for row in rows:
                grant = self._provider_egress_public_grant(row)
                if observed_now < _parse_timestamp(grant["expires_at"]):
                    continue
                intent = grant["grant_intent"]
                if not isinstance(intent, dict):
                    raise ProviderEgressPersistenceError("provider_egress_grant_corrupt")
                self._provider_egress_require_database_in_transaction(
                    connection,
                    self._provider_egress_binding(intent),
                )
                if grant["status"] == "reserved":
                    manifest_update = connection.execute(
                        """UPDATE provider_egress_manifests
                              SET status='cancelled',
                                  failure_code='grant_expired_before_send',
                                  completed_at=?
                            WHERE id=? AND status='planned'""",
                        (timestamp, grant["reserved_manifest_id"]),
                    )
                    if manifest_update.rowcount != 1:
                        raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
                    expired_reserved += 1
                else:
                    expired_active += 1
                grant_update = connection.execute(
                    """UPDATE provider_egress_grants
                          SET status='expired',terminal_at=?
                        WHERE id=? AND status=?""",
                    (timestamp, grant["id"], grant["status"]),
                )
                if grant_update.rowcount != 1:
                    raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
        return {"active": expired_active, "reserved": expired_reserved}

    def _provider_egress_load_pair_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        manifest_id: str,
        permit_sha256: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        manifest_row = connection.execute(
            "SELECT * FROM provider_egress_manifests WHERE id=? AND permit_sha256=?",
            (manifest_id, permit_sha256),
        ).fetchone()
        if manifest_row is None:
            raise ProviderEgressPersistenceError("provider_egress_manifest_missing")
        manifest = self._provider_egress_public_manifest(manifest_row)
        grant_row = connection.execute(
            "SELECT * FROM provider_egress_grants WHERE id=?",
            (manifest["grant_id"],),
        ).fetchone()
        if grant_row is None:
            raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
        grant = self._provider_egress_public_grant(grant_row)
        plan = manifest["manifest_plan"]
        intent = grant["grant_intent"]
        if not isinstance(plan, dict) or not isinstance(intent, dict):
            raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
        try:
            require_manifest_matches_grant_intent(plan, intent)
        except (ProviderEgressContractError, ValueError) as exc:
            raise ProviderEgressPersistenceError("provider_egress_storage_corrupt") from exc
        if (
            plan.get("manifest_id") != manifest["id"]
            or plan.get("grant_id") != grant["id"]
            or plan.get("permit_sha256") != manifest["permit_sha256"]
            or plan.get("grant_intent_sha256") != grant["grant_intent_sha256"]
            or grant["reserved_manifest_id"] != manifest["id"]
        ):
            raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
        if (grant["status"], manifest["status"]) not in {
            ("reserved", "planned"),
            ("consumed", "sending"),
            ("consumed", "sent"),
            ("consumed", "failed_after_send"),
            ("ambiguous", "ambiguous"),
            ("expired", "cancelled"),
            ("revoked", "cancelled"),
        }:
            raise ProviderEgressPersistenceError("provider_egress_storage_corrupt")
        self._provider_egress_require_database_in_transaction(
            connection,
            self._provider_egress_binding(plan),
        )
        return manifest, grant
