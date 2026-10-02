"""Canonical, unverified Creative Blueprint proposal command handler."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from core.blueprint_contract import (
    BlueprintContractError,
    blueprint_content_sha256,
    blueprint_evidence_refs,
    blueprint_residual_proofs,
    blueprint_semantic_sha256,
    compile_blueprint_document,
    diff_blueprint_sections,
    require_valid_blueprint_document,
    validate_blueprint_semantic,
)
from core.media_db import (
    BlueprintCommandResult,
    BlueprintHeadConflictError,
    BlueprintIntegrityError,
    BlueprintReceiptConflictError,
    CanonicalDerivativeMaterialError,
    CanonicalTimelineIntegrityError,
    CoverageIntegrityError,
    MediaRepository,
    canonical_json,
    new_id,
    utc_now_iso,
)
from .coverage import coverage_workspace_summary_from_validated_head_in_transaction
from .canonical_export import canonical_export_workspace_summary_in_transaction
from .timeline_lowering import timeline_workspace_summary_in_transaction


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_LEGACY_HISTORY_JSON_VALUE_MAX_BYTES = 1_048_576
_LEGACY_HISTORY_JSON_AGGREGATE_MAX_BYTES = 16 * 1_048_576
_COMMIT_FIELDS = {
    "expected_head",
    "initial_legacy_brief",
    "source_candidate_sha256",
    "semantic",
}
_RESTORE_FIELDS = {"expected_head", "restore_from"}


class BlueprintServiceError(ValueError):
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


@dataclass(frozen=True)
class BlueprintReadResult:
    response: dict[str, object]
    response_status: int = 200


def _safe_head(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "revision": value.get("revision"),
        "content_sha256": value.get("content_sha256"),
        "semantic_sha256": value.get("semantic_sha256"),
        "operation_id": value.get("operation_id"),
    }


def _closed_object(
    value: object,
    *,
    required: set[str],
    code: str,
    message: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != required:
        raise BlueprintServiceError(code, message)
    return value


def _revision_ref(value: object, *, nullable: bool, code: str) -> dict[str, Any] | None:
    if value is None and nullable:
        return None
    row = _closed_object(
        value,
        required={"revision", "content_sha256"},
        code=code,
        message="Blueprint revision precondition is invalid.",
    )
    revision = row["revision"]
    digest = row["content_sha256"]
    if (
        type(revision) is not int
        or not 1 <= revision <= 1_000_000
        or type(digest) is not str
        or _SHA256.fullmatch(digest) is None
    ):
        raise BlueprintServiceError(code, "Blueprint revision precondition is invalid.")
    return {"revision": revision, "content_sha256": digest}


def _candidate_digest(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise BlueprintServiceError(
            "invalid_blueprint_command",
            "Source candidate digest is invalid.",
        )
    return value


def _database_uuid(value: object) -> str:
    if type(value) is not str or _DATABASE_UUID.fullmatch(value) is None:
        raise BlueprintServiceError(
            "invalid_database_identity",
            "Expected MemoLens database identity is invalid.",
        )
    return value


def _command_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise BlueprintServiceError(
            "invalid_idempotency_key",
            "A bounded ASCII Idempotency-Key is required.",
        )
    return value


def _project_id(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise BlueprintServiceError("invalid_project_id", "Creative project identity is invalid.")
    return value


class BlueprintService:
    """Compile proposals into immutable revisions without claiming user approval."""

    principal = "desktop_app"
    command_version = "1"

    def __init__(self, repository: MediaRepository) -> None:
        self.repository = repository

    def commit_proposal(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        paired_capability_id: str | None = None,
        runtime_authority_epoch: str | None = None,
        presented_secret_sha256: str | None = None,
    ) -> BlueprintCommandResult:
        normalized_project = _project_id(project_id)
        body = _closed_object(
            payload,
            required=_COMMIT_FIELDS,
            code="invalid_blueprint_command",
            message="Blueprint proposal command has an unsupported shape.",
        )
        expected_head = _revision_ref(
            body["expected_head"],
            nullable=True,
            code="invalid_blueprint_command",
        )
        initial_legacy_brief = _revision_ref(
            body["initial_legacy_brief"],
            nullable=True,
            code="invalid_blueprint_command",
        )
        source_candidate_sha256 = _candidate_digest(body["source_candidate_sha256"])
        semantic = body["semantic"]
        semantic_errors = validate_blueprint_semantic(semantic)
        if semantic_errors:
            raise BlueprintServiceError(
                "invalid_blueprint_semantic",
                "Creative Blueprint semantic content is invalid.",
                status=422,
                details={"errors": semantic_errors},
            )
        assert isinstance(semantic, dict)
        self._reject_unresolvable_b0_reference_kinds(semantic)
        normalized = {
            "object": "memolens.blueprint_commit_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_head": expected_head,
            "initial_legacy_brief": initial_legacy_brief,
            "source_candidate_sha256": source_candidate_sha256,
            "semantic": semantic,
        }
        request_sha256 = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
        key = _command_key(idempotency_key)

        def mutation(
            connection: sqlite3.Connection,
            command_context=None,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._commit_mutation(
                connection,
                project_id=normalized_project,
                expected_head=expected_head,
                initial_legacy_brief=initial_legacy_brief,
                source_candidate_sha256=source_candidate_sha256,
                semantic=semantic,
                request_sha256=request_sha256,
                command_context=command_context,
            )

        paired_arguments = (
            paired_capability_id,
            runtime_authority_epoch,
            presented_secret_sha256,
        )
        if any(value is not None for value in paired_arguments):
            if not all(type(value) is str and value for value in paired_arguments):
                raise BlueprintServiceError(
                    "agent_pairing_required",
                    "A complete paired Agent command context is required.",
                    status=401,
                )
            return self.repository.execute_paired_blueprint_command(
                capability_id=str(paired_capability_id),
                runtime_authority_epoch=str(runtime_authority_epoch),
                presented_secret_sha256=str(presented_secret_sha256),
                project_id=normalized_project,
                command_type="blueprint.commit_proposal",
                command_version=self.command_version,
                idempotency_key=key,
                request_sha256=request_sha256,
                mutation=mutation,
            )
        return self.repository.execute_blueprint_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type="blueprint.commit_proposal",
            command_version=self.command_version,
            idempotency_key=key,
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
        paired_capability_id: str | None = None,
        runtime_authority_epoch: str | None = None,
        presented_secret_sha256: str | None = None,
    ) -> BlueprintCommandResult:
        normalized_project = _project_id(project_id)
        body = _closed_object(
            payload,
            required=_RESTORE_FIELDS,
            code="invalid_blueprint_command",
            message="Blueprint restore command has an unsupported shape.",
        )
        expected_head = _revision_ref(
            body["expected_head"],
            nullable=False,
            code="invalid_blueprint_command",
        )
        restore_from = _revision_ref(
            body["restore_from"],
            nullable=False,
            code="invalid_blueprint_command",
        )
        assert expected_head is not None and restore_from is not None
        normalized_database_uuid = (
            _database_uuid(expected_database_uuid)
            if expected_database_uuid is not None
            else None
        )
        normalized = {
            "object": "memolens.blueprint_restore_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_head": expected_head,
            "restore_from": restore_from,
        }
        if normalized_database_uuid is not None:
            normalized["expected_database_uuid"] = normalized_database_uuid
        request_sha256 = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
        key = _command_key(idempotency_key)

        def mutation(
            connection: sqlite3.Connection,
            command_context=None,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._restore_mutation(
                connection,
                project_id=normalized_project,
                expected_head=expected_head,
                restore_from=restore_from,
                expected_database_uuid=normalized_database_uuid,
                request_sha256=request_sha256,
                command_context=command_context,
            )

        paired_arguments = (
            paired_capability_id,
            runtime_authority_epoch,
            presented_secret_sha256,
        )
        if any(value is not None for value in paired_arguments):
            if not all(type(value) is str and value for value in paired_arguments):
                raise BlueprintServiceError(
                    "agent_pairing_required",
                    "A complete paired Agent command context is required.",
                    status=401,
                )
            return self.repository.execute_paired_blueprint_command(
                capability_id=str(paired_capability_id),
                runtime_authority_epoch=str(runtime_authority_epoch),
                presented_secret_sha256=str(presented_secret_sha256),
                project_id=normalized_project,
                command_type="blueprint.restore_revision",
                command_version=self.command_version,
                idempotency_key=key,
                request_sha256=request_sha256,
                mutation=mutation,
            )
        return self.repository.execute_blueprint_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type="blueprint.restore_revision",
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def _commit_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_head: dict[str, Any] | None,
        initial_legacy_brief: dict[str, Any] | None,
        source_candidate_sha256: str | None,
        semantic: dict[str, Any],
        request_sha256: str,
        command_context=None,
    ) -> tuple[dict[str, object], int, str, str]:
        self._require_writable_project(connection, project_id)
        current = self.repository.get_blueprint_head_in_transaction(connection, project_id)
        current_document = self._validated_row(current, project_id=project_id) if current else None
        self._require_expected_head(expected_head, current)
        if current is None:
            legacy = self._require_initial_legacy_base(
                connection,
                project_id=project_id,
                requested=initial_legacy_brief,
            )
        else:
            if initial_legacy_brief is not None:
                raise BlueprintServiceError(
                    "invalid_blueprint_command",
                    "Initial legacy binding is only accepted for the first Blueprint revision.",
                )
            assert current_document is not None
            legacy = current_document["lineage"]["initial_legacy_brief"]

        current_semantic = current_document["semantic"] if current_document is not None else None
        changed = diff_blueprint_sections(current_semantic, semantic)
        evidence_manifest = self._require_current_executable_material(
            connection,
            semantic,
            derivative_message=(
                "Successful canonical Export bytes cannot be reused as executable material."
            ),
        )
        operation_id = new_id("op")
        now = utc_now_iso()
        # Proposal identity is semantic.  A repeated semantic proposal is a
        # no-change operation and retains the evidence snapshot frozen into
        # the current revision, but current executable material must still be
        # admitted before recording that operation.  A proof refresh must not
        # create an otherwise empty, unauditable revision; a future typed
        # proof-refresh command would own that effect.
        if current_document is not None and not changed:
            head = _safe_head(current)
            assert head is not None
            return self._append_no_change(
                connection,
                project_id=project_id,
                operation_id=operation_id,
                command_type="blueprint.commit_proposal",
                intent_code="propose_blueprint",
                expected_head=expected_head,
                output_head=head,
                restore_from=None,
                request_sha256=request_sha256,
                now=now,
                command_context=command_context,
            )

        self._require_b0_reference_proofs(connection, semantic)

        revision = int(current["revision"]) + 1 if current is not None else 1
        parent = (
            {
                "revision": int(current["revision"]),
                "content_sha256": str(current["content_sha256"]),
            }
            if current is not None
            else None
        )
        try:
            document = compile_blueprint_document(
                project_id=project_id,
                revision=revision,
                parent=parent,
                created_by_operation_id=operation_id,
                semantic=semantic,
                source_candidate_sha256=source_candidate_sha256,
                initial_legacy_brief=legacy,
                evidence_manifest=evidence_manifest,
            )
        except BlueprintContractError as exc:
            raise BlueprintServiceError(
                "invalid_blueprint_document",
                "Creative Blueprint could not be compiled.",
                status=422,
                details={"errors": exc.errors},
            ) from exc
        content_digest = blueprint_content_sha256(document)
        semantic_digest = blueprint_semantic_sha256(semantic)
        new_head = {
            "revision": revision,
            "content_sha256": content_digest,
            "semantic_sha256": semantic_digest,
            "operation_id": operation_id,
        }
        result_kind = "revision_created"
        result = self._operation_result(result_kind, new_head, changed, restore_from=None)
        precondition = {
            "expected_head": expected_head,
            "initial_legacy_brief": legacy if current is None else None,
        }
        self.repository.append_blueprint_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type="blueprint.commit_proposal",
            intent_code="propose_blueprint",
            precondition=precondition,
            typed_diff=changed,
            input_sha256=request_sha256,
            output_sha256=content_digest,
            result_kind=result_kind,
            result_revision=revision,
            result_content_sha256=content_digest,
            result=result,
            created_at=now,
            command_context=command_context,
        )
        self.repository.append_blueprint_revision(
            connection,
            project_id=project_id,
            revision=revision,
            parent_revision=parent["revision"] if parent else None,
            parent_content_sha256=parent["content_sha256"] if parent else None,
            schema_sha256=str(document["schema_sha256"]),
            blueprint=document,
            content_sha256=content_digest,
            semantic_sha256=semantic_digest,
            source_candidate_sha256=source_candidate_sha256,
            operation_id=operation_id,
            created_at=now,
        )
        self.repository.cas_blueprint_head(
            connection,
            project_id=project_id,
            expected_head=expected_head,
            new_head=new_head,
            updated_at=now,
        )
        self.repository.touch_creative_project(connection, project_id=project_id, updated_at=now)
        response = self._command_response(
            project_id=project_id,
            command_type="blueprint.commit_proposal",
            operation_id=operation_id,
            result=result,
        )
        return response, 201, operation_id, f"{project_id}:{revision}"

    def _restore_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_head: dict[str, Any],
        restore_from: dict[str, Any],
        expected_database_uuid: str | None,
        request_sha256: str,
        command_context=None,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            expected_database_uuid is not None
            and self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise BlueprintServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project before restoring.",
                status=409,
            )
        self._require_writable_project(connection, project_id)
        current = self.repository.get_blueprint_head_in_transaction(connection, project_id)
        if current is None:
            raise BlueprintServiceError("blueprint_not_found", "Creative Blueprint does not exist.", status=404)
        current_document = self._validated_row(current, project_id=project_id)
        self._require_expected_head(expected_head, current)
        target = self.repository.get_blueprint_revision_in_transaction(
            connection,
            project_id,
            int(restore_from["revision"]),
            content_sha256=str(restore_from["content_sha256"]),
        )
        if target is None:
            raise BlueprintServiceError(
                "blueprint_restore_target_not_found",
                "Blueprint restore target does not exist.",
                status=404,
            )
        target_document = self._validated_row(target, project_id=project_id)
        target_semantic = target_document["semantic"]
        changed = diff_blueprint_sections(current_document["semantic"], target_semantic)
        operation_id = new_id("op")
        now = utc_now_iso()
        revision = int(current["revision"]) + 1
        parent = {
            "revision": int(current["revision"]),
            "content_sha256": str(current["content_sha256"]),
        }
        lineage = target_document["lineage"]
        self._require_current_executable_material(
            connection,
            target_semantic,
            derivative_message=(
                "Successful canonical Export bytes cannot be restored as executable material."
            ),
        )
        document = compile_blueprint_document(
            project_id=project_id,
            revision=revision,
            parent=parent,
            created_by_operation_id=operation_id,
            semantic=target_semantic,
            source_candidate_sha256=lineage["source_candidate_sha256"],
            initial_legacy_brief=lineage["initial_legacy_brief"],
            evidence_manifest=target_document["evidence_manifest"],
        )
        content_digest = blueprint_content_sha256(document)
        semantic_digest = blueprint_semantic_sha256(target_semantic)
        new_head = {
            "revision": revision,
            "content_sha256": content_digest,
            "semantic_sha256": semantic_digest,
            "operation_id": operation_id,
        }
        result_kind = "revision_restored"
        result = self._operation_result(result_kind, new_head, changed, restore_from=restore_from)
        self.repository.append_blueprint_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type="blueprint.restore_revision",
            intent_code="restore_blueprint",
            precondition={"expected_head": expected_head, "restore_from": restore_from},
            typed_diff=changed,
            input_sha256=request_sha256,
            output_sha256=content_digest,
            result_kind=result_kind,
            result_revision=revision,
            result_content_sha256=content_digest,
            result=result,
            created_at=now,
            command_context=command_context,
        )
        self.repository.append_blueprint_revision(
            connection,
            project_id=project_id,
            revision=revision,
            parent_revision=parent["revision"],
            parent_content_sha256=parent["content_sha256"],
            schema_sha256=str(document["schema_sha256"]),
            blueprint=document,
            content_sha256=content_digest,
            semantic_sha256=semantic_digest,
            source_candidate_sha256=lineage["source_candidate_sha256"],
            operation_id=operation_id,
            created_at=now,
        )
        self.repository.cas_blueprint_head(
            connection,
            project_id=project_id,
            expected_head=expected_head,
            new_head=new_head,
            updated_at=now,
        )
        self.repository.touch_creative_project(connection, project_id=project_id, updated_at=now)
        response = self._command_response(
            project_id=project_id,
            command_type="blueprint.restore_revision",
            operation_id=operation_id,
            result=result,
        )
        return response, 201, operation_id, f"{project_id}:{revision}"

    def _append_no_change(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        operation_id: str,
        command_type: str,
        intent_code: str,
        expected_head: dict[str, Any] | None,
        output_head: dict[str, object],
        restore_from: dict[str, Any] | None,
        request_sha256: str,
        now: str,
        command_context=None,
    ) -> tuple[dict[str, object], int, str, str]:
        result = self._operation_result("no_change", output_head, [], restore_from=restore_from)
        self.repository.append_blueprint_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            command_type=command_type,
            intent_code=intent_code,
            precondition={"expected_head": expected_head, "restore_from": restore_from},
            typed_diff=[],
            input_sha256=request_sha256,
            output_sha256=str(output_head["content_sha256"]),
            result_kind="no_change",
            result_revision=int(output_head["revision"]),
            result_content_sha256=str(output_head["content_sha256"]),
            result=result,
            created_at=now,
            command_context=command_context,
        )
        response = self._command_response(
            project_id=project_id,
            command_type=command_type,
            operation_id=operation_id,
            result=result,
        )
        return response, 200, operation_id, f"{project_id}:{output_head['revision']}"

    def _require_writable_project(self, connection: sqlite3.Connection, project_id: str) -> None:
        project = self.repository.get_creative_project_in_transaction(connection, project_id)
        if project is None:
            raise BlueprintServiceError("project_not_found", "Creative project does not exist.", status=404)
        if project.get("status") == "archived":
            raise BlueprintServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )

    @staticmethod
    def _reject_unresolvable_b0_reference_kinds(semantic: Mapping[str, Any]) -> None:
        """Reject references whose claimed pin cannot yet be proven by Core.

        B0 resolves local asset/span evidence, project existence, and exact
        Creator Memory revisions in the mutation transaction. External HTTPS
        and inline user text remain visibly unverified source declarations.
        Research snapshots, technique-card revisions, and Wiki generations do
        not yet have a Core-owned proof store, so accepting their IDs would
        make an unverified claim look pinned.
        """

        unsupported: list[dict[str, str]] = []
        references = semantic.get("reference_refs")
        if isinstance(references, list):
            for index, reference in enumerate(references):
                if (
                    isinstance(reference, Mapping)
                    and reference.get("kind") == "research_snapshot"
                ):
                    unsupported.append(
                        {
                            "path": f"/reference_refs/{index}",
                            "reason": "reference_kind_not_resolvable_in_b0",
                        }
                    )
        techniques = semantic.get("technique_refs")
        if isinstance(techniques, list):
            unsupported.extend(
                {
                    "path": f"/technique_refs/{index}",
                    "reason": "technique_revision_not_resolvable_in_b0",
                }
                for index in range(len(techniques))
            )
        bindings = semantic.get("bindings")
        if isinstance(bindings, Mapping):
            if bindings.get("wiki_generation") is not None:
                unsupported.append(
                    {
                        "path": "/bindings/wiki_generation",
                        "reason": "wiki_generation_not_resolvable_in_b0",
                    }
                )
        if unsupported:
            raise BlueprintServiceError(
                "blueprint_reference_not_resolvable",
                "One or more claimed Blueprint reference pins cannot be verified in this release.",
                status=422,
                details={"unsupported": unsupported},
            )

    def _require_b0_reference_proofs(
        self,
        connection: sqlite3.Connection,
        semantic: Mapping[str, Any],
    ) -> None:
        unresolved: list[dict[str, str]] = []
        references = semantic.get("reference_refs")
        if isinstance(references, list):
            for index, reference in enumerate(references):
                if not isinstance(reference, Mapping) or reference.get("kind") != "project":
                    continue
                locator = reference.get("locator")
                exists = connection.execute(
                    "SELECT 1 FROM creative_projects WHERE id=? LIMIT 1",
                    (locator,),
                ).fetchone()
                if exists is None:
                    unresolved.append(
                        {
                            "path": f"/reference_refs/{index}",
                            "reason": "project_reference_not_found",
                        }
                    )
        bindings = semantic.get("bindings")
        creator = bindings.get("creator_context") if isinstance(bindings, Mapping) else None
        if isinstance(creator, Mapping):
            resolved = self.repository.resolve_creator_profile_binding_in_transaction(
                connection,
                creator,
            )
            if resolved is None:
                unresolved.append(
                    {
                        "path": "/bindings/creator_context",
                        "reason": "creator_context_not_found_or_mismatched",
                    }
                )
        if unresolved:
            raise BlueprintServiceError(
                "blueprint_reference_unresolved",
                "One or more Blueprint references could not be proven in this project transaction.",
                status=422,
                details={"unresolved": unresolved},
            )

    @staticmethod
    def _require_verified_residual_manifest(
        manifest: object,
        *,
        residual_proofs: Mapping[str, Mapping[str, object]],
    ) -> None:
        if not residual_proofs:
            return
        rows = {
            str(row.get("evidence_ref")): row
            for row in manifest
            if isinstance(manifest, list) and isinstance(row, Mapping)
        } if isinstance(manifest, list) else {}
        stale = []
        for evidence_ref, proof in residual_proofs.items():
            row = rows.get(evidence_ref)
            expected_digest = hashlib.sha256(
                canonical_json(dict(proof)).encode("utf-8")
            ).hexdigest()
            if (
                row is None
                or row.get("status") != "verified"
                or row.get("proof_sha256") != expected_digest
            ):
                stale.append(evidence_ref)
        if stale:
            raise BlueprintServiceError(
                "blueprint_residual_evidence_stale",
                "Residual material proof is no longer current and exactly resolvable.",
                status=409,
                details={"evidence_refs": sorted(stale)},
            )

    def _require_current_executable_material(
        self,
        connection: sqlite3.Connection,
        semantic: Mapping[str, Any],
        *,
        derivative_message: str,
    ) -> list[dict[str, object]]:
        """Re-admit executable material against one current database snapshot."""

        residual_proofs = blueprint_residual_proofs(semantic)
        executable_evidence_refs = tuple(
            sorted(
                {
                    str(hint["evidence_ref"])
                    for hint in semantic["material_hints"]
                }
            )
        )
        try:
            evidence_manifest = self.repository.resolve_blueprint_evidence_in_transaction(
                connection,
                blueprint_evidence_refs(semantic),
                residual_proofs=residual_proofs,
                executable_evidence_refs=executable_evidence_refs,
            )
        except CanonicalDerivativeMaterialError as exc:
            raise BlueprintServiceError(
                exc.code,
                derivative_message,
                status=409,
                details={"evidence_refs": list(exc.evidence_refs)},
            ) from exc
        self._require_verified_residual_manifest(
            evidence_manifest,
            residual_proofs=residual_proofs,
        )
        return evidence_manifest

    def _require_initial_legacy_base(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        requested: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if requested is None:
            raise BlueprintServiceError(
                "blueprint_initial_base_required",
                "The first Blueprint revision requires an exact legacy brief base.",
                status=409,
            )
        latest = self.repository.get_latest_creative_brief_in_transaction(connection, project_id)
        if latest is None:
            raise BlueprintServiceError(
                "blueprint_initial_base_unavailable",
                "The initial legacy brief is unavailable.",
                status=409,
            )
        current = {
            "revision": latest.get("revision"),
            "content_sha256": latest.get("content_sha256"),
        }
        if requested != current:
            raise BlueprintServiceError(
                "blueprint_initial_base_conflict",
                "The initial legacy brief base is stale or does not match.",
                status=409,
                details={"current": current},
            )
        return requested

    @staticmethod
    def _require_expected_head(
        expected: dict[str, Any] | None,
        current: Mapping[str, object] | None,
    ) -> None:
        current_ref = (
            {
                "revision": current.get("revision"),
                "content_sha256": current.get("content_sha256"),
            }
            if current is not None
            else None
        )
        if expected != current_ref:
            raise BlueprintHeadConflictError(_safe_head(current))

    @staticmethod
    def _validated_row(
        row: Mapping[str, object],
        *,
        project_id: str,
    ) -> dict[str, Any]:
        document = row.get("blueprint")
        if not isinstance(document, dict):
            document = row.get("document")
        try:
            return require_valid_blueprint_document(
                document,
                expected_project_id=project_id,
                expected_revision=row.get("revision"),
                expected_content_sha256=row.get("content_sha256"),
                expected_operation_id=row.get("operation_id"),
            )
        except BlueprintContractError as exc:
            raise BlueprintIntegrityError("Persisted Creative Blueprint integrity validation failed.") from exc

    @staticmethod
    def _operation_result(
        kind: str,
        head: Mapping[str, object],
        changed: list[dict[str, object]],
        *,
        restore_from: dict[str, Any] | None,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "result_head": dict(head),
            "changed_sections": [str(item["section"]) for item in changed],
            "restore_from": restore_from,
        }

    @staticmethod
    def _command_response(
        *,
        project_id: str,
        command_type: str,
        operation_id: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        return {
            "object": "creative_blueprint.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": command_type,
            "operation_id": operation_id,
            "result": result,
            "authority": {"state": "unverified", "verified": False},
            "ledger": {
                "coverage_scope": "creative_blueprint",
                "complete_project_history": False,
            },
        }

    def get(self, project_id: str, *, revision: int | None = None) -> BlueprintReadResult:
        normalized_project = _project_id(project_id)
        if revision is not None and (
            type(revision) is not int or not 1 <= revision <= 1_000_000
        ):
            raise BlueprintServiceError("invalid_revision", "Blueprint revision is invalid.")
        with self.repository.transaction() as connection:
            database_uuid = self.repository._database_identity_in_transaction(connection)
            if (
                self.repository.get_creative_project_in_transaction(
                    connection,
                    normalized_project,
                )
                is None
            ):
                raise BlueprintServiceError(
                    "project_not_found",
                    "Creative project does not exist.",
                    status=404,
                )
            snapshot = self.repository.read_blueprint_snapshot_in_transaction(
                connection,
                normalized_project,
                revision=revision,
            )
            row = snapshot["revision"]
            if row is None:
                raise BlueprintServiceError(
                    "blueprint_not_found",
                    "Creative Blueprint does not exist.",
                    status=404,
                )
            assert isinstance(row, dict)
            document = self._validated_row(row, project_id=normalized_project)
            if revision is None:
                self._require_current_executable_material(
                    connection,
                    document["semantic"],
                    derivative_message=(
                        "Successful canonical Export bytes cannot be read as current "
                        "executable material."
                    ),
                )
        creation_authority = snapshot.get("creation_authority")
        if not isinstance(creation_authority, dict):
            creation_authority = {"state": "unverified", "verified": False}
        authority_projection = snapshot.get("authority_projection")
        if not isinstance(authority_projection, dict):
            # Compatibility for a B0 repository.  B1 Core always supplies the
            # projection; this fallback never upgrades semantic authority.
            authority_projection = {
                "state": "unverified",
                "confirmed_decision_unit_count": 0,
                "decision_unit_count": 8,
                "as_of_revision": row["revision"],
                "decision_units": {},
            }
        return BlueprintReadResult(
            {
                "object": "creative_blueprint.revision",
                "schema_version": "1",
                "database_uuid": database_uuid,
                "project_id": normalized_project,
                "revision": row["revision"],
                "content_sha256": row["content_sha256"],
                "semantic_sha256": row["semantic_sha256"],
                "operation_id": row["operation_id"],
                "current": bool(snapshot["is_current"]),
                "blueprint": document,
                "authority": {"state": "unverified", "verified": False},
                "creation_authority": creation_authority,
                "authority_projection": authority_projection,
            }
        )

    @staticmethod
    def _safe_operation_projection(
        operations: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        safe_operations: list[dict[str, object]] = []
        for row in operations:
            result = row.get("result")
            result = result if isinstance(result, Mapping) else {}
            safe_operations.append(
                {
                    "operation_id": row.get("id") or row.get("operation_id"),
                    "sequence": row.get("sequence"),
                    "parent_operation_id": row.get("parent_operation_id"),
                    "command_type": row.get("command_type"),
                    "intent_code": row.get("intent_code"),
                    "result_kind": row.get("result_kind"),
                    "result_revision": row.get("result_revision"),
                    "result_content_sha256": row.get("result_content_sha256"),
                    "changed_sections": result.get("changed_sections", []),
                    "restore_from": result.get("restore_from"),
                    "input_sha256": row.get("input_sha256"),
                    "output_sha256": row.get("output_sha256"),
                    "created_at": row.get("created_at"),
                }
            )
        return safe_operations

    @staticmethod
    def _has_any_blueprint_trace(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> bool:
        tables = (
            "creative_blueprint_heads",
            "creative_blueprint_revisions",
            "creative_blueprint_operations",
            "blueprint_command_receipts",
            "blueprint_desktop_receipt_integrity",
            "agent_project_capabilities",
            "agent_pairing_confirmation_receipts",
            "agent_project_capability_events",
            "blueprint_decision_authority_heads",
            "blueprint_decision_authority_events",
            "blueprint_decision_authority_receipts",
        )
        statements = [
            f"SELECT 1 AS present FROM {table} WHERE project_id=?" for table in tables
        ]
        statements.append(
            "SELECT 1 AS present FROM agent_project_command_receipts "
            "WHERE project_id=? AND receipt_schema_version='1' "
            "AND resource_type='creative_blueprint'"
        )
        statement = " UNION ALL ".join(statements)
        return connection.execute(
            f"SELECT present FROM ({statement}) LIMIT 1",
            (project_id,) * len(statements),
        ).fetchone() is not None

    @staticmethod
    def _has_any_canonical_timeline_trace(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> bool:
        """Classify a failed shared trust floor without adopting untrusted data."""

        tables = (
            "canonical_timeline_heads",
            "canonical_timeline_revisions",
            "canonical_timeline_operations",
            "canonical_timeline_receipts",
        )
        statement = " UNION ALL ".join(
            f"SELECT 1 AS present FROM main.{table} WHERE project_id=?"
            for table in tables
        )
        return connection.execute(
            f"SELECT present FROM ({statement}) LIMIT 1",
            (project_id,) * len(tables),
        ).fetchone() is not None

    @staticmethod
    def _decode_canonical_history_json(
        raw: object,
        *,
        expected_type: type,
        error_code: str,
    ) -> object:
        if type(raw) is not str:
            raise BlueprintIntegrityError(error_code)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise BlueprintIntegrityError(error_code) from exc
        if type(value) is not expected_type or canonical_json(value) != raw:
            raise BlueprintIntegrityError(error_code)
        return value

    @staticmethod
    def _legacy_projection_limits(
        *,
        brief_count: int,
        timeline_count: int,
        limit: int,
    ) -> tuple[int, int]:
        """Share one response budget without inventing cross-kind ordering."""

        brief_limit = min(brief_count, (limit + 1) // 2)
        timeline_limit = min(timeline_count, limit // 2)
        remaining = limit - brief_limit - timeline_limit
        if remaining:
            brief_extra = min(brief_count - brief_limit, remaining)
            brief_limit += brief_extra
            remaining -= brief_extra
        if remaining:
            timeline_limit += min(timeline_count - timeline_limit, remaining)
        return brief_limit, timeline_limit

    @staticmethod
    def _legacy_json_size_preflight(
        rows: list[sqlite3.Row],
        *,
        size_columns: tuple[str, ...],
        aggregate_bytes: int = 0,
    ) -> int:
        total = aggregate_bytes
        for row in rows:
            for column in size_columns:
                size = row[column]
                if (
                    type(size) is not int
                    or not 0 <= size <= _LEGACY_HISTORY_JSON_VALUE_MAX_BYTES
                ):
                    raise BlueprintIntegrityError("legacy_history_json_size_invalid")
                total += size
                if total > _LEGACY_HISTORY_JSON_AGGREGATE_MAX_BYTES:
                    raise BlueprintIntegrityError("legacy_history_json_budget_exceeded")
        return total

    def _legacy_artifacts_projection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        limit: int,
    ) -> dict[str, object]:
        brief_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM creative_briefs WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        )
        timeline_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM timelines WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
        )
        brief_limit, timeline_limit = self._legacy_projection_limits(
            brief_count=brief_count,
            timeline_count=timeline_count,
            limit=limit,
        )
        brief_size_rows = connection.execute(
            """SELECT length(CAST(brief_json AS BLOB)) AS brief_json_bytes,
                      length(CAST(provenance_json AS BLOB)) AS provenance_json_bytes
                 FROM creative_briefs WHERE project_id=?
                 ORDER BY revision DESC LIMIT ?""",
            (project_id, brief_limit),
        ).fetchall()
        aggregate_bytes = self._legacy_json_size_preflight(
            brief_size_rows,
            size_columns=("brief_json_bytes", "provenance_json_bytes"),
        )
        brief_rows = connection.execute(
            """SELECT revision,brief_json,content_sha256,provenance_json,created_at
                 FROM creative_briefs WHERE project_id=?
                 ORDER BY revision DESC LIMIT ?""",
            (project_id, brief_limit),
        ).fetchall()
        briefs: list[dict[str, object]] = []
        for row in brief_rows:
            brief = self._decode_canonical_history_json(
                row["brief_json"],
                expected_type=dict,
                error_code="legacy_brief_history_invalid",
            )
            self._decode_canonical_history_json(
                row["provenance_json"],
                expected_type=dict,
                error_code="legacy_brief_history_invalid",
            )
            digest = str(row["content_sha256"])
            if (
                type(row["revision"]) is not int
                or int(row["revision"]) < 1
                or _SHA256.fullmatch(digest) is None
                or hashlib.sha256(canonical_json(brief).encode()).hexdigest() != digest
                or not self.repository._is_utc_timestamp(row["created_at"])
            ):
                raise BlueprintIntegrityError("legacy_brief_history_invalid")
            briefs.append(
                {
                    "revision": int(row["revision"]),
                    "content_sha256": digest,
                    "created_at": str(row["created_at"]),
                    "role": "migration_context_only",
                }
            )

        timeline_size_rows = connection.execute(
            """SELECT length(CAST(timeline_json AS BLOB)) AS timeline_json_bytes,
                      length(CAST(provenance_json AS BLOB)) AS provenance_json_bytes,
                      length(CAST(validation_errors_json AS BLOB)) AS validation_errors_json_bytes
                 FROM timelines WHERE project_id=?
                 ORDER BY created_at DESC,id,revision DESC LIMIT ?""",
            (project_id, timeline_limit),
        ).fetchall()
        self._legacy_json_size_preflight(
            timeline_size_rows,
            size_columns=(
                "timeline_json_bytes",
                "provenance_json_bytes",
                "validation_errors_json_bytes",
            ),
            aggregate_bytes=aggregate_bytes,
        )
        timeline_rows = connection.execute(
            """SELECT id,revision,parent_revision,brief_revision,schema_version,
                      timeline_json,content_sha256,provenance_json,
                      validation_status,validation_errors_json,created_at
                 FROM timelines WHERE project_id=?
                 ORDER BY created_at DESC,id,revision DESC LIMIT ?""",
            (project_id, timeline_limit),
        ).fetchall()
        timelines: list[dict[str, object]] = []
        for row in timeline_rows:
            timeline = self._decode_canonical_history_json(
                row["timeline_json"],
                expected_type=dict,
                error_code="legacy_timeline_history_invalid",
            )
            self._decode_canonical_history_json(
                row["provenance_json"],
                expected_type=dict,
                error_code="legacy_timeline_history_invalid",
            )
            self._decode_canonical_history_json(
                row["validation_errors_json"],
                expected_type=list,
                error_code="legacy_timeline_history_invalid",
            )
            digest = str(row["content_sha256"])
            if (
                type(row["id"]) is not str
                or _IDENTIFIER.fullmatch(str(row["id"])) is None
                or type(row["revision"]) is not int
                or int(row["revision"]) < 1
                or (
                    row["parent_revision"] is not None
                    and (
                        type(row["parent_revision"]) is not int
                        or int(row["parent_revision"]) != int(row["revision"]) - 1
                    )
                )
                or type(row["brief_revision"]) is not int
                or int(row["brief_revision"]) < 1
                or row["schema_version"] != "1.0"
                or row["validation_status"] not in {"valid", "invalid"}
                or _SHA256.fullmatch(digest) is None
                or hashlib.sha256(canonical_json(timeline).encode()).hexdigest() != digest
                or not self.repository._is_utc_timestamp(row["created_at"])
            ):
                raise BlueprintIntegrityError("legacy_timeline_history_invalid")
            timelines.append(
                {
                    "timeline_id": str(row["id"]),
                    "revision": int(row["revision"]),
                    "parent_revision": (
                        int(row["parent_revision"])
                        if row["parent_revision"] is not None
                        else None
                    ),
                    "brief_revision": int(row["brief_revision"]),
                    "schema_version": str(row["schema_version"]),
                    "content_sha256": digest,
                    "validation_status": str(row["validation_status"]),
                    "created_at": str(row["created_at"]),
                    "role": "historical_observed_context_only",
                }
            )
        return {
            "status": "available",
            "reason_code": None,
            "lane": "legacy_artifacts",
            "role": "migration_context_only",
            "coverage_scope": "legacy_briefs_and_timelines",
            "order": "brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc",
            "authoritative_timeline_head": False,
            "briefs": briefs,
            "brief_count": brief_count,
            "briefs_truncated": brief_count > len(briefs),
            "timelines": timelines,
            "timeline_revision_count": timeline_count,
            "timelines_truncated": timeline_count > len(timelines),
        }

    def project_workspace(self, project_id: str, *, limit: int = 50) -> BlueprintReadResult:
        """Read either the legacy project or the canonical Blueprint workspace.

        The branch decision and every canonical lane are resolved from one
        SQLite snapshot.  A damaged Blueprint ledger is never allowed to fall
        back to the legacy Brief workbench.
        """

        normalized_project = _project_id(project_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BlueprintServiceError(
                "invalid_limit",
                "Workspace history limit must be between 1 and 100.",
            )
        with self.repository.transaction() as connection:
            database_uuid = self.repository._database_identity_in_transaction(connection)
            project = self.repository.get_creative_project_in_transaction(
                connection,
                normalized_project,
            )
            if project is None:
                raise BlueprintServiceError(
                    "project_not_found",
                    "Creative project does not exist.",
                    status=404,
                )
            # The trusted-prefix check reads SQLite connection identity via a
            # read-only PRAGMA. Run it before issuing the transaction-scoped
            # Blueprint attestation, then repeat it after Coverage validation.
            try:
                self.repository._assert_coverage_trusted_floor_in_transaction(
                    connection,
                    normalized_project,
                )
            except CoverageIntegrityError as exc:
                if self._has_any_canonical_timeline_trace(
                    connection,
                    normalized_project,
                ):
                    raise CanonicalTimelineIntegrityError(
                        "canonical_timeline_trusted_prefix_regressed"
                    ) from exc
                raise
            blueprint_ledger = self.repository.read_blueprint_workspace_ledger_in_transaction(
                connection,
                normalized_project,
                limit=limit,
            )
            if blueprint_ledger is None:
                if self._has_any_blueprint_trace(connection, normalized_project):
                    raise BlueprintIntegrityError("blueprint_head_missing")
                legacy = self.repository._get_project(connection, normalized_project)
                if legacy is None:
                    raise BlueprintServiceError(
                        "project_not_found",
                        "Creative project or legacy brief does not exist.",
                        status=404,
                    )
                legacy.update(
                    {
                        "object": "creative.project",
                        "schema_version": "1",
                        "database_uuid": database_uuid,
                        "canonical_source": "legacy_brief",
                    }
                )
                return BlueprintReadResult({"project": legacy, **legacy})

            head = blueprint_ledger.head
            document = head.get("blueprint")
            if not isinstance(document, dict):
                raise BlueprintIntegrityError("blueprint_document_contract_invalid")
            self._require_current_executable_material(
                connection,
                document["semantic"],
                derivative_message=(
                    "Successful canonical Export bytes cannot be projected as current "
                    "executable material."
                ),
            )
            operations = list(blueprint_ledger.operations)
            authority_ledger = self.repository._validate_authority_ledger(
                connection,
                normalized_project,
                validated_blueprint_ledger=blueprint_ledger,
            )
            revision_count = blueprint_ledger.revision_count
            operation_count = blueprint_ledger.operation_count
            try:
                legacy_artifacts = self._legacy_artifacts_projection(
                    connection,
                    normalized_project,
                    limit=limit,
                )
            except BlueprintIntegrityError:
                brief_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM creative_briefs WHERE project_id=?",
                        (normalized_project,),
                    ).fetchone()[0]
                )
                timeline_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM timelines WHERE project_id=?",
                        (normalized_project,),
                    ).fetchone()[0]
                )
                legacy_artifacts = {
                    "status": "unavailable",
                    "reason_code": "legacy_artifact_history_integrity_error",
                    "lane": "legacy_artifacts",
                    "role": "migration_context_only",
                    "coverage_scope": "legacy_briefs_and_timelines",
                    "order": "brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc",
                    "authoritative_timeline_head": False,
                    "briefs": [],
                    "brief_count": brief_count,
                    "briefs_truncated": brief_count > 0,
                    "timelines": [],
                    "timeline_revision_count": timeline_count,
                    "timelines_truncated": timeline_count > 0,
                }
            coverage_head = self.repository.get_coverage_head_in_transaction(
                connection,
                normalized_project,
                validated_blueprint_ledger=blueprint_ledger,
            )
            coverage_summary = (
                coverage_workspace_summary_from_validated_head_in_transaction(
                    self.repository,
                    connection,
                    head=coverage_head,
                    blueprint=head,
                )
            )
            canonical_timeline_head = (
                self.repository.get_canonical_timeline_head_in_transaction(
                    connection,
                    normalized_project,
                    validated_blueprint_ledger=blueprint_ledger,
                )
            )
            timeline_summary = timeline_workspace_summary_in_transaction(
                self.repository,
                connection,
                project_id=normalized_project,
                blueprint=head,
                coverage=coverage_head,
                validated_blueprint_ledger=blueprint_ledger,
                validated_timeline_head=canonical_timeline_head,
            )
            canonical_export_summary, canonical_export_available = (
                canonical_export_workspace_summary_in_transaction(
                    self.repository,
                    connection,
                    project_id=normalized_project,
                    timeline_summary=timeline_summary,
                    validated_timeline_head=canonical_timeline_head,
                )
            )
            try:
                self.repository._assert_coverage_trusted_floor_in_transaction(
                    connection,
                    normalized_project,
                )
            except CoverageIntegrityError as exc:
                if self._has_any_canonical_timeline_trace(
                    connection,
                    normalized_project,
                ):
                    raise CanonicalTimelineIntegrityError(
                        "canonical_timeline_trusted_prefix_regressed"
                    ) from exc
                raise

        current_blueprint = {
            "object": "creative_blueprint.revision",
            "schema_version": "1",
            "database_uuid": database_uuid,
            "project_id": normalized_project,
            "revision": int(head["revision"]),
            "content_sha256": str(head["content_sha256"]),
            "semantic_sha256": str(head["semantic_sha256"]),
            "operation_id": str(head["operation_id"]),
            "current": True,
            "blueprint": document,
            "authority": {"state": "unverified", "verified": False},
            "creation_authority": {"state": "unverified", "verified": False},
            "authority_projection": authority_ledger.current_projection,
        }
        authority_event_count = len(authority_ledger.events)
        authority_events = []
        for event in authority_ledger.events[-limit:]:
            observed = event.get("observed_head")
            observed = observed if isinstance(observed, Mapping) else {}
            authority_events.append(
                {
                    "event_id": event.get("id"),
                    "sequence": event.get("sequence"),
                    "authority_operation": event.get("authority_operation"),
                    "observed_revision": observed.get("revision"),
                    "decision_units": event.get("decision_units", []),
                    "created_at": event.get("created_at"),
                }
            )
        timeline_state = str(timeline_summary["state"])
        if timeline_state == "missing":
            coverage_state = str(coverage_summary["state"])
            if coverage_state == "current":
                executable_reason = (
                    "coverage_not_lowerable"
                    if int(coverage_summary["gap_count"]) > 0
                    else "canonical_timeline_materialization_available"
                )
            else:
                executable_reason = {
                    "missing": "coverage_plan_missing",
                    "stale_blueprint": "coverage_plan_stale_blueprint",
                    "stale_evidence": "coverage_plan_stale_evidence",
                }[coverage_state]
        else:
            executable_reason = {
                "current": "canonical_timeline_current",
                "stale_blueprint": "canonical_timeline_stale_blueprint",
                "stale_coverage": "canonical_timeline_stale_coverage",
                "stale_source_binding": "canonical_timeline_stale_source_binding",
            }[timeline_state]
        timeline_head = timeline_summary["head"]
        timeline_materialization_available = bool(
            timeline_state == "missing"
            and coverage_summary["state"] == "current"
            and int(coverage_summary["gap_count"]) == 0
        )
        timeline_reconciliation_available = bool(
            timeline_head is not None
            and timeline_state != "current"
            and coverage_summary["state"] == "current"
            and int(coverage_summary["gap_count"]) == 0
        )
        timeline_mutation_available = bool(
            timeline_head is not None and timeline_state == "current"
        )
        workspace = {
            "object": "creative.project_workspace",
            "schema_version": "1",
            "database_uuid": database_uuid,
            "id": normalized_project,
            "project_id": normalized_project,
            "title": project["title"],
            "status": project["status"],
            "canonical_source": "creative_blueprint",
            "workspace_mode": "canonical_blueprint",
            "current_blueprint": current_blueprint,
            "coverage": coverage_summary,
            "timeline": timeline_summary,
            "canonical_export": canonical_export_summary,
            "executable": {
                "state": (
                    "not_compiled"
                    if timeline_state == "missing"
                    else "compiled_draft"
                ),
                "current_timeline": timeline_head,
                "reason_code": executable_reason,
            },
            "history": {
                "complete_project_history": False,
                "global_total_order": False,
                "replay_scope": "per_lane_only",
                "blueprint": {
                    "lane": "semantic_changes",
                    "operations": self._safe_operation_projection(operations),
                    "available_revision_count": revision_count,
                    "available_operation_count": operation_count,
                    "operations_truncated": len(operations) < operation_count,
                    "ledger": {
                        "coverage_scope": "creative_blueprint",
                        "complete_project_history": False,
                    },
                    "order": "sequence_desc",
                },
                "decision_authority": {
                    "lane": "decision_authority",
                    "events": authority_events,
                    "available_event_count": authority_event_count,
                    "events_truncated": authority_event_count > len(authority_events),
                    "projection": authority_ledger.current_projection,
                    "coverage_scope": "blueprint_decision_authority",
                    "complete_project_history": False,
                    "order": "sequence_asc",
                },
                "legacy_artifacts": legacy_artifacts,
            },
            "capabilities": {
                "complete_project_history": False,
                "authoritative_timeline_head": timeline_head is not None,
                "coverage_plan_materialization": True,
                "timeline_materialization": timeline_materialization_available,
                "timeline_reconciliation": timeline_reconciliation_available,
                "blueprint_timeline_compiler": True,
                "timeline_mutation": timeline_mutation_available,
                "preview": False,
                "render": False,
                "export": False,
                "canonical_export": canonical_export_available,
            },
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
        }
        return BlueprintReadResult({"project": workspace})

    def history(self, project_id: str, *, limit: int) -> BlueprintReadResult:
        normalized_project = _project_id(project_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BlueprintServiceError("invalid_limit", "History limit must be between 1 and 100.")
        with self.repository.transaction() as connection:
            if (
                self.repository.get_creative_project_in_transaction(
                    connection,
                    normalized_project,
                )
                is None
            ):
                raise BlueprintServiceError(
                    "project_not_found",
                    "Creative project does not exist.",
                    status=404,
                )
            operations = self.repository.list_blueprint_operations_in_transaction(
                connection,
                normalized_project,
                limit=limit,
            )
        safe_operations = self._safe_operation_projection(operations)
        return BlueprintReadResult(
            {
                "object": "creative_blueprint.operation_list",
                "schema_version": "1",
                "project_id": normalized_project,
                "operations": safe_operations,
                "ledger": {
                    "coverage_scope": "creative_blueprint",
                    "complete_project_history": False,
                },
            }
        )


__all__ = [
    "BlueprintReadResult",
    "BlueprintService",
    "BlueprintServiceError",
    "BlueprintHeadConflictError",
    "BlueprintIntegrityError",
    "BlueprintReceiptConflictError",
]
