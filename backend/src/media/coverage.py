"""Canonical Coverage Plan command and read service.

Coverage is the only footage-planning truth between Creative Blueprint and a
future executable Timeline.  This baseline deliberately materializes only the
script blocks and evidence hints already present in one exact Blueprint.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Mapping

from core.blueprint_contract import blueprint_residual_proofs
from core.coverage_contract import (
    CoverageContractError,
    canonical_sha256,
    compile_baseline_coverage_plan,
    coverage_plan_content_sha256,
    normalize_evidence_proofs,
)
from core.media_db import (
    CanonicalDerivativeMaterialError,
    CoverageCommandResult,
    CoverageHeadConflictError,
    CoverageIntegrityError,
    MediaRepository,
    canonical_json,
    new_id,
    utc_now_iso,
)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATABASE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_COMMAND_FIELDS = {"expected_blueprint", "expected_plan_head"}


class CoverageServiceError(ValueError):
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
        raise CoverageServiceError(code, "Coverage command has an unsupported shape.")
    return value


def _project_id(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise CoverageServiceError("invalid_project_id", "Creative project identity is invalid.")
    return value


def _command_key(value: object) -> str:
    if type(value) is not str or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise CoverageServiceError(
            "invalid_idempotency_key",
            "A bounded ASCII Idempotency-Key is required.",
        )
    return value


def _database_uuid(value: object) -> str:
    if type(value) is not str or _DATABASE_UUID.fullmatch(value) is None:
        raise CoverageServiceError(
            "invalid_database_identity",
            "Expected MemoLens database identity is invalid.",
        )
    return value


def _plan_head(value: object, *, nullable: bool) -> dict[str, object] | None:
    if nullable and value is None:
        return None
    row = _closed(value, {"revision", "content_sha256"}, code="invalid_coverage_command")
    if (
        type(row["revision"]) is not int
        or not 1 <= row["revision"] <= 1_000_000
        or type(row["content_sha256"]) is not str
        or _SHA256.fullmatch(row["content_sha256"]) is None
    ):
        raise CoverageServiceError(
            "invalid_coverage_command",
            "Coverage head precondition is invalid.",
        )
    return {
        "revision": row["revision"],
        "content_sha256": row["content_sha256"],
    }


def _blueprint_binding(value: object) -> dict[str, object]:
    row = _closed(
        value,
        {"revision", "content_sha256", "semantic_sha256"},
        code="invalid_coverage_command",
    )
    if (
        type(row["revision"]) is not int
        or not 1 <= row["revision"] <= 1_000_000
        or any(
            type(row[name]) is not str or _SHA256.fullmatch(row[name]) is None
            for name in ("content_sha256", "semantic_sha256")
        )
    ):
        raise CoverageServiceError(
            "invalid_coverage_command",
            "Creative Blueprint binding is invalid.",
        )
    return {
        "revision": row["revision"],
        "content_sha256": row["content_sha256"],
        "semantic_sha256": row["semantic_sha256"],
    }


def _head_projection(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "revision": int(value["revision"]),
        "content_sha256": str(value["content_sha256"]),
    }


def coverage_freshness_in_transaction(
    repository: MediaRepository,
    connection: sqlite3.Connection,
    *,
    selected: Mapping[str, object] | None,
    blueprint: Mapping[str, object] | None,
) -> dict[str, object]:
    if selected is None:
        return {"state": "missing", "reasons": []}
    reasons: list[str] = []
    if blueprint is None or any(
        selected[name] != blueprint[target]
        for name, target in (
            ("blueprint_revision", "revision"),
            ("blueprint_content_sha256", "content_sha256"),
            ("blueprint_semantic_sha256", "semantic_sha256"),
        )
    ):
        reasons.append("blueprint_head_changed")
    plan = selected["plan"]
    if type(plan) is not dict or type(plan.get("evidence_manifest")) is not list:
        raise CoverageIntegrityError("coverage_plan_contract_invalid")
    legacy_image_evidence = any(
        type(item) is dict
        and type(item.get("proof")) is dict
        and item["proof"].get("kind") == "asset"
        and set(item["proof"]) == {"kind", "asset_id", "asset_sha256"}
        for item in plan["evidence_manifest"]
    )
    if legacy_image_evidence:
        reasons.append("legacy_image_identity_only")
        return {
            "state": (
                "stale_blueprint"
                if "blueprint_head_changed" in reasons
                else "stale_evidence"
            ),
            "reasons": reasons,
        }
    evidence_refs = [
        str(item["evidence_ref"])
        for item in plan["evidence_manifest"]
        if type(item) is dict and type(item.get("evidence_ref")) is str
    ]
    residual_proofs = {
        str(item["evidence_ref"]): dict(item["proof"])
        for item in plan["evidence_manifest"]
        if type(item) is dict
        and type(item.get("evidence_ref")) is str
        and type(item.get("proof")) is dict
        and item["proof"].get("kind") == "residual_span"
    }
    try:
        if residual_proofs:
            observed = repository.resolve_media_evidence_proofs_in_transaction(
                connection,
                evidence_refs,
                image_assets_only=True,
                residual_proofs=residual_proofs,
                executable_evidence_refs=evidence_refs,
            )
        else:
            observed = repository.resolve_media_evidence_proofs_in_transaction(
                connection,
                evidence_refs,
                image_assets_only=True,
                executable_evidence_refs=evidence_refs,
            )
    except CanonicalDerivativeMaterialError:
        reasons.append("derivative_material_forbidden")
        observed = []
    if list(normalize_evidence_proofs(observed)) != plan["evidence_manifest"]:
        reasons.append("evidence_head_changed")
    state = (
        "stale_blueprint"
        if "blueprint_head_changed" in reasons
        else "stale_evidence"
        if "evidence_head_changed" in reasons
        else "current"
    )
    return {"state": state, "reasons": reasons}


def coverage_workspace_summary_in_transaction(
    repository: MediaRepository,
    connection: sqlite3.Connection,
    *,
    project_id: str,
    blueprint: Mapping[str, object] | None,
    validated_blueprint_ledger: object | None = None,
    trusted_floor_prevalidated: bool = False,
) -> dict[str, object]:
    head = repository.get_trusted_coverage_head_in_transaction(
        connection,
        project_id,
        validated_blueprint_ledger=validated_blueprint_ledger,  # type: ignore[arg-type]
        _trusted_floor_prevalidated=trusted_floor_prevalidated,
    )
    return coverage_workspace_summary_from_validated_head_in_transaction(
        repository,
        connection,
        head=head,
        blueprint=blueprint,
    )


def coverage_workspace_summary_from_validated_head_in_transaction(
    repository: MediaRepository,
    connection: sqlite3.Connection,
    *,
    head: Mapping[str, object] | None,
    blueprint: Mapping[str, object] | None,
) -> dict[str, object]:
    """Project a Coverage head already audited in the current read transaction."""

    freshness = coverage_freshness_in_transaction(
        repository,
        connection,
        selected=head,
        blueprint=blueprint,
    )
    if head is None:
        return {
            "state": "missing",
            "head": None,
            "blueprint_binding": None,
            "beat_count": 0,
            "selected_assignment_count": 0,
            "gap_count": 0,
        }
    plan = head["plan"]
    assert isinstance(plan, dict)
    beats = plan["beats"]
    binding = plan["blueprint_binding"]
    assert isinstance(beats, list) and isinstance(binding, dict)
    return {
        "state": freshness["state"],
        "head": _head_projection(head),
        "blueprint_binding": {
            "revision": binding["revision"],
            "content_sha256": binding["content_sha256"],
            "semantic_sha256": binding["semantic_sha256"],
        },
        "beat_count": len(beats),
        "selected_assignment_count": sum(
            len(beat["selected_assignments"])
            for beat in beats
            if isinstance(beat, dict)
            and isinstance(beat.get("selected_assignments"), list)
        ),
        "gap_count": sum(
            1
            for beat in beats
            if isinstance(beat, dict) and beat.get("gap") is not None
        ),
    }


class CoverageService:
    principal = "desktop_app"
    command_type = "coverage.materialize_baseline"
    command_version = "1"

    def __init__(self, repository: MediaRepository) -> None:
        self.repository = repository

    def materialize_baseline(
        self,
        project_id: str,
        payload: object,
        *,
        idempotency_key: str,
        expected_database_uuid: str | None = None,
    ) -> CoverageCommandResult:
        normalized_project = _project_id(project_id)
        body = _closed(payload, _COMMAND_FIELDS, code="invalid_coverage_command")
        expected_blueprint = _blueprint_binding(body["expected_blueprint"])
        expected_plan_head = _plan_head(body["expected_plan_head"], nullable=True)
        key = _command_key(idempotency_key)
        normalized_database_uuid = (
            _database_uuid(expected_database_uuid)
            if expected_database_uuid is not None
            else None
        )
        request = {
            "object": "memolens.coverage_materialize_command",
            "schema_version": "1",
            "project_id": normalized_project,
            "expected_blueprint": expected_blueprint,
            "expected_plan_head": expected_plan_head,
        }
        if normalized_database_uuid is not None:
            request["expected_database_uuid"] = normalized_database_uuid
        request_sha256 = hashlib.sha256(canonical_json(request).encode()).hexdigest()

        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            return self._materialize_mutation(
                connection,
                project_id=normalized_project,
                expected_blueprint=expected_blueprint,
                expected_plan_head=expected_plan_head,
                expected_database_uuid=normalized_database_uuid,
                request_sha256=request_sha256,
            )

        return self.repository.execute_coverage_command(
            authenticated_principal=self.principal,
            project_id=normalized_project,
            command_type=self.command_type,
            command_version=self.command_version,
            idempotency_key=key,
            request_sha256=request_sha256,
            mutation=mutation,
        )

    def _materialize_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        expected_blueprint: dict[str, object],
        expected_plan_head: dict[str, object] | None,
        expected_database_uuid: str | None,
        request_sha256: str,
    ) -> tuple[dict[str, object], int, str, str]:
        if (
            expected_database_uuid is not None
            and self.repository._database_identity_in_transaction(connection)
            != expected_database_uuid
        ):
            raise CoverageServiceError(
                "database_identity_changed",
                "MemoLens database identity changed; reopen the project.",
                status=409,
            )
        project = self.repository.get_creative_project_in_transaction(connection, project_id)
        if project is None:
            raise CoverageServiceError("project_not_found", "Creative project does not exist.", status=404)
        if project.get("status") == "archived":
            raise CoverageServiceError(
                "project_archived",
                "Archived creative projects cannot be changed.",
                status=409,
            )
        blueprint = self.repository.get_blueprint_head_in_transaction(connection, project_id)
        if blueprint is None:
            raise CoverageServiceError(
                "blueprint_not_found",
                "A canonical Creative Blueprint is required first.",
                status=409,
            )
        observed_blueprint = {
            "revision": blueprint["revision"],
            "content_sha256": blueprint["content_sha256"],
            "semantic_sha256": blueprint["semantic_sha256"],
        }
        if observed_blueprint != expected_blueprint:
            raise CoverageServiceError(
                "blueprint_head_conflict",
                "Creative Blueprint changed before Coverage materialization.",
                status=409,
                details={"current": observed_blueprint},
            )
        current = self.repository.get_coverage_head_in_transaction(connection, project_id)
        if _head_projection(current) != expected_plan_head:
            raise CoverageHeadConflictError(_head_projection(current))
        document = blueprint["blueprint"]
        if type(document) is not dict:
            raise CoverageIntegrityError("coverage_blueprint_invalid")
        semantic = document["semantic"]
        evidence_refs = tuple(
            sorted(
                {
                    str(hint["evidence_ref"])
                    for hint in semantic["material_hints"]
                }
            )
        )
        residual_proofs = blueprint_residual_proofs(semantic)
        try:
            if residual_proofs:
                evidence_rows = self.repository.resolve_media_evidence_proofs_in_transaction(
                    connection,
                    evidence_refs,
                    image_assets_only=True,
                    residual_proofs=residual_proofs,
                    executable_evidence_refs=evidence_refs,
                )
            else:
                evidence_rows = self.repository.resolve_media_evidence_proofs_in_transaction(
                    connection,
                    evidence_refs,
                    image_assets_only=True,
                    executable_evidence_refs=evidence_refs,
                )
        except CanonicalDerivativeMaterialError as exc:
            raise CoverageServiceError(
                exc.code,
                "Successful canonical Export bytes cannot enter executable Coverage.",
                status=409,
                details={"evidence_refs": list(exc.evidence_refs)},
            ) from exc
        unresolved_residuals = sorted(
            str(row["evidence_ref"])
            for row in evidence_rows
            if row.get("evidence_ref") in residual_proofs and row.get("proof") is None
        )
        if unresolved_residuals:
            raise CoverageServiceError(
                "coverage_residual_evidence_stale",
                "Residual material proof changed before Coverage materialization.",
                status=409,
                details={"evidence_refs": unresolved_residuals},
            )
        revision = int(current["revision"]) + 1 if current is not None else 1
        parent = _head_projection(current)
        operation_id = new_id("coverage_op")
        try:
            plan = compile_baseline_coverage_plan(
                project_id=project_id,
                revision=revision,
                parent=parent,
                blueprint=document,
                blueprint_binding=observed_blueprint,
                evidence_rows=evidence_rows,
            )
        except CoverageContractError as exc:
            raise CoverageServiceError(
                "coverage_compile_failed",
                "Coverage Plan could not be compiled.",
                status=422,
                details={"errors": exc.errors},
            ) from exc
        content_digest = coverage_plan_content_sha256(plan)
        manifest_digest = canonical_sha256(plan["evidence_manifest"])
        now = utc_now_iso()
        result_head = {
            "revision": revision,
            "content_sha256": content_digest,
            "blueprint_revision": blueprint["revision"],
            "blueprint_content_sha256": blueprint["content_sha256"],
            "blueprint_semantic_sha256": blueprint["semantic_sha256"],
            "operation_id": operation_id,
        }
        result = {"kind": "revision_created", "result_head": result_head}
        self.repository.append_coverage_operation(
            connection,
            operation_id=operation_id,
            project_id=project_id,
            expected_blueprint_revision=int(blueprint["revision"]),
            expected_blueprint_content_sha256=str(blueprint["content_sha256"]),
            expected_plan_head=expected_plan_head,
            request_sha256=request_sha256,
            result_revision=revision,
            result_content_sha256=content_digest,
            result=result,
            created_at=now,
        )
        self.repository.append_coverage_revision(
            connection,
            project_id=project_id,
            revision=revision,
            parent_revision=(int(current["revision"]) if current is not None else None),
            parent_content_sha256=(
                str(current["content_sha256"]) if current is not None else None
            ),
            blueprint_revision=int(blueprint["revision"]),
            blueprint_content_sha256=str(blueprint["content_sha256"]),
            blueprint_semantic_sha256=str(blueprint["semantic_sha256"]),
            blueprint_operation_id=str(blueprint["operation_id"]),
            plan=plan,
            content_sha256_value=content_digest,
            evidence_manifest_sha256=manifest_digest,
            operation_id=operation_id,
            created_at=now,
        )
        self.repository.cas_coverage_head(
            connection,
            project_id=project_id,
            expected_head=expected_plan_head,
            new_head=result_head,
            updated_at=now,
        )
        self.repository.touch_creative_project(connection, project_id=project_id, updated_at=now)
        response = {
            "object": "coverage_plan.command_result",
            "schema_version": "1",
            "project_id": project_id,
            "command_type": self.command_type,
            "operation_id": operation_id,
            "result": result,
        }
        return response, 201, operation_id, f"{project_id}:{revision}"

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
            raise CoverageServiceError("invalid_revision", "Coverage revision is invalid.")
        with self.repository.transaction() as connection:
            project = self.repository.get_creative_project_in_transaction(
                connection, normalized_project
            )
            if project is None:
                raise CoverageServiceError(
                    "project_not_found", "Creative project does not exist.", status=404
                )
            head = self.repository.get_trusted_coverage_head_in_transaction(
                connection, normalized_project
            )
            blueprint = self.repository.get_blueprint_head_in_transaction(
                connection, normalized_project
            )
            selected = (
                head
                if revision is None
                else self.repository.get_trusted_coverage_revision_in_transaction(
                    connection,
                    normalized_project,
                    revision,
                    _validate_ledger=False,
                )
            )
            if revision is not None and selected is None:
                raise CoverageServiceError(
                    "coverage_not_found", "Coverage revision does not exist.", status=404
                )
            freshness = coverage_freshness_in_transaction(
                self.repository,
                connection,
                selected=selected,
                blueprint=blueprint,
            )
            operations = self.repository.list_coverage_operations_in_transaction(
                connection, normalized_project, limit=50
            )
            database_uuid = self.repository._database_identity_in_transaction(connection)
        return {
            "object": "coverage_plan.workspace",
            "schema_version": "1",
            "database_uuid": database_uuid,
            "project_id": normalized_project,
            "head": _head_projection(head),
            "selected_revision": (
                None
                if selected is None
                else {
                    "revision": selected["revision"],
                    "content_sha256": selected["content_sha256"],
                    "evidence_manifest_sha256": selected[
                        "evidence_manifest_sha256"
                    ],
                    "operation_id": selected["operation_id"],
                    "is_head": bool(
                        head is not None
                        and selected["revision"] == head["revision"]
                        and selected["content_sha256"] == head["content_sha256"]
                    ),
                }
            ),
            "freshness": freshness,
            "coverage_plan": None if selected is None else selected["plan"],
            "operations": operations,
        }
