"""High-level CLI workflow for native-approved, project-scoped Agent writes."""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Mapping

from memolens_agent_client import (
    AgentApiClient,
    COMMAND_NONCE_ACTIONS,
    DEFAULT_PAIRING_ACTIONS,
    PAIRING_ACTIONS,
    TIMELINE_PREVIEW_ACTION,
)
from memolens_agent_credentials import (
    load_agent_credential,
    public_credential_summary,
    save_agent_credential,
)
from memolens_contracts import MemoLensError


def _required_string(value: object, field: str, *, maximum: int = 256) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise MemoLensError(
            f"MemoLens pairing response field `{field}` is invalid.",
            code="invalid_response",
        )
    return value


def _status(value: object) -> str:
    if value in {"pending", "active", "approved", "denied", "expired", "revoked", "unavailable"}:
        return "active" if value == "approved" else str(value)
    raise MemoLensError(
        "MemoLens pairing response status is invalid.", code="invalid_response"
    )


def _capability_fields(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = payload.get("capability")
    return candidate if isinstance(candidate, Mapping) else payload


class AgentPairingWorkflow:
    """Keep secret-bearing mechanics below the JSON CLI result boundary."""

    def __init__(self, *, base_url: str | None, timeout: float) -> None:
        self.client = AgentApiClient(base_url, timeout=timeout)

    def create(
        self,
        *,
        project_id: str,
        current_head: Mapping[str, Any],
        claimed_client_label: str,
        ttl_seconds: int,
        max_operations: int,
        actions: list[str] | None = None,
    ) -> dict[str, Any]:
        # Keep the historical Blueprint-only default. Canonical Timeline
        # authority is opt-in because requesting it also requires Core to
        # derive and present an exact observed Timeline head for native review.
        selected_actions = list(actions or DEFAULT_PAIRING_ACTIONS)
        if tuple(selected_actions) != tuple(
            action for action in PAIRING_ACTIONS if action in selected_actions
        ) or not selected_actions:
            raise MemoLensError(
                "Agent pairing actions are invalid or out of order.",
                code="invalid_argument",
            )
        observed_head = {
            "revision": current_head.get("revision"),
            "content_sha256": current_head.get("content_sha256"),
        }
        if (
            type(observed_head["revision"]) is not int
            or not isinstance(observed_head["content_sha256"], str)
        ):
            raise MemoLensError(
                "The project does not expose an exact Creative Blueprint head to pair.",
                code="blueprint_head_not_found",
            )
        subject_id = f"agent_{uuid.uuid4().hex}"
        proof_secret = secrets.token_hex(32)
        response = self.client.create_pairing(
            project_id=project_id,
            observed_head=observed_head,
            subject_id=subject_id,
            claimed_client_label=claimed_client_label,
            proof_secret=proof_secret,
            actions=selected_actions,
            ttl_seconds=ttl_seconds,
            max_operations=max_operations,
        )
        pairing_id = _required_string(response.get("pairing_id"), "pairing_id")
        display_code = _required_string(response.get("display_code"), "display_code", maximum=32)
        created_at = _required_string(response.get("created_at"), "created_at", maximum=64)
        expires_at = _required_string(response.get("expires_at"), "expires_at", maximum=64)
        pairing_status = _status(response.get("status"))
        if pairing_status != "pending":
            raise MemoLensError(
                "A new Agent pairing must begin in pending state.",
                code="invalid_response",
            )
        database_uuid = response.get("database_uuid")
        if database_uuid is not None and not isinstance(database_uuid, str):
            raise MemoLensError(
                "MemoLens pairing database identity is invalid.",
                code="invalid_response",
            )
        credential = {
            "object": "memolens.agent_project_credential",
            "schema_version": "1",
            "base_url": self.client.base_url,
            "project_id": project_id,
            "pairing_id": pairing_id,
            "capability_id": None,
            "subject_id": subject_id,
            "claimed_client_label": claimed_client_label,
            "proof_secret": proof_secret,
            "database_uuid": database_uuid,
            "actions": selected_actions,
            "created_at": created_at,
            "expires_at": expires_at,
            "status": "pending",
        }
        summary = save_agent_credential(credential)
        return {
            **summary,
            "display_code": display_code,
            "observed_head": observed_head,
            "next_action": "Open MemoLens Desktop and review the matching pairing code.",
            "native_confirmation_required": True,
            "pairing_secret_exposed": False,
        }

    def status(self, project_id: str) -> dict[str, Any]:
        credential = load_agent_credential(project_id)
        if credential["base_url"] != self.client.base_url:
            raise MemoLensError(
                "The stored Agent pairing belongs to a different loopback endpoint.",
                code="agent_pairing_scope_denied",
            )
        response = self.client.pairing_status(
            credential["pairing_id"],
            proof_secret=credential["proof_secret"],
        )
        pairing_status = _status(response.get("status"))
        capability = _capability_fields(response)
        capability_id = capability.get("capability_id")
        database_uuid = capability.get("database_uuid", response.get("database_uuid"))
        expires_at = capability.get("expires_at", response.get("expires_at"))
        if pairing_status == "active":
            credential["capability_id"] = _required_string(
                capability_id, "capability_id"
            )
            credential["database_uuid"] = _required_string(
                database_uuid, "database_uuid"
            )
            credential["expires_at"] = _required_string(
                expires_at, "expires_at", maximum=64
            )
        credential["status"] = pairing_status
        summary = save_agent_credential(credential)
        safe_response = {
            key: response.get(key)
            for key in (
                "display_code",
                "remaining_operations",
                "max_operations",
                "issued_at",
                "expires_at",
                "reason_code",
            )
            if response.get(key) is not None
        }
        actions = credential.get("actions")
        write_ready = pairing_status == "active" and isinstance(actions, list) and any(
            action in COMMAND_NONCE_ACTIONS for action in actions
        )
        preview_ready = (
            pairing_status == "active"
            and isinstance(actions, list)
            and TIMELINE_PREVIEW_ACTION in actions
        )
        return {
            **summary,
            **safe_response,
            "write_ready": write_ready,
            "preview_ready": preview_ready,
            "next_action": (
                "Use only the actions granted by this active project-scoped pairing."
                if pairing_status == "active" and (write_ready or preview_ready)
                else "Open MemoLens Desktop to approve, or create a new pairing if this one expired."
            ),
        }

    def write(
        self,
        *,
        command: str,
        project_id: str,
        body: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        credential = load_agent_credential(project_id)
        if credential["base_url"] != self.client.base_url:
            raise MemoLensError(
                "The stored Agent pairing belongs to a different loopback endpoint.",
                code="agent_pairing_scope_denied",
            )
        if credential["status"] != "active":
            # A pending credential may have become active since the prior CLI
            # invocation. Refresh once without ever returning its secret.
            self.status(project_id)
            credential = load_agent_credential(project_id)
        if credential["status"] != "active":
            raise MemoLensError(
                "The Agent pairing is not active. Review it in MemoLens Desktop.",
                code="agent_pairing_required",
            )
        action = (
            "blueprint.commit_proposal"
            if command == "commit"
            else "blueprint.restore_revision"
        )
        if action not in credential["actions"]:
            raise MemoLensError(
                "The paired Agent capability does not allow this command.",
                code="agent_pairing_scope_denied",
            )
        response = self.client.blueprint_write(
            command=command,
            project_id=project_id,
            body=body,
            idempotency_key=idempotency_key,
            credential=credential,
        )
        if response.get("object") != "creative_blueprint.command_result":
            raise MemoLensError(
                "MemoLens returned an unexpected Blueprint command result.",
                code="invalid_response",
            )
        # Return the Core result as-is; the contract contains no capability or
        # proof secret.  A defensive scan catches accidental future leakage.
        rendered = str(response)
        if credential["proof_secret"] in rendered:
            raise MemoLensError(
                "MemoLens returned secret-bearing data; the response was suppressed.",
                code="invalid_response",
            )
        return response


__all__ = ["AgentPairingWorkflow", "public_credential_summary"]
