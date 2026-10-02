"""Strict command and read service for canonical hard-cut Timeline drafts.

The service owns orchestration only.  The repository owns the verified SQLite
transaction and permanent receipt, while ``core.timeline_lowering_contract``
owns the deterministic Coverage-to-Timeline derivation.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Mapping

from core.media_db import (
    CanonicalTimelineCommandResult,
    CanonicalTimelineIntegrityError,
    CanonicalTimelineSourceUnavailableError,
    MediaRepository,
    canonical_json,
    new_id,
    utc_now_iso,
)
from core.timeline_lowering_contract import (
    TimelineLoweringContractError,
    canonical_timeline_content_sha256,
    compile_canonical_timeline,
)
from core.timeline_edit_contract import (
    TimelineEditContractError,
    apply_timeline_edit,
)
from core.timeline_structural_edit_contract import (
    TimelineStructuralEditContractError,
    apply_timeline_structural_edit,
)
from core.timeline_restore_contract import (
    TimelineRestoreContractError,
    restore_canonical_timeline,
)
from .coverage import coverage_freshness_in_transaction


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_COMMAND_FIELDS = {
    "expected_blueprint",
    "expected_coverage",
    "expected_timeline_head",
}
_EDIT_COMMAND_FIELDS = _COMMAND_FIELDS | {"edit"}
_STRUCTURAL_EDIT_COMMAND_FIELDS = _COMMAND_FIELDS | {"structural_edit"}
_RESTORE_COMMAND_FIELDS = _COMMAND_FIELDS | {"restore_from"}
_BLUEPRINT_BINDING_FIELDS = {
    "revision",
    "content_sha256",
    "semantic_sha256",
    "operation_id",
}
_COVERAGE_BINDING_FIELDS = {
    "revision",
    "content_sha256",
    "evidence_manifest_sha256",
    "operation_id",
}
_TIMELINE_HEAD_FIELDS = {
    "revision",
    "revision_sha256",
    "timeline_id",
    "timeline_content_sha256",
    "blueprint_binding",
    "coverage_binding",
    "operation_id",
}


class TimelineLoweringServiceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _closed(value: object, fields: set[str], *, code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise TimelineLoweringServiceError(
            code,
            "Canonical Timeline command has an unsupported shape.",
        )
    return value


def _project_id(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise TimelineLoweringServiceError(
            "invalid_project_id",
            "Creative project identity is invalid.",
        )
    return value


def _command_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise TimelineLoweringServiceError(
            "invalid_idempotency_key",
            "A bounded ASCII Idempotency-Key is required.",
        )
    return value


def _database_uuid(value: object) -> str:
    if type(value) is not str or _DATABASE_UUID.fullmatch(value) is None:
        raise TimelineLoweringServiceError(
            "invalid_database_identity",
            "Expected MemoLens database identity is required and invalid.",
        )
    return value


def _binding(
    value: object,
    *,
    fields: set[str],
    semantic: bool,
) -> dict[str, object]:
    row = _closed(value, fields, code="invalid_timeline_command")
    digest_fields = (
        ("content_sha256", "semantic_sha256")
        if semantic
        else ("content_sha256", "evidence_manifest_sha256")
    )
    if (
        type(row["revision"]) is not int
        or not 1 <= row["revision"] <= 1_000_000
        or any(
            type(row[field]) is not str or _SHA256.fullmatch(row[field]) is None
            for field in digest_fields
        )
        or type(row["operation_id"]) is not str
        or _IDENTIFIER.fullmatch(row["operation_id"]) is None
    ):
        raise TimelineLoweringServiceError(
            "invalid_timeline_command",
            "Canonical upstream binding is invalid.",
        )
    return {field: row[field] for field in sorted(fields)}


def _blueprint_binding(value: object) -> dict[str, object]:
    return _binding(value, fields=_BLUEPRINT_BINDING_FIELDS, semantic=True)


def _coverage_binding(value: object) -> dict[str, object]:
    return _binding(value, fields=_COVERAGE_BINDING_FIELDS, semantic=False)


def _timeline_head_binding(value: object) -> dict[str, object]:
    row = _closed(value, _TIMELINE_HEAD_FIELDS, code="invalid_timeline_command")
    if (
        type(row["revision"]) is not int
        or not 1 <= row["revision"] <= 1_000_000
        or any(
            type(row[field]) is not str or _SHA256.fullmatch(row[field]) is None
            for field in (
                "revision_sha256",
                "timeline_content_sha256",
            )
        )
        or any(
            type(row[field]) is not str or _IDENTIFIER.fullmatch(row[field]) is None
            for field in ("timeline_id", "operation_id")
        )
    ):
        raise TimelineLoweringServiceError(
            "invalid_timeline_command",
            "Expected canonical Timeline head is invalid.",
        )
    return {
        "revision": row["revision"],
        "revision_sha256": row["revision_sha256"],
        "timeline_id": row["timeline_id"],
        "timeline_content_sha256": row["timeline_content_sha256"],
        "blueprint_binding": _blueprint_binding(row["blueprint_binding"]),
        "coverage_binding": _coverage_binding(row["coverage_binding"]),
        "operation_id": row["operation_id"],
    }


def _observed_blueprint(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "revision": value["revision"],
        "content_sha256": value["content_sha256"],
        "semantic_sha256": value["semantic_sha256"],
        "operation_id": value["operation_id"],
    }


def _observed_coverage(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "revision": value["revision"],
        "content_sha256": value["content_sha256"],
        "evidence_manifest_sha256": value["evidence_manifest_sha256"],
        "operation_id": value["operation_id"],
    }


def _head_projection(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "revision": value["revision"],
        "revision_sha256": value["revision_sha256"],
        "timeline_id": value["timeline_id"],
        "timeline_content_sha256": value["timeline_content_sha256"],
        "blueprint_binding": value["blueprint_binding"],
        "coverage_binding": value["coverage_binding"],
        "operation_id": value["operation_id"],
    }


def _selected_projection(
    value: Mapping[str, object],
    *,
    head: Mapping[str, object] | None,
) -> dict[str, object]:
    return {
        **_head_projection(value),  # type: ignore[arg-type]
        "is_head": bool(
            head is not None
            and value["revision"] == head["revision"]
            and value["revision_sha256"] == head["revision_sha256"]
        ),
    }


def timeline_workspace_summary_in_transaction(
    repository: MediaRepository,
    connection: sqlite3.Connection,
    *,
    project_id: str,
    blueprint: Mapping[str, object] | None,
    coverage: Mapping[str, object] | None,
    validated_blueprint_ledger: object | None = None,
    validated_timeline_head: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Project the trusted canonical Timeline inside an existing read snapshot."""

    head = (
        dict(validated_timeline_head)
        if validated_timeline_head is not None
        else repository.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
            validated_blueprint_ledger=validated_blueprint_ledger,  # type: ignore[arg-type]
        )
    )
    if head is None:
        return {
            "state": "missing",
            "head": None,
            "blueprint_binding": None,
            "coverage_binding": None,
            "clip_count": 0,
            "source_binding_count": 0,
            "lifecycle": {
                "state": "not_materialized",
                "approval": "not_established",
                "exportable": False,
            },
        }
    freshness = TimelineLoweringService(repository)._freshness_in_transaction(
        connection,
        selected=head,
        blueprint=blueprint,
        coverage=coverage,
    )
    timeline = head.get("timeline")
    source_bindings = head.get("source_bindings")
    if not isinstance(timeline, dict) or not isinstance(source_bindings, list):
        raise CanonicalTimelineIntegrityError(
            "canonical_timeline_workspace_projection_invalid"
        )
    tracks = timeline.get("tracks")
    if (
        type(tracks) is not list
        or len(tracks) != 1
        or not isinstance(tracks[0], dict)
        or type(tracks[0].get("clips")) is not list
    ):
        raise CanonicalTimelineIntegrityError(
            "canonical_timeline_workspace_projection_invalid"
        )
    return {
        "state": freshness["state"],
        "head": _head_projection(head),
        "blueprint_binding": head["blueprint_binding"],
        "coverage_binding": head["coverage_binding"],
        "clip_count": len(tracks[0]["clips"]),
        "source_binding_count": len(source_bindings),
        "lifecycle": {
            "state": "draft",
            "approval": "not_established",
            "exportable": False,
        },
    }


class TimelineLoweringService:
    principal = "desktop_app"
    command_type = "timeline.materialize_first_cut"
    reconcile_command_type = "timeline.reconcile_from_coverage"
    edit_command_type = "timeline.apply_edit"
    structural_edit_command_type = "timeline.apply_structural_edit"
    restore_command_type = "timeline.restore_revision"
    command_version = "1"

    def __init__(self, repository: MediaRepository) -> None:
        self.repository = repository

    def materialize_first_cut(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None = None,
    ) -> CanonicalTimelineCommandResult:
        normalized_project = _project_id(project_id)
        body = _closed(payload, _COMMAND_FIELDS, code="invalid_timeline_command")
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        if body["expected_timeline_head"] is not None:
            raise TimelineLoweringServiceError(
                "invalid_timeline_command",
                "First-cut materialization requires an explicitly empty Timeline head.",
            )
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_materialize_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": None,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._materialize_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                request_sha256=request_sha256,
            )

        return self.repository.execute_canonical_timeline_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type=self.command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def reconcile_from_coverage(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None = None,
    ) -> CanonicalTimelineCommandResult:
        normalized_project = _project_id(project_id)
        body = _closed(payload, _COMMAND_FIELDS, code="invalid_timeline_command")
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_reconcile_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._reconcile_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                request_sha256=request_sha256,
            )

        return self.repository.execute_canonical_timeline_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type=self.reconcile_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def apply_edit(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None = None,
    ) -> CanonicalTimelineCommandResult:
        normalized_project = _project_id(project_id)
        body = _closed(
            payload,
            _EDIT_COMMAND_FIELDS,
            code="invalid_timeline_edit_command",
        )
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        edit = body["edit"]
        if type(edit) is not dict:
            raise TimelineLoweringServiceError(
                "invalid_timeline_edit",
                "Canonical Timeline edit must be one closed object.",
                status=422,
            )
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_apply_edit_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
            "edit": edit,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._apply_edit_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                edit=edit,
                request_sha256=request_sha256,
            )

        return self.repository.execute_canonical_timeline_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type=self.edit_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def apply_paired_edit(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None,
        paired_capability_id: str,
        runtime_authority_epoch: str,
        presented_secret_sha256: str,
    ) -> CanonicalTimelineCommandResult:
        """Apply the B2B3 edit through one durable paired-Agent authority use."""

        normalized_project = _project_id(project_id)
        body = _closed(
            payload,
            _EDIT_COMMAND_FIELDS,
            code="invalid_timeline_edit_command",
        )
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        edit = body["edit"]
        if type(edit) is not dict:
            raise TimelineLoweringServiceError(
                "invalid_timeline_edit",
                "Canonical Timeline edit must be one closed object.",
                status=422,
            )
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_apply_edit_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
            "edit": edit,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._apply_edit_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                edit=edit,
                request_sha256=request_sha256,
            )

        return self.repository.execute_paired_canonical_timeline_command(
            capability_id=paired_capability_id,
            runtime_authority_epoch=runtime_authority_epoch,
            presented_secret_sha256=presented_secret_sha256,
            project_id=normalized_project,
            command_type=self.edit_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_value=request,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def apply_structural_edit(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None = None,
    ) -> CanonicalTimelineCommandResult:
        """Append one separately authorized split/remove Timeline revision."""

        normalized_project = _project_id(project_id)
        body = _closed(
            payload,
            _STRUCTURAL_EDIT_COMMAND_FIELDS,
            code="invalid_timeline_structural_edit_command",
        )
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        structural_edit = body["structural_edit"]
        if type(structural_edit) is not dict:
            raise TimelineLoweringServiceError(
                "invalid_timeline_structural_edit",
                "Canonical Timeline structural edit must be one closed object.",
                status=422,
            )
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_apply_structural_edit_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
            "structural_edit": structural_edit,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._apply_structural_edit_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                structural_edit=structural_edit,
                request_sha256=request_sha256,
            )

        return self.repository.execute_canonical_timeline_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type=self.structural_edit_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def apply_paired_structural_edit(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None,
        paired_capability_id: str,
        runtime_authority_epoch: str,
        presented_secret_sha256: str,
    ) -> CanonicalTimelineCommandResult:
        """Use one exact paired structural capability for split/remove."""

        normalized_project = _project_id(project_id)
        body = _closed(
            payload,
            _STRUCTURAL_EDIT_COMMAND_FIELDS,
            code="invalid_timeline_structural_edit_command",
        )
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        structural_edit = body["structural_edit"]
        if type(structural_edit) is not dict:
            raise TimelineLoweringServiceError(
                "invalid_timeline_structural_edit",
                "Canonical Timeline structural edit must be one closed object.",
                status=422,
            )
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_apply_structural_edit_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
            "structural_edit": structural_edit,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._apply_structural_edit_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                structural_edit=structural_edit,
                request_sha256=request_sha256,
            )

        return self.repository.execute_paired_canonical_timeline_command(
            capability_id=paired_capability_id,
            runtime_authority_epoch=runtime_authority_epoch,
            presented_secret_sha256=presented_secret_sha256,
            project_id=normalized_project,
            command_type=self.structural_edit_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_value=request,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def restore_revision(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None = None,
    ) -> CanonicalTimelineCommandResult:
        """Append one historical Timeline payload as the current head's successor."""

        normalized_project = _project_id(project_id)
        body = _closed(
            payload,
            _RESTORE_COMMAND_FIELDS,
            code="invalid_timeline_restore_command",
        )
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        restore_from = _timeline_head_binding(body["restore_from"])
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_restore_revision_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
            "restore_from": restore_from,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._restore_revision_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                restore_from=restore_from,
                request_sha256=request_sha256,
            )

        return self.repository.execute_canonical_timeline_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type=self.restore_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def restore_paired_revision(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None,
        paired_capability_id: str,
        runtime_authority_epoch: str,
        presented_secret_sha256: str,
    ) -> CanonicalTimelineCommandResult:
        """Use one exact paired capability command for append-only restore."""

        normalized_project = _project_id(project_id)
        body = _closed(
            payload,
            _RESTORE_COMMAND_FIELDS,
            code="invalid_timeline_restore_command",
        )
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_coverage = _coverage_binding(body["expected_coverage"])
        expected_timeline_head = _timeline_head_binding(
            body["expected_timeline_head"]
        )
        restore_from = _timeline_head_binding(body["restore_from"])
        normalized_database_uuid = _database_uuid(expected_database_uuid)
        key = _command_key(idempotency_key)
        request = {
            "object": "memolens.timeline_restore_revision_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_database_uuid": normalized_database_uuid,
            "expected_blueprint": expected_blueprint,
            "expected_coverage": expected_coverage,
            "expected_timeline_head": expected_timeline_head,
            "restore_from": restore_from,
        }
        request_sha256 = hashlib.sha256(
            canonical_json(request).encode("utf-8")
        ).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._restore_revision_mutation(
                connection,
                project_id=normalized_project,
                expected_database_uuid=normalized_database_uuid,
                expected_blueprint=expected_blueprint,
                expected_coverage=expected_coverage,
                expected_timeline_head=expected_timeline_head,
                restore_from=restore_from,
                request_sha256=request_sha256,
            )

        return self.repository.execute_paired_canonical_timeline_command(
            capability_id=paired_capability_id,
            runtime_authority_epoch=runtime_authority_epoch,
            presented_secret_sha256=presented_secret_sha256,
            project_id=normalized_project,
            command_type=self.restore_command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_value=request,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def _materialize_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_database_uuid: str,
        expected_blueprint: dict[str, object],
        expected_coverage: dict[str, object],
        request_sha256: str,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise TimelineLoweringServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project.",
                status=409,
            )
        project = self.repository.get_creative_project_in_transaction(
            connection,
            project_id,
        )
        if project is None:
            raise TimelineLoweringServiceError(
                "project_not_found",
                "Creative project does not exist.",
                status=404,
            )
        if project.get("status") == "archived":
            raise TimelineLoweringServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )

        current = self.repository.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )
        if current is not None:
            raise TimelineLoweringServiceError(
                "timeline_manual_current_conflict",
                "A canonical Timeline already exists and will not be overwritten.",
                status=409,
                details={"current": _head_projection(current)},
            )

        blueprint = self.repository.get_blueprint_head_in_transaction(connection, project_id)
        observed_blueprint = _observed_blueprint(blueprint)
        if observed_blueprint != expected_blueprint:
            raise TimelineLoweringServiceError(
                "blueprint_head_conflict",
                "Creative Blueprint changed before Timeline materialization.",
                status=409,
                details={"current": observed_blueprint},
            )
        coverage = self.repository.get_coverage_head_in_transaction(connection, project_id)
        observed_coverage = _observed_coverage(coverage)
        if observed_coverage != expected_coverage:
            raise TimelineLoweringServiceError(
                "coverage_head_conflict",
                "Coverage Plan changed before Timeline materialization.",
                status=409,
                details={"current": observed_coverage},
            )
        assert blueprint is not None and coverage is not None
        plan = coverage.get("plan")
        document = blueprint.get("blueprint")
        if type(plan) is not dict or type(document) is not dict:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_upstream_document_missing"
            )
        if plan.get("blueprint_binding") != {
            field: expected_blueprint[field]
            for field in ("revision", "content_sha256", "semantic_sha256")
        }:
            raise TimelineLoweringServiceError(
                "coverage_blueprint_conflict",
                "Current Coverage Plan is not bound to the current Creative Blueprint.",
                status=409,
            )
        try:
            resolved_sources = (
                self.repository.resolve_canonical_timeline_source_bindings_in_transaction(
                    connection,
                    plan,
                )
            )
        except CanonicalTimelineSourceUnavailableError as exc:
            code = (
                "coverage_not_lowerable"
                if exc.code == "coverage_not_lowerable"
                else "timeline_source_unavailable"
            )
            raise TimelineLoweringServiceError(
                code,
                (
                    "Coverage Plan cannot be lowered into a canonical first cut."
                    if code == "coverage_not_lowerable"
                    else "A selected Coverage assignment has no current exact source."
                ),
                status=422,
            ) from exc
        coverage_freshness = coverage_freshness_in_transaction(
            self.repository,
            connection,
            selected=coverage,
            blueprint=blueprint,
        )
        if coverage_freshness["state"] != "current":
            raise TimelineLoweringServiceError(
                "coverage_plan_stale_evidence",
                "Coverage Plan evidence changed before Timeline materialization.",
                status=409,
                details={"freshness": coverage_freshness},
            )

        operation_id = new_id("timeline_op")
        try:
            compiled = compile_canonical_timeline(
                project_id=project_id,
                revision=1,
                parent=None,
                blueprint=document,
                blueprint_binding=expected_blueprint,
                coverage_plan=plan,
                coverage_binding=expected_coverage,
                source_bindings=resolved_sources,
            )
        except TimelineLoweringContractError as exc:
            raise TimelineLoweringServiceError(
                "coverage_not_lowerable",
                "Coverage Plan cannot be lowered into a canonical first cut.",
                status=422,
                details={"errors": exc.errors},
            ) from exc
        timeline = compiled["timeline"]
        source_bindings = compiled["source_bindings"]
        assert isinstance(timeline, dict) and isinstance(source_bindings, list)
        timeline_content_sha256 = canonical_timeline_content_sha256(timeline)
        now = utc_now_iso()
        revision = self.repository.append_canonical_timeline_revision(
            connection,
            project_id=project_id,
            revision=1,
            parent_revision=None,
            parent_revision_sha256=None,
            timeline=timeline,
            source_bindings=source_bindings,
            blueprint_binding=expected_blueprint,
            coverage_binding=expected_coverage,
            operation_id=operation_id,
            created_at=now,
        )
        result_head = _head_projection(revision)
        assert result_head is not None
        result = {"kind": "revision_created", "result_head": result_head}
        self.repository.append_canonical_timeline_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            expected_blueprint=expected_blueprint,
            expected_coverage=expected_coverage,
            expected_timeline_head=None,
            request_sha256=request_sha256,
            result_revision=1,
            result_revision_sha256=str(revision["revision_sha256"]),
            result_timeline_content_sha256=timeline_content_sha256,
            result=result,
            created_at=now,
        )
        current_head = self.repository.cas_canonical_timeline_head(
            connection,
            project_id=project_id,
            expected_head=None,
            revision=1,
        )
        if _head_projection(current_head) != result_head:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_head_projection_mismatch"
            )
        response = {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": self.command_type,
            "operation_id": operation_id,
            "result": result,
        }
        return response, 201, operation_id, f"{project_id}:1"

    def _reconcile_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_database_uuid: str,
        expected_blueprint: dict[str, object],
        expected_coverage: dict[str, object],
        expected_timeline_head: dict[str, object],
        request_sha256: str,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise TimelineLoweringServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project.",
                status=409,
            )
        project = self.repository.get_creative_project_in_transaction(
            connection,
            project_id,
        )
        if project is None:
            raise TimelineLoweringServiceError(
                "project_not_found",
                "Creative project does not exist.",
                status=404,
            )
        if project.get("status") == "archived":
            raise TimelineLoweringServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )
        current = self.repository.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )
        observed_timeline_head = _head_projection(current)
        if observed_timeline_head != expected_timeline_head:
            raise TimelineLoweringServiceError(
                "timeline_head_conflict",
                "Canonical Timeline changed before reconciliation.",
                status=409,
                details={"current": observed_timeline_head},
            )
        assert current is not None
        blueprint = self.repository.get_blueprint_head_in_transaction(
            connection,
            project_id,
        )
        observed_blueprint = _observed_blueprint(blueprint)
        if observed_blueprint != expected_blueprint:
            raise TimelineLoweringServiceError(
                "blueprint_head_conflict",
                "Creative Blueprint changed before Timeline reconciliation.",
                status=409,
                details={"current": observed_blueprint},
            )
        coverage = self.repository.get_coverage_head_in_transaction(
            connection,
            project_id,
        )
        observed_coverage = _observed_coverage(coverage)
        if observed_coverage != expected_coverage:
            raise TimelineLoweringServiceError(
                "coverage_head_conflict",
                "Coverage Plan changed before Timeline reconciliation.",
                status=409,
                details={"current": observed_coverage},
            )
        assert blueprint is not None and coverage is not None
        current_freshness = self._freshness_in_transaction(
            connection,
            selected=current,
            blueprint=blueprint,
            coverage=coverage,
        )
        if current_freshness["state"] == "current":
            raise TimelineLoweringServiceError(
                "timeline_already_current",
                "Canonical Timeline already matches the current Coverage Plan.",
                status=409,
                details={"current": expected_timeline_head},
            )
        plan = coverage.get("plan")
        document = blueprint.get("blueprint")
        if type(plan) is not dict or type(document) is not dict:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_upstream_document_missing"
            )
        if plan.get("blueprint_binding") != {
            field: expected_blueprint[field]
            for field in ("revision", "content_sha256", "semantic_sha256")
        }:
            raise TimelineLoweringServiceError(
                "coverage_blueprint_conflict",
                "Current Coverage Plan is not bound to the current Creative Blueprint.",
                status=409,
            )
        coverage_freshness = coverage_freshness_in_transaction(
            self.repository,
            connection,
            selected=coverage,
            blueprint=blueprint,
        )
        if coverage_freshness["state"] != "current":
            raise TimelineLoweringServiceError(
                "coverage_plan_stale_evidence",
                "Coverage Plan must be current before Timeline reconciliation.",
                status=409,
                details={"freshness": coverage_freshness},
            )
        try:
            resolved_sources = (
                self.repository.resolve_canonical_timeline_source_bindings_in_transaction(
                    connection,
                    plan,
                )
            )
        except CanonicalTimelineSourceUnavailableError as exc:
            code = (
                "coverage_not_lowerable"
                if exc.code == "coverage_not_lowerable"
                else "timeline_source_unavailable"
            )
            raise TimelineLoweringServiceError(
                code,
                (
                    "Coverage Plan contains a gap and cannot reconcile a canonical Timeline."
                    if code == "coverage_not_lowerable"
                    else "A selected Coverage assignment has no current exact source."
                ),
                status=422,
            ) from exc

        operation_id = new_id("timeline_op")
        next_revision = int(current["revision"]) + 1
        parent = {
            "revision": int(current["revision"]),
            "content_sha256": str(current["timeline_content_sha256"]),
        }
        try:
            compiled = compile_canonical_timeline(
                project_id=project_id,
                revision=next_revision,
                parent=parent,
                blueprint=document,
                blueprint_binding=expected_blueprint,
                coverage_plan=plan,
                coverage_binding=expected_coverage,
                source_bindings=resolved_sources,
            )
        except TimelineLoweringContractError as exc:
            raise TimelineLoweringServiceError(
                "coverage_not_lowerable",
                "Coverage Plan cannot reconcile a canonical Timeline.",
                status=422,
                details={"errors": exc.errors},
            ) from exc
        timeline = compiled["timeline"]
        source_bindings = compiled["source_bindings"]
        assert isinstance(timeline, dict) and isinstance(source_bindings, list)
        timeline_content_sha256 = canonical_timeline_content_sha256(timeline)
        now = utc_now_iso()
        revision = self.repository.append_canonical_timeline_revision(
            connection,
            project_id=project_id,
            revision=next_revision,
            parent_revision=int(current["revision"]),
            parent_revision_sha256=str(current["revision_sha256"]),
            timeline=timeline,
            source_bindings=source_bindings,
            blueprint_binding=expected_blueprint,
            coverage_binding=expected_coverage,
            operation_id=operation_id,
            created_at=now,
        )
        result_head = _head_projection(revision)
        assert result_head is not None
        result = {"kind": "revision_created", "result_head": result_head}
        self.repository.append_canonical_timeline_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type=self.reconcile_command_type,
            command_version=self.command_version,
            expected_blueprint=expected_blueprint,
            expected_coverage=expected_coverage,
            expected_timeline_head=expected_timeline_head,
            request_sha256=request_sha256,
            result_revision=next_revision,
            result_revision_sha256=str(revision["revision_sha256"]),
            result_timeline_content_sha256=timeline_content_sha256,
            result=result,
            created_at=now,
        )
        current_head = self.repository.cas_canonical_timeline_head(
            connection,
            project_id=project_id,
            expected_head=expected_timeline_head,
            revision=next_revision,
        )
        if _head_projection(current_head) != result_head:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_head_projection_mismatch"
            )
        response = {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": self.reconcile_command_type,
            "operation_id": operation_id,
            "result": result,
        }
        return response, 201, operation_id, f"{project_id}:{next_revision}"

    def _apply_edit_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_database_uuid: str,
        expected_blueprint: dict[str, object],
        expected_coverage: dict[str, object],
        expected_timeline_head: dict[str, object],
        edit: dict[str, object],
        request_sha256: str,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise TimelineLoweringServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project.",
                status=409,
            )
        project = self.repository.get_creative_project_in_transaction(
            connection,
            project_id,
        )
        if project is None:
            raise TimelineLoweringServiceError(
                "project_not_found",
                "Creative project does not exist.",
                status=404,
            )
        if project.get("status") == "archived":
            raise TimelineLoweringServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )
        current = self.repository.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )
        observed_timeline_head = _head_projection(current)
        if observed_timeline_head != expected_timeline_head:
            raise TimelineLoweringServiceError(
                "timeline_head_conflict",
                "Canonical Timeline changed before the edit was saved.",
                status=409,
                details={"current": observed_timeline_head},
            )
        assert current is not None
        blueprint = self.repository.get_blueprint_head_in_transaction(
            connection,
            project_id,
        )
        observed_blueprint = _observed_blueprint(blueprint)
        if observed_blueprint != expected_blueprint:
            raise TimelineLoweringServiceError(
                "blueprint_head_conflict",
                "Creative Blueprint changed before the Timeline edit was saved.",
                status=409,
                details={"current": observed_blueprint},
            )
        coverage = self.repository.get_coverage_head_in_transaction(
            connection,
            project_id,
        )
        observed_coverage = _observed_coverage(coverage)
        if observed_coverage != expected_coverage:
            raise TimelineLoweringServiceError(
                "coverage_head_conflict",
                "Coverage Plan changed before the Timeline edit was saved.",
                status=409,
                details={"current": observed_coverage},
            )
        assert blueprint is not None and coverage is not None
        freshness = self._freshness_in_transaction(
            connection,
            selected=current,
            blueprint=blueprint,
            coverage=coverage,
        )
        if freshness["state"] != "current":
            raise TimelineLoweringServiceError(
                "timeline_not_current",
                "Only the exact current canonical Timeline can be edited.",
                status=409,
                details={"freshness": freshness},
            )
        plan = coverage.get("plan")
        timeline = current.get("timeline")
        source_bindings = current.get("source_bindings")
        if (
            type(plan) is not dict
            or type(timeline) is not dict
            or type(source_bindings) is not list
        ):
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_edit_input_missing"
            )

        replacement_source: dict[str, object] | None = None
        if edit.get("op") == "replace_clip":
            replacement_source = self._resolve_replacement_source_in_transaction(
                connection,
                timeline=timeline,
                coverage_plan=plan,
                edit=edit,
            )
        try:
            edited = apply_timeline_edit(
                parent_timeline=timeline,
                parent_source_bindings=source_bindings,
                coverage_plan=plan,
                edit=edit,
                replacement_source=replacement_source,
            )
        except TimelineEditContractError as exc:
            raise TimelineLoweringServiceError(
                "invalid_timeline_edit",
                "Canonical Timeline edit is invalid.",
                status=422,
                details={"errors": exc.errors},
            ) from exc
        edited_timeline = edited["timeline"]
        edited_sources = edited["source_bindings"]
        assert isinstance(edited_timeline, dict) and isinstance(edited_sources, list)
        next_revision = int(current["revision"]) + 1
        if edited_timeline.get("revision") != next_revision:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_edit_revision_mismatch"
            )
        operation_id = new_id("timeline_op")
        now = utc_now_iso()
        revision = self.repository.append_canonical_timeline_revision(
            connection,
            project_id=project_id,
            revision=next_revision,
            parent_revision=int(current["revision"]),
            parent_revision_sha256=str(current["revision_sha256"]),
            timeline=edited_timeline,
            source_bindings=edited_sources,
            blueprint_binding=expected_blueprint,
            coverage_binding=expected_coverage,
            operation_id=operation_id,
            created_at=now,
        )
        result_head = _head_projection(revision)
        assert result_head is not None
        exact_edit = dict(edit)
        result = {
            "kind": "revision_created",
            "result_head": result_head,
            "edit": exact_edit,
        }
        self.repository.append_canonical_timeline_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type=self.edit_command_type,
            command_version=self.command_version,
            expected_blueprint=expected_blueprint,
            expected_coverage=expected_coverage,
            expected_timeline_head=expected_timeline_head,
            request_sha256=request_sha256,
            result_revision=next_revision,
            result_revision_sha256=str(revision["revision_sha256"]),
            result_timeline_content_sha256=canonical_timeline_content_sha256(
                edited_timeline
            ),
            result=result,
            created_at=now,
        )
        current_head = self.repository.cas_canonical_timeline_head(
            connection,
            project_id=project_id,
            expected_head=expected_timeline_head,
            revision=next_revision,
        )
        if _head_projection(current_head) != result_head:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_head_projection_mismatch"
            )
        response = {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": self.edit_command_type,
            "operation_id": operation_id,
            "result": result,
        }
        return response, 201, operation_id, f"{project_id}:{next_revision}"

    def _apply_structural_edit_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_database_uuid: str,
        expected_blueprint: dict[str, object],
        expected_coverage: dict[str, object],
        expected_timeline_head: dict[str, object],
        structural_edit: dict[str, object],
        request_sha256: str,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise TimelineLoweringServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project.",
                status=409,
            )
        project = self.repository.get_creative_project_in_transaction(
            connection,
            project_id,
        )
        if project is None:
            raise TimelineLoweringServiceError(
                "project_not_found",
                "Creative project does not exist.",
                status=404,
            )
        if project.get("status") == "archived":
            raise TimelineLoweringServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )
        current = self.repository.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )
        observed_timeline_head = _head_projection(current)
        if observed_timeline_head != expected_timeline_head:
            raise TimelineLoweringServiceError(
                "timeline_head_conflict",
                "Canonical Timeline changed before the structural edit was saved.",
                status=409,
                details={"current": observed_timeline_head},
            )
        assert current is not None
        blueprint = self.repository.get_blueprint_head_in_transaction(
            connection,
            project_id,
        )
        observed_blueprint = _observed_blueprint(blueprint)
        if observed_blueprint != expected_blueprint:
            raise TimelineLoweringServiceError(
                "blueprint_head_conflict",
                "Creative Blueprint changed before the Timeline structural edit was saved.",
                status=409,
                details={"current": observed_blueprint},
            )
        coverage = self.repository.get_coverage_head_in_transaction(
            connection,
            project_id,
        )
        observed_coverage = _observed_coverage(coverage)
        if observed_coverage != expected_coverage:
            raise TimelineLoweringServiceError(
                "coverage_head_conflict",
                "Coverage Plan changed before the Timeline structural edit was saved.",
                status=409,
                details={"current": observed_coverage},
            )
        assert blueprint is not None and coverage is not None
        freshness = self._freshness_in_transaction(
            connection,
            selected=current,
            blueprint=blueprint,
            coverage=coverage,
        )
        if freshness["state"] != "current":
            raise TimelineLoweringServiceError(
                "timeline_not_current",
                "Only the exact current canonical Timeline can be structurally edited.",
                status=409,
                details={"freshness": freshness},
            )
        plan = coverage.get("plan")
        timeline = current.get("timeline")
        source_bindings = current.get("source_bindings")
        if (
            type(plan) is not dict
            or type(timeline) is not dict
            or type(source_bindings) is not list
        ):
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_structural_edit_input_missing"
            )
        try:
            edited = apply_timeline_structural_edit(
                parent_timeline=timeline,
                parent_source_bindings=source_bindings,
                coverage_plan=plan,
                edit=structural_edit,
            )
        except TimelineStructuralEditContractError as exc:
            raise TimelineLoweringServiceError(
                "invalid_timeline_structural_edit",
                "Canonical Timeline structural edit is invalid.",
                status=422,
                details={"errors": exc.errors},
            ) from exc
        edited_timeline = edited["timeline"]
        edited_sources = edited["source_bindings"]
        if type(edited_timeline) is not dict or type(edited_sources) is not list:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_structural_edit_result_invalid"
            )
        next_revision = int(current["revision"]) + 1
        if edited_timeline.get("revision") != next_revision:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_structural_edit_revision_mismatch"
            )
        operation_id = new_id("timeline_op")
        now = utc_now_iso()
        revision = self.repository.append_canonical_timeline_revision(
            connection,
            project_id=project_id,
            revision=next_revision,
            parent_revision=int(current["revision"]),
            parent_revision_sha256=str(current["revision_sha256"]),
            timeline=edited_timeline,
            source_bindings=edited_sources,
            blueprint_binding=expected_blueprint,
            coverage_binding=expected_coverage,
            operation_id=operation_id,
            created_at=now,
        )
        result_head = _head_projection(revision)
        assert result_head is not None
        exact_structural_edit = dict(structural_edit)
        result = {
            "kind": "revision_created",
            "result_head": result_head,
            "structural_edit": exact_structural_edit,
        }
        self.repository.append_canonical_timeline_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type=self.structural_edit_command_type,
            command_version=self.command_version,
            expected_blueprint=expected_blueprint,
            expected_coverage=expected_coverage,
            expected_timeline_head=expected_timeline_head,
            request_sha256=request_sha256,
            result_revision=next_revision,
            result_revision_sha256=str(revision["revision_sha256"]),
            result_timeline_content_sha256=canonical_timeline_content_sha256(
                edited_timeline
            ),
            result=result,
            created_at=now,
        )
        current_head = self.repository.cas_canonical_timeline_head(
            connection,
            project_id=project_id,
            expected_head=expected_timeline_head,
            revision=next_revision,
        )
        if _head_projection(current_head) != result_head:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_head_projection_mismatch"
            )
        response = {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": self.structural_edit_command_type,
            "operation_id": operation_id,
            "result": result,
        }
        return response, 201, operation_id, f"{project_id}:{next_revision}"

    def _restore_revision_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_database_uuid: str,
        expected_blueprint: dict[str, object],
        expected_coverage: dict[str, object],
        expected_timeline_head: dict[str, object],
        restore_from: dict[str, object],
        request_sha256: str,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise TimelineLoweringServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project.",
                status=409,
            )
        project = self.repository.get_creative_project_in_transaction(
            connection,
            project_id,
        )
        if project is None:
            raise TimelineLoweringServiceError(
                "project_not_found",
                "Creative project does not exist.",
                status=404,
            )
        if project.get("status") == "archived":
            raise TimelineLoweringServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )

        current = self.repository.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )
        observed_timeline_head = _head_projection(current)
        if observed_timeline_head != expected_timeline_head:
            raise TimelineLoweringServiceError(
                "timeline_head_conflict",
                "Canonical Timeline changed before the historical revision was restored.",
                status=409,
                details={"current": observed_timeline_head},
            )
        assert current is not None
        blueprint = self.repository.get_blueprint_head_in_transaction(
            connection,
            project_id,
        )
        observed_blueprint = _observed_blueprint(blueprint)
        if observed_blueprint != expected_blueprint:
            raise TimelineLoweringServiceError(
                "blueprint_head_conflict",
                "Creative Blueprint changed before the Timeline restore was saved.",
                status=409,
                details={"current": observed_blueprint},
            )
        coverage = self.repository.get_coverage_head_in_transaction(
            connection,
            project_id,
        )
        observed_coverage = _observed_coverage(coverage)
        if observed_coverage != expected_coverage:
            raise TimelineLoweringServiceError(
                "coverage_head_conflict",
                "Coverage Plan changed before the Timeline restore was saved.",
                status=409,
                details={"current": observed_coverage},
            )
        assert blueprint is not None and coverage is not None

        target_revision = int(restore_from["revision"])
        if target_revision >= int(current["revision"]):
            raise TimelineLoweringServiceError(
                "timeline_restore_target_invalid",
                "Restore target must be older than the exact current Timeline head.",
                status=409,
            )
        target = self.repository.get_canonical_timeline_revision_in_transaction(
            connection,
            project_id,
            target_revision,
        )
        if target is None or _head_projection(target) != restore_from:
            raise TimelineLoweringServiceError(
                "timeline_restore_target_invalid",
                "The exact historical Timeline revision is unavailable.",
                status=409,
            )
        if (
            current.get("timeline_id") != target.get("timeline_id")
            or current.get("blueprint_binding") != target.get("blueprint_binding")
            or current.get("coverage_binding") != target.get("coverage_binding")
            or current.get("blueprint_binding") != expected_blueprint
            or current.get("coverage_binding") != expected_coverage
        ):
            raise TimelineLoweringServiceError(
                "timeline_restore_binding_mismatch",
                "Current and historical Timeline revisions must share exact current upstream bindings.",
                status=409,
            )

        current_freshness = self._freshness_in_transaction(
            connection,
            selected=current,
            blueprint=blueprint,
            coverage=coverage,
        )
        target_freshness = self._freshness_in_transaction(
            connection,
            selected=target,
            blueprint=blueprint,
            coverage=coverage,
        )
        if (
            current_freshness["state"] != "current"
            or target_freshness["state"] != "current"
        ):
            raise TimelineLoweringServiceError(
                "timeline_restore_source_stale",
                "Current or historical fixed Timeline sources are no longer exact.",
                status=409,
                details={
                    "current_freshness": current_freshness,
                    "restore_freshness": target_freshness,
                },
            )
        current_timeline = current.get("timeline")
        current_sources = current.get("source_bindings")
        target_timeline = target.get("timeline")
        target_sources = target.get("source_bindings")
        if (
            type(current_timeline) is not dict
            or type(current_sources) is not list
            or type(target_timeline) is not dict
            or type(target_sources) is not list
        ):
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_restore_input_missing"
            )
        try:
            restored = restore_canonical_timeline(
                current_timeline=current_timeline,
                current_source_bindings=current_sources,
                restore_timeline=target_timeline,
                restore_source_bindings=target_sources,
            )
        except TimelineRestoreContractError as exc:
            error = exc.errors[0] if exc.errors else {
                "code": "timeline_restore_target_invalid",
                "message": "Canonical Timeline restore is invalid.",
            }
            raise TimelineLoweringServiceError(
                error["code"],
                error["message"],
                status=409,
                details={"errors": exc.errors},
            ) from exc
        restored_timeline = restored["timeline"]
        restored_sources = restored["source_bindings"]
        if type(restored_timeline) is not dict or type(restored_sources) is not list:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_restore_result_invalid"
            )
        next_revision = int(current["revision"]) + 1
        if restored_timeline.get("revision") != next_revision:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_restore_revision_mismatch"
            )

        operation_id = new_id("timeline_op")
        now = utc_now_iso()
        revision = self.repository.append_canonical_timeline_revision(
            connection,
            project_id=project_id,
            revision=next_revision,
            parent_revision=int(current["revision"]),
            parent_revision_sha256=str(current["revision_sha256"]),
            timeline=restored_timeline,
            source_bindings=restored_sources,
            blueprint_binding=expected_blueprint,
            coverage_binding=expected_coverage,
            operation_id=operation_id,
            created_at=now,
        )
        result_head = _head_projection(revision)
        assert result_head is not None
        result = {
            "kind": "revision_restored",
            "result_head": result_head,
            "restore_from": dict(restore_from),
        }
        self.repository.append_canonical_timeline_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type=self.restore_command_type,
            command_version=self.command_version,
            expected_blueprint=expected_blueprint,
            expected_coverage=expected_coverage,
            expected_timeline_head=expected_timeline_head,
            request_sha256=request_sha256,
            result_revision=next_revision,
            result_revision_sha256=str(revision["revision_sha256"]),
            result_timeline_content_sha256=canonical_timeline_content_sha256(
                restored_timeline
            ),
            result=result,
            created_at=now,
        )
        current_head = self.repository.cas_canonical_timeline_head(
            connection,
            project_id=project_id,
            expected_head=expected_timeline_head,
            revision=next_revision,
        )
        if _head_projection(current_head) != result_head:
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_head_projection_mismatch"
            )
        response = {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": self.restore_command_type,
            "operation_id": operation_id,
            "result": result,
        }
        return response, 201, operation_id, f"{project_id}:{next_revision}"

    def _resolve_replacement_source_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        timeline: Mapping[str, object],
        coverage_plan: Mapping[str, object],
        edit: Mapping[str, object],
    ) -> dict[str, object]:
        tracks = timeline.get("tracks")
        beats = coverage_plan.get("beats")
        manifest = coverage_plan.get("evidence_manifest")
        if (
            type(tracks) is not list
            or len(tracks) != 1
            or not isinstance(tracks[0], Mapping)
            or type(tracks[0].get("clips")) is not list
            or type(beats) is not list
            or type(manifest) is not list
        ):
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_replacement_input_invalid"
            )
        target = next(
            (
                clip
                for clip in tracks[0]["clips"]
                if isinstance(clip, Mapping)
                and clip.get("clip_id") == edit.get("clip_id")
            ),
            None,
        )
        beat = next(
            (
                row
                for row in beats
                if isinstance(row, Mapping)
                and target is not None
                and row.get("beat_id") == target.get("beat_id")
            ),
            None,
        )
        alternatives = beat.get("alternatives") if isinstance(beat, Mapping) else None
        assignment = next(
            (
                row
                for row in alternatives
                if isinstance(row, Mapping)
                and row.get("assignment_id")
                == edit.get("assignment_id")
            ),
            None,
        ) if type(alternatives) is list else None
        evidence_ref = (
            assignment.get("evidence_ref")
            if isinstance(assignment, Mapping)
            else None
        )
        evidence = next(
            (
                row
                for row in manifest
                if isinstance(row, Mapping)
                and row.get("evidence_ref") == evidence_ref
            ),
            None,
        )
        proof = evidence.get("proof") if isinstance(evidence, Mapping) else None
        if (
            target is None
            or assignment is None
            or evidence is None
            or evidence.get("status") != "verified"
            or not isinstance(proof, Mapping)
        ):
            raise TimelineLoweringServiceError(
                "invalid_replacement_candidate",
                "Replacement must name verified alternative evidence from the target Beat.",
                status=422,
            )
        try:
            return self.repository.resolve_canonical_timeline_evidence_source_in_transaction(
                connection,
                evidence_ref=str(evidence_ref),
                proof=proof,
            )
        except CanonicalTimelineSourceUnavailableError as exc:
            raise TimelineLoweringServiceError(
                "timeline_source_unavailable",
                "Replacement evidence has no current exact source.",
                status=422,
            ) from exc

    def read(
        self,
        project_id: str,
        *,
        revision: int | None = None,
    ) -> dict[str, object]:
        normalized_project = _project_id(project_id)
        if revision is not None and (
            type(revision) is not int or not 1 <= revision <= 1_000_000
        ):
            raise TimelineLoweringServiceError(
                "invalid_revision",
                "Canonical Timeline revision is invalid.",
            )
        with self.repository.transaction() as connection:
            project = self.repository.get_creative_project_in_transaction(
                connection,
                normalized_project,
            )
            if project is None:
                raise TimelineLoweringServiceError(
                    "project_not_found",
                    "Creative project does not exist.",
                    status=404,
                )
            head = self.repository.get_trusted_canonical_timeline_head_in_transaction(
                connection,
                normalized_project,
            )
            selected_number = revision if revision is not None else (
                None if head is None else int(head["revision"])
            )
            selected = (
                None
                if selected_number is None
                else (
                    head
                    if head is not None
                    and int(head["revision"]) == selected_number
                    else self.repository.get_trusted_canonical_timeline_revision_in_transaction(
                        connection,
                        normalized_project,
                        selected_number,
                    )
                )
            )
            if revision is not None and selected is None:
                raise TimelineLoweringServiceError(
                    "timeline_not_found",
                    "Canonical Timeline revision does not exist.",
                    status=404,
                )
            if head is not None and selected is None:
                raise CanonicalTimelineIntegrityError(
                    "canonical_timeline_head_revision_missing"
                )
            blueprint = self.repository.get_blueprint_head_in_transaction(
                connection,
                normalized_project,
            )
            coverage = self.repository.get_coverage_head_in_transaction(
                connection,
                normalized_project,
            )
            freshness = self._freshness_in_transaction(
                connection,
                selected=selected,
                blueprint=blueprint,
                coverage=coverage,
            )
            database_uuid = self.repository._database_identity_in_transaction(connection)
        return {
            "object": "canonical_timeline.workspace",
            "schema_version": "1",
            "database_uuid": database_uuid,
            "project_id": normalized_project,
            "head": _head_projection(head),
            "selected_revision": (
                None
                if selected is None
                else _selected_projection(selected, head=head)
            ),
            "freshness": freshness,
            "timeline": None if selected is None else selected["timeline"],
            "source_bindings": (
                [] if selected is None else selected["source_bindings"]
            ),
            "lifecycle": {
                "state": "not_materialized" if selected is None else "draft",
                "approval": "not_established",
                "exportable": False,
            },
        }

    def _freshness_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        selected: Mapping[str, object] | None,
        blueprint: Mapping[str, object] | None,
        coverage: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if selected is None:
            return {"state": "missing", "reasons": []}
        if selected["blueprint_binding"] != _observed_blueprint(blueprint):
            return {
                "state": "stale_blueprint",
                "reasons": ["blueprint_head_changed"],
            }
        if selected["coverage_binding"] != _observed_coverage(coverage):
            return {
                "state": "stale_coverage",
                "reasons": ["coverage_head_changed"],
            }
        source_bindings = selected["source_bindings"]
        if not isinstance(source_bindings, list):
            raise CanonicalTimelineIntegrityError(
                "canonical_timeline_source_bindings_invalid"
            )
        if not self.repository.canonical_timeline_source_bindings_current_in_transaction(
            connection,
            source_bindings,
        ):
            return {
                "state": "stale_source_binding",
                "reasons": ["fixed_source_binding_changed"],
            }
        coverage_freshness = coverage_freshness_in_transaction(
            self.repository,
            connection,
            selected=coverage,
            blueprint=blueprint,
        )
        if coverage_freshness["state"] != "current":
            return {
                "state": "stale_coverage",
                "reasons": [
                    "coverage_plan_stale_evidence"
                    if coverage_freshness["state"] == "stale_evidence"
                    else "coverage_plan_stale_blueprint"
                ],
            }
        return {"state": "current", "reasons": []}
