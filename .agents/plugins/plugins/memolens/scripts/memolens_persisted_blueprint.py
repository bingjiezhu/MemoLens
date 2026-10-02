"""Fail-closed, read-only projections of persisted Creative Blueprints.

The writer lives behind the desktop-authenticated Core boundary.  This module
only reads a private SQLite snapshot and deliberately exposes neither command
receipts nor raw operation JSON.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any

from memolens_blueprint_authority import (
    AUTHORITY_RELATIONS,
    authority_schema_available,
    project_validated_authority,
    validate_exact_managed_schema_manifest,
    validate_authority_ledger,
    validate_and_project_authority,
)
from memolens_agent_receipts import validate_agent_receipt_ledger
from memolens_contracts import MemoLensError, safety_summary
from memolens_creative_blueprint import (
    PERSISTED_BLUEPRINT_SOURCE_LIMITS,
    _Errors,
    _cross_validate,
    _validate_schema,
    canonical_json,
    canonical_sha256,
)
from memolens_media_store import MediaIndexReader
from memolens_project_store import ProjectIndexReader, validate_project_identifier
from memolens_residual_authority import (
    ResidualAuthorityError,
    current_candidate_usage_facts,
    require_valid_residual_binding,
    revalidate_residual_proof_in_connection,
)
from memolens_sqlite import ReadOnlyDatabase
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json
from memolens_wiki import parse_evidence_id


PERSISTED_BLUEPRINT_OBJECT = "memolens.creative_blueprint"
PERSISTED_BLUEPRINT_PROJECTION_OBJECT = "memolens.creative_blueprint_projection"
PERSISTED_BLUEPRINT_SCHEMA_VERSION = "1"
PERSISTED_BLUEPRINT_READ_SCHEMA_VERSION = "1"
PERSISTED_BLUEPRINT_SCHEMA_ARTIFACT = "creative-blueprint-v1.schema.json"
PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_ARTIFACT = (
    "creative-blueprint-residual-v1.schema.json"
)
# Filled from the frozen exact-byte artifact by the contract slice.  A mismatch
# disables this optional read capability instead of accepting a lookalike schema.
PERSISTED_BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256 = (
    "e239655e84014be04434eae19497706b28e62280b86b9db93bb1c80e32a56d52"
)
PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_V1_EXPECTED_SHA256 = (
    "2a7be876aff82b642bee8dc09060e21c57352c9ef4826043daea1a3487689e64"
)

_SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
_SCHEMA_PATH = _SCHEMA_ROOT / PERSISTED_BLUEPRINT_SCHEMA_ARTIFACT
_RESIDUAL_SCHEMA_PATH = _SCHEMA_ROOT / PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_ARTIFACT
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00$"
)
_REVISION_COLUMNS = {
    "project_id",
    "revision",
    "parent_revision",
    "parent_content_sha256",
    "schema_version",
    "schema_sha256",
    "blueprint_json",
    "content_sha256",
    "semantic_sha256",
    "source_candidate_sha256",
    "authority_state",
    "operation_id",
    "created_at",
}
_REVISION_IDENTITY_COLUMNS = (
    "project_id",
    "revision",
    "parent_revision",
    "parent_content_sha256",
    "content_sha256",
    "semantic_sha256",
    "operation_id",
    "created_at",
)
_HEAD_COLUMNS = {
    "project_id",
    "revision",
    "content_sha256",
    "semantic_sha256",
    "operation_id",
    "updated_at",
}
_OPERATION_COLUMNS = {
    "id",
    "project_id",
    "sequence",
    "parent_operation_id",
    "schema_version",
    "coverage_scope",
    "complete_project_history",
    "command_type",
    "command_version",
    "effect_class",
    "actor_json",
    "origin_json",
    "intent_code",
    "precondition_json",
    "typed_diff_json",
    "input_sha256",
    "output_sha256",
    "result_kind",
    "result_revision",
    "result_content_sha256",
    "result_json",
    "created_at",
}
_RELATIONS = {
    "creative_blueprint_revisions": _REVISION_COLUMNS,
    "creative_blueprint_heads": _HEAD_COLUMNS,
    "creative_blueprint_operations": _OPERATION_COLUMNS,
}
_COMMAND_TYPES = {"blueprint.commit_proposal", "blueprint.restore_revision"}
_RESULT_KINDS = {"revision_created", "no_change", "revision_restored"}
_REVISION_RESULT_KINDS = {"revision_created", "revision_restored"}
_SEMANTIC_SECTIONS = {
    "intent",
    "script",
    "direction",
    "output",
    "constraints",
    "material_hints",
    "reference_refs",
    "technique_refs",
    "bindings",
    "assumptions",
    "missing_evidence",
    "open_decisions",
}
_SEMANTIC_SECTION_ORDER = (
    "intent",
    "script",
    "direction",
    "output",
    "constraints",
    "material_hints",
    "reference_refs",
    "technique_refs",
    "bindings",
    "assumptions",
    "missing_evidence",
    "open_decisions",
)
_DECISION_UNITS = {
    "intent_goal",
    "intent_stance",
    "script",
    "creative_direction",
    "output",
    "material_constraints",
    "references",
    "techniques",
}
_OPERATION_JSON_LIMITS = StrictJsonLimits(
    max_bytes=262_144,
    max_depth=16,
    max_nodes=20_000,
    max_object_items=512,
    max_array_items=512,
    max_string_chars=12_000,
)
_OPERATION_JSON_COLUMNS = (
    "o.actor_json",
    "o.origin_json",
    "o.precondition_json",
    "o.typed_diff_json",
    "o.result_json",
)
_MAX_BLUEPRINT_LEDGER_ROWS = 1_000_000
# Core permits each of the five envelopes to reach the strict per-value bound.
# Keep the aggregate checks explicit without inventing a narrower reader-only
# contract that would reject a legal Core ledger.
_OPERATION_ROW_STORED_JSON_MAX_BYTES = (
    len(_OPERATION_JSON_COLUMNS) * _OPERATION_JSON_LIMITS.max_bytes
)
_OPERATION_PROJECT_STORED_JSON_MAX_BYTES = (
    _MAX_BLUEPRINT_LEDGER_ROWS * _OPERATION_ROW_STORED_JSON_MAX_BYTES
)
_REVISION_PROJECT_STORED_JSON_MAX_BYTES = (
    _MAX_BLUEPRINT_LEDGER_ROWS * PERSISTED_BLUEPRINT_SOURCE_LIMITS.max_bytes
)
_REVISION_BUNDLE_CACHE_SIZE = 2


def _load_schema(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not _SHA256.fullmatch(expected_sha256):
        return None, None
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            return None, None
        decoded = decode_strict_json(raw, limits=PERSISTED_BLUEPRINT_SOURCE_LIMITS)
    except (OSError, StrictJsonError):
        return None, None
    return (decoded, hashlib.sha256(raw).hexdigest()) if isinstance(decoded, dict) else (None, None)


PERSISTED_BLUEPRINT_SCHEMA, PERSISTED_BLUEPRINT_SCHEMA_SHA256 = _load_schema(
    _SCHEMA_PATH,
    PERSISTED_BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256,
)
(
    PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA,
    PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_SHA256,
) = _load_schema(
    _RESIDUAL_SCHEMA_PATH,
    PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_V1_EXPECTED_SHA256,
)
PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE = PERSISTED_BLUEPRINT_SCHEMA is not None


def _expanded_schema(
    schema: dict[str, Any],
    *,
    base_schema: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve frozen local/base ``$defs`` references in memory."""

    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return None
    base_definitions = (
        base_schema.get("$defs") if isinstance(base_schema, dict) else None
    )

    def expand(
        value: Any,
        stack: tuple[str, ...] = (),
        namespace: str = "local",
    ) -> Any:
        if isinstance(value, list):
            return [expand(item, stack, namespace) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if reference is not None:
            local_prefix = "#/$defs/"
            base_prefix = f"{PERSISTED_BLUEPRINT_SCHEMA_ARTIFACT}#/$defs/"
            if not isinstance(reference, str) or set(value) != {"$ref"}:
                raise ValueError("unsupported schema reference")
            if reference.startswith(local_prefix):
                selected = (
                    base_definitions if namespace == "base" else definitions
                )
                if not isinstance(selected, dict):
                    raise ValueError("invalid schema reference")
                name = reference.removeprefix(local_prefix)
                selected_namespace = namespace
                identity = f"{namespace}:{name}"
            elif reference.startswith(base_prefix) and isinstance(
                base_definitions, dict
            ):
                selected = base_definitions
                name = reference.removeprefix(base_prefix)
                selected_namespace = "base"
                identity = f"base:{name}"
            else:
                raise ValueError("unsupported schema reference")
            if identity in stack or name not in selected:
                raise ValueError("invalid schema reference")
            return expand(
                selected[name],
                (*stack, identity),
                selected_namespace,
            )
        return {
            key: expand(child, stack, namespace)
            for key, child in value.items()
            if key != "$defs"
        }

    try:
        expanded = expand(schema)
    except (RecursionError, ValueError):
        return None
    return expanded if isinstance(expanded, dict) else None


PERSISTED_BLUEPRINT_EXPANDED_SCHEMA = (
    _expanded_schema(PERSISTED_BLUEPRINT_SCHEMA)
    if PERSISTED_BLUEPRINT_SCHEMA is not None
    else None
)
PERSISTED_BLUEPRINT_RESIDUAL_EXPANDED_SCHEMA = (
    _expanded_schema(
        PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA,
        base_schema=PERSISTED_BLUEPRINT_SCHEMA,
    )
    if PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA is not None
    else None
)
PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE = bool(
    PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA is not None
    and PERSISTED_BLUEPRINT_RESIDUAL_EXPANDED_SCHEMA is not None
)
PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE = bool(
    PERSISTED_BLUEPRINT_SCHEMA is not None
    and PERSISTED_BLUEPRINT_EXPANDED_SCHEMA is not None
    and PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE
)


def persisted_blueprint_contract_summary() -> dict[str, Any]:
    return {
        "object": PERSISTED_BLUEPRINT_OBJECT,
        "schema_version": PERSISTED_BLUEPRINT_SCHEMA_VERSION,
        "schema_available": PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE,
        "schema_sha256": PERSISTED_BLUEPRINT_SCHEMA_SHA256,
        "schema_artifact": PERSISTED_BLUEPRINT_SCHEMA_ARTIFACT,
        "residual_schema_available": PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE,
        "residual_schema_sha256": PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_SHA256,
        "residual_schema_artifact": PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_ARTIFACT,
        "persisted_read_only": True,
        "creation_authority_verified": False,
        "ledger_coverage_scope": "creative_blueprint",
        "complete_project_history": False,
    }


def persisted_blueprint_safety_summary(
    *,
    operation_receipts_validated: bool = False,
) -> dict[str, Any]:
    return {
        **safety_summary(),
        "private_ephemeral_snapshot": True,
        "source_database_written": False,
        "blueprint_written": False,
        "operation_receipts_read": operation_receipts_validated,
        "operation_receipts_validated": operation_receipts_validated,
        "raw_operation_receipts_returned": False,
        "raw_operation_json_returned": False,
        "private_locator_returned_by_history": False,
    }


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _valid_revision(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 1_000_000


def _valid_timestamp(value: Any) -> bool:
    if not (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and _UTC_TIMESTAMP.fullmatch(value) is not None
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0) and parsed.isoformat() == value


def _valid_revision_ref(value: Any, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return bool(
        isinstance(value, dict)
        and set(value) == {"revision", "content_sha256"}
        and _valid_revision(value.get("revision"))
        and _valid_digest(value.get("content_sha256"))
    )


def _semantic_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> list[dict[str, Any]]:
    diff: list[dict[str, Any]] = []
    for section in _SEMANTIC_SECTION_ORDER:
        before_digest = canonical_sha256(before[section]) if before is not None else None
        after_digest = canonical_sha256(after[section])
        if before_digest != after_digest:
            diff.append(
                {
                    "section": section,
                    "change": "added" if before is None else "modified",
                    "before_sha256": before_digest,
                    "after_sha256": after_digest,
                }
            )
    return diff


def _restore_material_sha256(document: dict[str, Any]) -> str:
    """Digest only the immutable material that a restore must reproduce."""

    return canonical_sha256(
        {
            "semantic": document["semantic"],
            "evidence_manifest": document["evidence_manifest"],
            "lineage": document["lineage"],
        }
    )


def _corrupt() -> MemoLensError:
    return MemoLensError(
        "Persisted Creative Blueprint integrity verification failed.",
        code="blueprint_integrity_failed",
    )


def _strict_object(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise _corrupt()
    try:
        value = decode_strict_json(raw.encode("utf-8"), limits=_OPERATION_JSON_LIMITS)
    except (UnicodeError, StrictJsonError) as exc:
        raise _corrupt() from exc
    if not isinstance(value, dict):
        raise _corrupt()
    if canonical_json(value) != raw:
        raise _corrupt()
    return value


def _schema_errors(document: dict[str, Any]) -> list[dict[str, str]]:
    schema_sha256 = document.get("schema_sha256")
    expanded_schema = (
        PERSISTED_BLUEPRINT_RESIDUAL_EXPANDED_SCHEMA
        if schema_sha256 == PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_SHA256
        else (
            PERSISTED_BLUEPRINT_EXPANDED_SCHEMA
            if schema_sha256 == PERSISTED_BLUEPRINT_SCHEMA_SHA256
            else None
        )
    )
    if expanded_schema is None:
        return [{"code": "schema_unavailable", "path": "", "message": "Schema unavailable."}]
    errors = _Errors()
    _validate_schema(document, expanded_schema, errors)
    semantic = document.get("semantic")
    if not errors.items and isinstance(semantic, dict):
        # The persisted semantic vocabulary is the A1 vocabulary without its
        # source claims, so the established cross-reference checks still apply.
        _cross_validate(semantic, errors)
    if not errors.items and isinstance(semantic, dict):
        has_residual = any(
            isinstance(item, dict) and item.get("proof") is not None
            for item in semantic.get("material_hints", [])
        )
        expected = (
            PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_SHA256
            if has_residual
            else PERSISTED_BLUEPRINT_SCHEMA_SHA256
        )
        if schema_sha256 != expected:
            errors.add(
                "schema_digest_mismatch",
                "/schema_sha256",
                "Blueprint semantic does not match its frozen schema profile.",
            )
    return errors.result()


def _decision_payloads(semantic: dict[str, Any]) -> dict[str, Any]:
    intent = semantic.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    return {
        "intent_goal": {
            "goal": intent.get("goal"),
            "audience": intent.get("audience"),
            "platform": intent.get("platform"),
        },
        "intent_stance": {"stance": intent.get("stance")},
        "script": semantic.get("script"),
        "creative_direction": semantic.get("direction"),
        "output": semantic.get("output"),
        "material_constraints": {
            "constraints": semantic.get("constraints"),
            "material_hints": semantic.get("material_hints"),
        },
        "references": {
            "reference_refs": semantic.get("reference_refs"),
            "bindings": semantic.get("bindings"),
        },
        "techniques": semantic.get("technique_refs"),
    }


def _validate_authority(document: dict[str, Any]) -> None:
    authority = document.get("authority")
    if not isinstance(authority, dict) or authority.get("state") != "unverified":
        raise _corrupt()
    units = authority.get("decision_units")
    if not isinstance(units, dict) or set(units) != _DECISION_UNITS:
        raise _corrupt()
    semantic = document.get("semantic")
    if not isinstance(semantic, dict):
        raise _corrupt()
    for name, payload in _decision_payloads(semantic).items():
        unit = units.get(name)
        if not (
            isinstance(unit, dict)
            and set(unit) == {"semantic_sha256", "claim", "verified"}
            and unit.get("claim") == "agent_proposal"
            and unit.get("verified") is False
            and unit.get("semantic_sha256") == canonical_sha256(payload)
        ):
            raise _corrupt()


def _semantic_evidence_refs(semantic: dict[str, Any]) -> list[str]:
    references: set[str] = set()
    constraints = semantic.get("constraints")
    if isinstance(constraints, dict):
        for group in ("must_include", "must_exclude"):
            items = constraints.get(group)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str):
                    references.add(item["evidence_ref"])
    materials = semantic.get("material_hints")
    if isinstance(materials, list):
        for item in materials:
            if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str):
                references.add(item["evidence_ref"])
    external = semantic.get("reference_refs")
    if isinstance(external, list):
        for item in external:
            if (
                isinstance(item, dict)
                and item.get("kind") == "evidence"
                and isinstance(item.get("locator"), str)
            ):
                references.add(item["locator"])
    return sorted(references)


def _semantic_residual_proofs(
    semantic: dict[str, Any],
) -> dict[str, dict[str, object]]:
    proofs: dict[str, dict[str, object]] = {}
    materials = semantic.get("material_hints")
    if not isinstance(materials, list):
        raise _corrupt()
    for item in materials:
        if not isinstance(item, dict) or item.get("proof") is None:
            continue
        evidence_ref = item.get("evidence_ref")
        proof = item.get("proof")
        if (
            not isinstance(evidence_ref, str)
            or not isinstance(proof, dict)
            or set(proof) != {"kind", "residual_binding"}
            or proof.get("kind") != "residual_span"
        ):
            raise _corrupt()
        try:
            binding = require_valid_residual_binding(
                proof.get("residual_binding")  # type: ignore[arg-type]
            )
        except (ResidualAuthorityError, TypeError) as exc:
            raise _corrupt() from exc
        normalized = {
            "kind": "residual_span",
            "residual_binding": binding,
        }
        if evidence_ref != f"memolens://evidence/span/{binding['residual_id']}":
            raise _corrupt()
        prior = proofs.get(evidence_ref)
        if prior is not None and prior != normalized:
            raise _corrupt()
        proofs[evidence_ref] = normalized
    return proofs


def _validate_evidence_manifest(document: dict[str, Any]) -> None:
    semantic = document.get("semantic")
    manifest = document.get("evidence_manifest")
    if not isinstance(semantic, dict) or not isinstance(manifest, list):
        raise _corrupt()
    observed: list[str] = []
    residual_proofs = _semantic_residual_proofs(semantic)
    for item in manifest:
        if not isinstance(item, dict):
            raise _corrupt()
        evidence_ref = item.get("evidence_ref")
        status = item.get("status")
        proof = item.get("proof_sha256")
        if not isinstance(evidence_ref, str):
            raise _corrupt()
        if status == "verified":
            if not _valid_digest(proof):
                raise _corrupt()
        elif status == "unresolved":
            if proof is not None:
                raise _corrupt()
        else:
            raise _corrupt()
        residual_proof = residual_proofs.get(evidence_ref)
        if residual_proof is not None and (
            status != "verified" or proof != canonical_sha256(residual_proof)
        ):
            raise _corrupt()
        observed.append(evidence_ref)
    if observed != sorted(set(observed)) or observed != _semantic_evidence_refs(semantic):
        raise _corrupt()


def _decode_document(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("blueprint_json")
    if not isinstance(raw, str):
        raise _corrupt()
    try:
        document = decode_strict_json(
            raw.encode("utf-8"), limits=PERSISTED_BLUEPRINT_SOURCE_LIMITS
        )
    except (UnicodeError, StrictJsonError) as exc:
        raise _corrupt() from exc
    if not isinstance(document, dict) or _schema_errors(document):
        raise _corrupt()
    exact_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        canonical_digest = canonical_sha256(document)
    except (TypeError, ValueError) as exc:
        raise _corrupt() from exc
    semantic = document.get("semantic")
    if not isinstance(semantic, dict):
        raise _corrupt()
    semantic_digest = canonical_sha256(semantic)
    expected_schema_sha256 = (
        PERSISTED_BLUEPRINT_RESIDUAL_SCHEMA_SHA256
        if _semantic_residual_proofs(semantic)
        else PERSISTED_BLUEPRINT_SCHEMA_SHA256
    )
    parent = document.get("parent")
    if parent is None:
        parent_matches = row.get("parent_revision") is None and row.get("parent_content_sha256") is None
    else:
        parent_matches = bool(
            isinstance(parent, dict)
            and parent.get("revision") == row.get("parent_revision")
            and parent.get("content_sha256") == row.get("parent_content_sha256")
        )
    lineage = document.get("lineage")
    source_candidate = lineage.get("source_candidate_sha256") if isinstance(lineage, dict) else None
    if not (
        exact_digest == canonical_digest == row.get("content_sha256")
        and document.get("object") == PERSISTED_BLUEPRINT_OBJECT
        and document.get("schema_version") == PERSISTED_BLUEPRINT_SCHEMA_VERSION
        and document.get("schema_sha256") == expected_schema_sha256
        and row.get("schema_version") == PERSISTED_BLUEPRINT_SCHEMA_VERSION
        and row.get("schema_sha256") == expected_schema_sha256
        and document.get("project_id") == row.get("project_id")
        and document.get("revision") == row.get("revision")
        and document.get("created_by_operation_id") == row.get("operation_id")
        and document.get("status") == row.get("status", "proposal")
        and row.get("authority_state") == "unverified"
        and _valid_timestamp(row.get("created_at"))
        and semantic_digest == document.get("semantic_sha256") == row.get("semantic_sha256")
        and source_candidate == row.get("source_candidate_sha256")
        and parent_matches
    ):
        raise _corrupt()
    if document["revision"] == 1 and document["parent"] is not None:
        raise _corrupt()
    if document["revision"] > 1 and (
        not isinstance(document["parent"], dict)
        or document["parent"].get("revision") != document["revision"] - 1
    ):
        raise _corrupt()
    _validate_authority(document)
    _validate_evidence_manifest(document)
    return document


def _decode_result(raw: Any) -> dict[str, Any]:
    result = _strict_object(raw)
    if set(result) != {"kind", "result_head", "changed_sections", "restore_from"}:
        raise _corrupt()
    kind = result.get("kind")
    head = result.get("result_head")
    sections = result.get("changed_sections")
    restore = result.get("restore_from")
    if not (
        kind in _RESULT_KINDS
        and isinstance(head, dict)
        and set(head) == {"revision", "content_sha256", "semantic_sha256", "operation_id"}
        and _valid_revision(head.get("revision"))
        and _valid_digest(head.get("content_sha256"))
        and _valid_digest(head.get("semantic_sha256"))
        and _valid_identifier(head.get("operation_id"))
        and isinstance(sections, list)
        and len(sections) <= len(_SEMANTIC_SECTIONS)
        and len(sections) == len(set(sections))
        and all(section in _SEMANTIC_SECTIONS for section in sections)
        and sections
        == [section for section in _SEMANTIC_SECTION_ORDER if section in sections]
    ):
        raise _corrupt()
    if restore is not None and not (
        isinstance(restore, dict)
        and set(restore) == {"revision", "content_sha256"}
        and _valid_revision(restore.get("revision"))
        and _valid_digest(restore.get("content_sha256"))
    ):
        raise _corrupt()
    if (kind == "revision_restored") != (restore is not None):
        raise _corrupt()
    return result


def _validate_auxiliary_operation_json(row: dict[str, Any]) -> dict[str, Any]:
    actor = _strict_object(row.get("actor_json"))
    origin = _strict_object(row.get("origin_json"))
    precondition = _strict_object(row.get("precondition_json"))
    raw_diff = row.get("typed_diff_json")
    if not isinstance(raw_diff, str):
        raise _corrupt()
    try:
        diff = decode_strict_json(
            raw_diff.encode("utf-8"),
            require_object=False,
            limits=_OPERATION_JSON_LIMITS,
        )
    except (UnicodeError, StrictJsonError) as exc:
        raise _corrupt() from exc
    if not isinstance(diff, list) or canonical_json(diff) != raw_diff:
        raise _corrupt()
    sections: list[str] = []
    for item in diff:
        if not (
            isinstance(item, dict)
            and set(item)
            == {"section", "change", "before_sha256", "after_sha256"}
            and item.get("section") in _SEMANTIC_SECTIONS
            and item.get("change") in {"added", "modified"}
            and (item.get("before_sha256") is None or _valid_digest(item.get("before_sha256")))
            and _valid_digest(item.get("after_sha256"))
        ):
            raise _corrupt()
        sections.append(item["section"])
    if (
        len(sections) != len(set(sections))
        or sections
        != [section for section in _SEMANTIC_SECTION_ORDER if section in sections]
    ):
        raise _corrupt()
    return {
        "actor": actor,
        "origin": origin,
        "precondition": precondition,
        "typed_diff": diff,
    }


def _operation_principal(actor: Any, origin: Any) -> str:
    desktop_actor = {
        "identity_verified": True,
        "kind": "desktop_app_command",
        "user_authority_verified": False,
    }
    desktop_origin = {
        "principal": "desktop_app",
        "surface": "desktop_authenticated_api",
    }
    if actor == desktop_actor and origin == desktop_origin:
        return "desktop_app"

    paired_origin = {"principal": "paired_agent", "surface": "agent_cli"}
    if not (
        isinstance(actor, dict)
        and set(actor)
        == {
            "capability_id",
            "claimed_client_label",
            "identity_verified",
            "kind",
            "paired_subject_id",
            "user_authority_verified",
            "vendor_identity_verified",
        }
        and actor.get("kind") == "paired_agent_command"
        and actor.get("identity_verified") is True
        and actor.get("vendor_identity_verified") is False
        and actor.get("user_authority_verified") is False
        and _valid_identifier(actor.get("capability_id"))
        and _valid_identifier(actor.get("paired_subject_id"))
        and isinstance(actor.get("claimed_client_label"), str)
        and 1 <= len(actor["claimed_client_label"]) <= 200
        and not any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in actor["claimed_client_label"]
        )
        and origin == paired_origin
    ):
        raise _corrupt()
    return "paired_agent"


def _validate_operation(
    row: dict[str, Any],
    result_revision: dict[str, Any],
    *,
    expected_typed_diff: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = _decode_result(row.get("result_json"))
    auxiliary = _validate_auxiliary_operation_json(row)
    head = result["result_head"]
    complete_history = row.get("complete_project_history")
    if isinstance(complete_history, bool):
        complete_history_value = complete_history
    elif complete_history in {0, 1}:
        complete_history_value = bool(complete_history)
    else:
        raise _corrupt()
    if not (
        _valid_identifier(row.get("id"))
        and _valid_identifier(row.get("project_id"))
        and _valid_revision(row.get("sequence"))
        and (row.get("parent_operation_id") is None or _valid_identifier(row.get("parent_operation_id")))
        and row.get("schema_version") == "1"
        and row.get("coverage_scope") == "creative_blueprint"
        and complete_history_value is False
        and row.get("command_type") in _COMMAND_TYPES
        and row.get("command_version") == "1"
        and row.get("effect_class") == "reversible_project_write"
        and isinstance(row.get("intent_code"), str)
        and row.get("intent_code") in {"propose_blueprint", "restore_blueprint"}
        and _valid_digest(row.get("input_sha256"))
        and _valid_timestamp(row.get("created_at"))
        and row.get("output_sha256") == head["content_sha256"]
        and row.get("result_kind") == result["kind"]
        and row.get("result_revision") == head["revision"]
        and row.get("result_content_sha256") == head["content_sha256"]
        and result_revision.get("project_id") == row.get("project_id")
        and result_revision.get("revision") == head["revision"]
        and result_revision.get("content_sha256") == head["content_sha256"]
        and result_revision.get("semantic_sha256") == head["semantic_sha256"]
        and result_revision.get("operation_id") == head["operation_id"]
        and [item["section"] for item in auxiliary["typed_diff"]]
        == result["changed_sections"]
    ):
        raise _corrupt()
    typed_diff = auxiliary["typed_diff"]
    precondition = auxiliary["precondition"]
    result_ref = {
        "revision": head["revision"],
        "content_sha256": head["content_sha256"],
    }
    parent_ref = (
        {
            "revision": result_revision.get("parent_revision"),
            "content_sha256": result_revision.get("parent_content_sha256"),
        }
        if result_revision.get("parent_revision") is not None
        else None
    )
    if result["kind"] in _REVISION_RESULT_KINDS and head["operation_id"] != row["id"]:
        raise _corrupt()
    if result["kind"] == "no_change" and head["operation_id"] == row["id"]:
        raise _corrupt()
    if result["kind"] == "no_change":
        if not (
            row["command_type"] == "blueprint.commit_proposal"
            and row["intent_code"] == "propose_blueprint"
            and result["restore_from"] is None
            and result["changed_sections"] == []
            and typed_diff == []
            and set(precondition) == {"expected_head", "restore_from"}
            and _valid_revision_ref(precondition.get("expected_head"))
            and precondition.get("expected_head") == result_ref
            and precondition.get("restore_from") is None
        ):
            raise _corrupt()
    elif result["kind"] == "revision_created":
        revision = result_revision.get("revision")
        initial = precondition.get("initial_legacy_brief")
        if not (
            row["command_type"] == "blueprint.commit_proposal"
            and row["intent_code"] == "propose_blueprint"
            and result["restore_from"] is None
            and set(precondition) == {"expected_head", "initial_legacy_brief"}
            and (
                (
                    revision == 1
                    and precondition.get("expected_head") is None
                    and _valid_revision_ref(initial)
                )
                or (
                    isinstance(revision, int)
                    and revision > 1
                    and _valid_revision_ref(precondition.get("expected_head"))
                    and precondition.get("expected_head") == parent_ref
                    and initial is None
                )
            )
            and row.get("created_at") == result_revision.get("created_at")
        ):
            raise _corrupt()
    elif result["kind"] == "revision_restored":
        restore_from = precondition.get("restore_from")
        if not (
            row["command_type"] == "blueprint.restore_revision"
            and row["intent_code"] == "restore_blueprint"
            and set(precondition) == {"expected_head", "restore_from"}
            and _valid_revision_ref(precondition.get("expected_head"))
            and precondition.get("expected_head") == parent_ref
            and _valid_revision_ref(restore_from)
            and restore_from == result["restore_from"]
            and restore_from["revision"] < result_revision.get("revision", 0)
            and row.get("created_at") == result_revision.get("created_at")
        ):
            raise _corrupt()
    else:
        raise _corrupt()
    if expected_typed_diff is not None and typed_diff != expected_typed_diff:
        raise _corrupt()
    _operation_principal(auxiliary["actor"], auxiliary["origin"])
    return result


def _outline(semantic: dict[str, Any]) -> dict[str, int]:
    script = semantic.get("script")
    script_blocks = script.get("blocks") if isinstance(script, dict) else []
    constraints = semantic.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    return {
        "script_block_count": len(script_blocks) if isinstance(script_blocks, list) else 0,
        "must_include_count": len(constraints.get("must_include", [])) if isinstance(constraints.get("must_include"), list) else 0,
        "must_exclude_count": len(constraints.get("must_exclude", [])) if isinstance(constraints.get("must_exclude"), list) else 0,
        "material_hint_count": len(semantic.get("material_hints", [])) if isinstance(semantic.get("material_hints"), list) else 0,
        "reference_count": len(semantic.get("reference_refs", [])) if isinstance(semantic.get("reference_refs"), list) else 0,
        "technique_count": len(semantic.get("technique_refs", [])) if isinstance(semantic.get("technique_refs"), list) else 0,
        "open_decision_count": len(semantic.get("open_decisions", [])) if isinstance(semantic.get("open_decisions"), list) else 0,
    }


def _authority_summary(projection: dict[str, Any]) -> dict[str, Any]:
    confirmed = projection["confirmed_decision_unit_count"]
    decision_units = projection["decision_unit_count"]
    return {
        "state": projection["state"],
        "all_decision_units_confirmed": confirmed == decision_units,
        "any_decision_unit_confirmed": confirmed > 0,
        "decision_unit_count": decision_units,
        "verified_decision_unit_count": confirmed,
        "claims": (
            ["agent_proposal", "native_user_decision_confirmation"]
            if confirmed
            else ["agent_proposal"]
        ),
    }


def _authority_gaps(projection: dict[str, Any]) -> list[dict[str, str]]:
    confirmed = projection["confirmed_decision_unit_count"]
    total = projection["decision_unit_count"]
    if confirmed == 0:
        return [
            {
                "code": "blueprint_user_authority_unverified",
                "message": (
                    "The persisted Blueprint is an Agent proposal and none of its "
                    "decision units has verified user authority."
                ),
            }
        ]
    if confirmed < total:
        return [
            {
                "code": "blueprint_user_authority_partial",
                "message": (
                    f"The user has confirmed {confirmed} of {total} Blueprint decision "
                    "units; the remaining units are unverified."
                ),
            }
        ]
    return []


def _blueprint_read_gaps(
    projection: dict[str, Any],
    document: dict[str, Any],
) -> list[dict[str, str]]:
    gaps = _authority_gaps(projection)
    semantic = document["semantic"]
    open_decisions = semantic["open_decisions"]
    if open_decisions:
        gaps.append(
            {
                "code": "blueprint_open_decisions_unresolved",
                "message": (
                    f"The Blueprint still has {len(open_decisions)} explicit open "
                    "decision(s); unit confirmation does not resolve them."
                ),
            }
        )
    gaps.extend(
        [
            {
                "code": "blueprint_timeline_compiler_unavailable",
                "message": (
                    "This slice cannot yet compile the Blueprint into a "
                    "Blueprint-bound Timeline."
                ),
            },
            {
                "code": "blueprint_publish_authority_unavailable",
                "message": (
                    "Decision-unit confirmation is not approval to render, export, "
                    "publish, move, upload, or delete files."
                ),
            },
        ]
    )
    return gaps


def _blueprint_projection(
    row: dict[str, Any],
    document: dict[str, Any],
    authority_projection: dict[str, Any],
) -> dict[str, Any]:
    semantic = document["semantic"]
    return {
        # This is a safe read projection with row metadata and summaries, not
        # the exact frozen persisted document (whose schema forbids extras).
        "object": PERSISTED_BLUEPRINT_PROJECTION_OBJECT,
        "schema_version": document["schema_version"],
        "schema_sha256": document["schema_sha256"],
        "project_id": row["project_id"],
        "revision": row["revision"],
        "parent": document["parent"],
        "created_by_operation_id": row["operation_id"],
        "status": "proposal",
        "content_sha256": row["content_sha256"],
        "semantic_sha256": row["semantic_sha256"],
        "created_at": row["created_at"],
        "semantic": semantic,
        "lineage": document["lineage"],
        "evidence_manifest": document["evidence_manifest"],
        "authority": document["authority"],
        "creation_authority": {"state": "unverified", "verified": False},
        "authority_projection": authority_projection,
        "authority_summary": _authority_summary(authority_projection),
        "outline": _outline(semantic),
        "data_trust": "untrusted_data",
        "untrusted_data_fields": ["semantic"],
    }


def _operation_projection(row: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": row["id"],
        "sequence": row["sequence"],
        "parent_operation_id": row["parent_operation_id"],
        "coverage_scope": "creative_blueprint",
        "complete_project_history": False,
        "command_type": row["command_type"],
        "command_version": row["command_version"],
        "effect_class": row["effect_class"],
        "intent_code": row["intent_code"],
        "result_kind": row["result_kind"],
        "result_revision": row["result_revision"],
        "result_content_sha256": row["result_content_sha256"],
        "changed_sections": result["changed_sections"],
        "restore_from": result["restore_from"],
        "created_at": row["created_at"],
    }


_RevisionBundle = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]


class _ValidatedRevisionChain(Mapping[int, _RevisionBundle]):
    """A fully validated revision range with a bounded bundle cache.

    The stable private SQLite snapshot is the backing store.  Validation walks
    every document once, but the read projection retains at most two decoded
    documents instead of one potentially 1 MiB document for every legal
    revision.  Scalar identities used by the operation chain never decode JSON.
    """

    def __init__(
        self,
        *,
        reader: "PersistedBlueprintReader",
        connection: sqlite3.Connection,
        project_id: str,
        maximum_revision: int,
        initial: tuple[int, _RevisionBundle] | None = None,
    ) -> None:
        self._reader = reader
        self._connection = connection
        self._project_id = project_id
        self._maximum_revision = maximum_revision
        self._cache: OrderedDict[int, _RevisionBundle] = OrderedDict()
        if initial is not None:
            self._remember(*initial)

    def __len__(self) -> int:
        return self._maximum_revision

    def __iter__(self) -> Iterator[int]:
        return iter(range(1, self._maximum_revision + 1))

    def iter_bundles(self) -> Iterator[tuple[int, _RevisionBundle]]:
        """Replay the validated range through one cursor with a two-bundle cache."""

        try:
            rows = self._connection.execute(
                f"SELECT {self._reader._revision_select()},"
                f"{self._reader._operation_select()} "
                "FROM creative_blueprint_revisions r "
                "JOIN creative_blueprint_operations o "
                "ON o.project_id=r.project_id AND o.id=r.operation_id "
                "WHERE r.project_id=? AND r.revision<=? ORDER BY r.revision",
                (self._project_id, self._maximum_revision),
            )
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.DataError):
                raise _corrupt() from exc
            raise MemoLensError(
                "The Creative Blueprint revision chain could not be streamed.",
                code="database_unavailable",
            ) from exc
        observed = 0
        for expected_revision, raw in enumerate(rows, start=1):
            observed += 1
            revision_row = self._reader._row_with_prefix(raw, "r_")
            if revision_row.get("revision") != expected_revision:
                raise _corrupt()
            bundle = self._reader._bundle_from_joined_row(
                raw,
            )
            self._remember(expected_revision, bundle)
            yield expected_revision, bundle
        if observed != self._maximum_revision:
            raise _corrupt()

    def __getitem__(self, revision: int) -> _RevisionBundle:
        if type(revision) is not int or not 1 <= revision <= self._maximum_revision:
            raise KeyError(revision)
        cached = self._cache.pop(revision, None)
        if cached is not None:
            self._cache[revision] = cached
            return cached
        bundle = self._reader._load_revision_bundle(
            self._connection,
            self._project_id,
            revision,
        )
        self._remember(revision, bundle)
        return bundle

    def _remember(self, revision: int, bundle: _RevisionBundle) -> None:
        self._cache[revision] = bundle
        self._cache.move_to_end(revision)
        while len(self._cache) > _REVISION_BUNDLE_CACHE_SIZE:
            self._cache.popitem(last=False)


class PersistedBlueprintReader:
    """Read current/exact Blueprint revisions and their bounded operation ledger."""

    def __init__(
        self,
        database: ReadOnlyDatabase,
        projects: ProjectIndexReader,
    ) -> None:
        self.database = database
        self.projects = projects
        self.media = MediaIndexReader(database)

    def _admit_executable_material(
        self,
        connection: sqlite3.Connection,
        document: dict[str, Any],
    ) -> None:
        """Reject exported derivatives before a Blueprint can feed Coverage.

        ``reference_refs`` and constraint evidence remain reference-only.  The
        Coverage contract turns only ``material_hints`` into assignments, so
        only those references enter this admission boundary.
        """

        semantic = document.get("semantic")
        materials = semantic.get("material_hints") if isinstance(semantic, dict) else None
        if not isinstance(materials, list) or not materials:
            return
        material_rows = [
            item
            for item in materials
            if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
        ]
        if not material_rows:
            return
        self.media.validate_no_persisted_residual_rows(connection)
        # Local import avoids a module-initialization cycle: Export authority
        # uses this reader's strict document decoder to reconstruct script facts.
        from memolens_export_authority import project_validated_derivative_facts

        validate_exact_managed_schema_manifest(connection)
        facts = project_validated_derivative_facts(connection)
        derivative_sha256s = facts["video_sha256"]
        if not isinstance(derivative_sha256s, frozenset):
            raise MemoLensError(
                "Canonical Export derivative facts are invalid.",
                code="derivative_authority_integrity_error",
            )
        residual_rows = [
            item
            for item in material_rows
            if str(item["evidence_ref"]).startswith(
                "memolens://evidence/span/rseg_"
            )
        ]
        occurrences: list[dict[str, object]] = []
        usage_revision: str | None = None
        if residual_rows:
            raw_occurrences = facts.get("usage_occurrences")
            if not isinstance(raw_occurrences, list) or any(
                not isinstance(item, dict) for item in raw_occurrences
            ):
                raise MemoLensError(
                    "Canonical Usage facts are invalid.",
                    code="usage_authority_integrity_error",
                )
            try:
                occurrences, usage_revision = current_candidate_usage_facts(
                    connection,
                    raw_occurrences,
                )
            except ResidualAuthorityError as exc:
                raise MemoLensError(str(exc), code=exc.code) from exc
        for item in sorted(material_rows, key=lambda row: str(row["evidence_ref"])):
            evidence_ref = str(item["evidence_ref"])
            if evidence_ref.startswith("memolens://evidence/span/rseg_"):
                assert usage_revision is not None
                try:
                    verified = revalidate_residual_proof_in_connection(
                        connection,
                        evidence_ref=evidence_ref,
                        presented_proof=item.get("proof"),
                        occurrences=occurrences,
                        usage_revision=usage_revision,
                        derivative_sha256s=derivative_sha256s,
                    )
                except ResidualAuthorityError as exc:
                    raise MemoLensError(str(exc), code=exc.code) from exc
                if verified is None:
                    raise MemoLensError(
                        "The persisted Blueprint residual evidence is stale.",
                        code="blueprint_residual_evidence_stale",
                    )
                continue
            try:
                reference = parse_evidence_id(evidence_ref)
            except MemoLensError as exc:
                raise _corrupt() from exc
            proof = self.media.evidence_proof_in_connection(
                connection,
                kind=reference.kind,
                identifier=reference.identifier,
            )
            if proof is not None and proof["asset_sha256"] in derivative_sha256s:
                raise MemoLensError(
                    "The persisted Blueprint selects a canonical Export derivative as executable material.",
                    code="canonical_derivative_executable_material_forbidden",
                )

    def schema_capabilities(self, connection: sqlite3.Connection) -> dict[str, Any]:
        observed = {
            table: self.database.columns(connection, table) for table in _RELATIONS
        }
        authority_observed = {
            table: self.database.columns(connection, table)
            for table in AUTHORITY_RELATIONS
        }
        present = {table for table, columns in observed.items() if columns}
        if present and present != set(_RELATIONS):
            raise MemoLensError(
                "The persisted Creative Blueprint relation set is incomplete.",
                code="blueprint_schema_invalid",
            )
        if present:
            for table, required in _RELATIONS.items():
                if not required <= observed[table]:
                    raise MemoLensError(
                        "The persisted Creative Blueprint relation schema is invalid.",
                        code="blueprint_schema_invalid",
                    )
        relation_available = present == set(_RELATIONS)
        authority_available = authority_schema_available(
            connection,
            authority_observed,
        )
        head_count = 0
        revision_count = 0
        operation_count = 0
        authority_event_count = 0
        if relation_available:
            try:
                head_count = int(connection.execute("SELECT COUNT(*) FROM creative_blueprint_heads").fetchone()[0])
                revision_count = int(connection.execute("SELECT COUNT(*) FROM creative_blueprint_revisions").fetchone()[0])
                operation_count = int(connection.execute("SELECT COUNT(*) FROM creative_blueprint_operations WHERE coverage_scope='creative_blueprint'").fetchone()[0])
                if authority_available:
                    authority_event_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM blueprint_decision_authority_events"
                        ).fetchone()[0]
                    )
            except sqlite3.Error as exc:
                raise MemoLensError("Persisted Creative Blueprint counts could not be read.", code="database_unavailable") from exc
        read_available = bool(relation_available and PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE)
        return {
            "persisted_blueprint_schema_available": PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE,
            "persisted_blueprint_relation_available": relation_available,
            "persisted_blueprint_read_available": read_available,
            "blueprint_operation_ledger_available": read_available,
            "blueprint_decision_authority_available": authority_available,
            "complete_project_history": False,
            "blueprint_head_count": head_count,
            "blueprint_revision_count": revision_count,
            "blueprint_operation_count": operation_count,
            "blueprint_authority_event_count": authority_event_count,
        }

    @staticmethod
    def _row_with_prefix(row: sqlite3.Row, prefix: str) -> dict[str, Any]:
        return {
            key.removeprefix(prefix): row[key]
            for key in row.keys()
            if key.startswith(prefix)
        }

    @staticmethod
    def _revision_select(alias: str = "r", prefix: str = "r_") -> str:
        return ",".join(f"{alias}.{column} AS {prefix}{column}" for column in sorted(_REVISION_COLUMNS))

    @staticmethod
    def _operation_select(alias: str = "o", prefix: str = "o_") -> str:
        return ",".join(f"{alias}.{column} AS {prefix}{column}" for column in sorted(_OPERATION_COLUMNS))

    @staticmethod
    def _revision_identity_select(alias: str = "r", prefix: str = "i_") -> str:
        return ",".join(
            f"{alias}.{column} AS {prefix}{column}"
            for column in _REVISION_IDENTITY_COLUMNS
        )

    @staticmethod
    def _assert_stored_json_budget(
        connection: sqlite3.Connection,
        *,
        from_clause: str,
        where_clause: str,
        parameters: tuple[Any, ...] | list[Any],
        columns: tuple[str, ...],
        max_rows: int,
        per_value_max_bytes: int,
        aggregate_max_bytes: int,
        order_by: str = "",
    ) -> int:
        """Validate stored JSON byte sizes before selecting any JSON text.

        All SQL fragments and column names are module-owned constants. The
        private snapshot cannot change between this preflight and the later
        read, so a successful check is a stable bound rather than a racy hint.
        """

        expressions = ",".join(
            expression
            for column in columns
            for expression in (
                f"typeof({column})",
                f"length(CAST({column} AS BLOB))",
            )
        )
        try:
            rows = connection.execute(
                f"SELECT {expressions} FROM {from_clause} "
                f"WHERE {where_clause}{order_by}",
                parameters,
            )
            observed = 0
            aggregate = 0
            for raw in rows:
                observed += 1
                if observed > max_rows:
                    raise _corrupt()
                for offset in range(0, len(raw), 2):
                    storage_type = raw[offset]
                    byte_length = raw[offset + 1]
                    if not (
                        storage_type == "text"
                        and type(byte_length) is int
                        and 0 <= byte_length <= per_value_max_bytes
                    ):
                        raise _corrupt()
                    aggregate += byte_length
                    if aggregate > aggregate_max_bytes:
                        raise _corrupt()
        except MemoLensError:
            raise
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.DataError):
                raise _corrupt() from exc
            raise MemoLensError(
                "Persisted Creative Blueprint JSON bounds could not be verified.",
                code="database_unavailable",
            ) from exc
        if observed == 0:
            raise _corrupt()
        return observed

    def _preflight_project_json(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        head_revision: int,
    ) -> int:
        """Bound the complete project ledger before decoding its first value."""

        revision_count = self._assert_stored_json_budget(
            connection,
            from_clause="creative_blueprint_revisions r",
            where_clause="r.project_id=?",
            parameters=(project_id,),
            columns=("r.blueprint_json",),
            max_rows=_MAX_BLUEPRINT_LEDGER_ROWS,
            per_value_max_bytes=PERSISTED_BLUEPRINT_SOURCE_LIMITS.max_bytes,
            aggregate_max_bytes=_REVISION_PROJECT_STORED_JSON_MAX_BYTES,
        )
        operation_count = self._assert_stored_json_budget(
            connection,
            from_clause="creative_blueprint_operations o",
            where_clause="o.project_id=?",
            parameters=(project_id,),
            columns=_OPERATION_JSON_COLUMNS,
            max_rows=_MAX_BLUEPRINT_LEDGER_ROWS,
            per_value_max_bytes=_OPERATION_JSON_LIMITS.max_bytes,
            aggregate_max_bytes=_OPERATION_PROJECT_STORED_JSON_MAX_BYTES,
        )
        if revision_count != head_revision or operation_count < revision_count:
            raise _corrupt()
        return operation_count

    def _bundle_from_joined_row(
        self,
        raw: sqlite3.Row,
        *,
        decoded_document: dict[str, Any] | None = None,
        expected_typed_diff: list[dict[str, Any]] | None = None,
    ) -> _RevisionBundle:
        revision_row = self._row_with_prefix(raw, "r_")
        operation_row = self._row_with_prefix(raw, "o_")
        document = (
            decoded_document
            if decoded_document is not None
            else _decode_document(revision_row)
        )
        result = _validate_operation(
            operation_row,
            revision_row,
            expected_typed_diff=expected_typed_diff,
        )
        if operation_row["result_kind"] not in _REVISION_RESULT_KINDS:
            raise _corrupt()
        return revision_row, document, {"row": operation_row, "result": result}

    def _validate_joined_restore_target(
        self,
        raw: sqlite3.Row,
        *,
        restored_document: dict[str, Any],
        restore_from: dict[str, Any] | None,
    ) -> None:
        """Validate one restore target without retaining revision-wide state."""

        target_row = self._row_with_prefix(raw, "t_")
        if restore_from is None:
            if any(value is not None for value in target_row.values()):
                raise _corrupt()
            return
        if not (
            target_row.get("revision") == restore_from["revision"]
            and target_row.get("content_sha256")
            == restore_from["content_sha256"]
        ):
            raise _corrupt()
        target_document = _decode_document(target_row)
        if _restore_material_sha256(target_document) != _restore_material_sha256(
            restored_document
        ):
            raise _corrupt()

    def _load_revision_bundle(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        revision: int,
    ) -> _RevisionBundle:
        """Reload one bundle after the stable snapshot's full-chain validation."""

        try:
            raw = connection.execute(
                f"SELECT {self._revision_select()},{self._operation_select()} "
                "FROM creative_blueprint_revisions r "
                "JOIN creative_blueprint_operations o "
                "ON o.project_id=r.project_id AND o.id=r.operation_id "
                "WHERE r.project_id=? AND r.revision=? LIMIT 1",
                (project_id, revision),
            ).fetchone()
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.DataError):
                raise _corrupt() from exc
            raise MemoLensError(
                "The Creative Blueprint revision could not be read.",
                code="database_unavailable",
            ) from exc
        if raw is None:
            raise _corrupt()
        return self._bundle_from_joined_row(
            raw,
        )

    def _validate_revision_chain(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        head: dict[str, Any],
    ) -> _ValidatedRevisionChain:
        try:
            rows = connection.execute(
                f"SELECT {self._revision_select()},{self._operation_select()},"
                f"{self._revision_select(alias='t', prefix='t_')} "
                "FROM creative_blueprint_revisions r "
                "JOIN creative_blueprint_operations o "
                "ON o.project_id=r.project_id AND o.id=r.operation_id "
                "LEFT JOIN creative_blueprint_revisions t "
                "ON t.project_id=r.project_id "
                "AND o.result_kind='revision_restored' "
                "AND t.revision=CASE WHEN json_valid(o.result_json) "
                "THEN json_extract(o.result_json,'$.restore_from.revision') "
                "ELSE NULL END "
                "WHERE r.project_id=? ORDER BY r.revision",
                (project_id,),
            )
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.DataError):
                raise _corrupt() from exc
            raise MemoLensError(
                "The Creative Blueprint revision chain could not be read.",
                code="database_unavailable",
            ) from exc

        previous_row: dict[str, Any] | None = None
        previous_document: dict[str, Any] | None = None
        latest: _RevisionBundle | None = None
        observed = 0
        for expected_revision, raw in enumerate(rows, start=1):
            observed += 1
            revision_row = self._row_with_prefix(raw, "r_")
            if revision_row.get("revision") != expected_revision:
                raise _corrupt()
            expected_parent = (
                None
                if previous_row is None
                else {
                    "revision": expected_revision - 1,
                    "content_sha256": previous_row["content_sha256"],
                }
            )
            document_preview = _decode_document(revision_row)
            if document_preview["parent"] != expected_parent:
                raise _corrupt()
            expected_diff = _semantic_diff(
                previous_document["semantic"] if previous_document is not None else None,
                document_preview["semantic"],
            )
            latest = self._bundle_from_joined_row(
                raw,
                decoded_document=document_preview,
                expected_typed_diff=expected_diff,
            )
            self._validate_joined_restore_target(
                raw,
                restored_document=document_preview,
                restore_from=latest[2]["result"]["restore_from"],
            )
            previous_row, previous_document, _operation = latest

        if observed != head["revision"] or latest is None:
            raise _corrupt()
        latest_row = latest[0]
        if any(
            latest_row[key] != head[key]
            for key in (
                "project_id",
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        ):
            raise _corrupt()
        return _ValidatedRevisionChain(
            reader=self,
            connection=connection,
            project_id=project_id,
            maximum_revision=observed,
            initial=(observed, latest),
        )

    def _current_bundle(
        self, connection: sqlite3.Connection, project_id: str
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        _ValidatedRevisionChain,
    ] | None:
        try:
            head = connection.execute(
                "SELECT project_id,revision,content_sha256,semantic_sha256,operation_id,updated_at "
                "FROM creative_blueprint_heads WHERE project_id=? LIMIT 1",
                (project_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MemoLensError("The Creative Blueprint head could not be read.", code="database_unavailable") from exc
        if head is None:
            orphan = connection.execute(
                "SELECT 1 FROM creative_blueprint_revisions WHERE project_id=? "
                "UNION ALL SELECT 1 FROM creative_blueprint_operations "
                "WHERE project_id=? LIMIT 1",
                (project_id, project_id),
            ).fetchone()
            if orphan is not None:
                raise _corrupt()
            return None
        head_row = dict(head)
        if not (
            head_row.get("project_id") == project_id
            and _valid_revision(head_row.get("revision"))
            and _valid_digest(head_row.get("content_sha256"))
            and _valid_digest(head_row.get("semantic_sha256"))
            and _valid_identifier(head_row.get("operation_id"))
            and _valid_timestamp(head_row.get("updated_at"))
        ):
            raise _corrupt()
        operation_count = self._preflight_project_json(
            connection,
            project_id,
            int(head_row["revision"]),
        )
        # The B0 ledger is linear: restore also appends a new revision.  The
        # explicit head selects the current row, but it may never be rewound to
        # hide a later immutable revision.  MAX is used only as an integrity
        # assertion here, never as a fallback selector.
        try:
            newer_revision = connection.execute(
                "SELECT 1 FROM creative_blueprint_revisions "
                "WHERE project_id=? AND revision>? LIMIT 1",
                (project_id, head_row["revision"]),
            ).fetchone()
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The Creative Blueprint revision chain could not be verified.",
                code="database_unavailable",
            ) from exc
        if newer_revision is not None:
            raise _corrupt()
        revision_chain = self._validate_revision_chain(
            connection,
            project_id,
            head_row,
        )
        revision_row, document, operation = revision_chain[head_row["revision"]]
        if operation["result"]["result_head"] != {
            "revision": head_row["revision"],
            "content_sha256": head_row["content_sha256"],
            "semantic_sha256": head_row["semantic_sha256"],
            "operation_id": head_row["operation_id"],
        }:
            raise _corrupt()
        self._validate_operation_chain(
            connection,
            project_id,
            head_row,
            expected_operation_count=operation_count,
        )
        return head_row, revision_row, document, operation, revision_chain

    def _require_project(self, connection: sqlite3.Connection, project_id: str) -> None:
        if not self.projects.exists_in_connection(connection, project_id):
            raise MemoLensError("Creative project was not found.", code="project_not_found")

    @staticmethod
    def _base(
        object_name: str,
        *,
        status: str,
        capability_available: bool,
        project_id: str,
        gaps: list[dict[str, str]],
        operation_receipts_validated: bool = False,
    ) -> dict[str, Any]:
        return {
            "object": object_name,
            "schema_version": PERSISTED_BLUEPRINT_READ_SCHEMA_VERSION,
            "status": status,
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "capability_available": capability_available,
            "project_id": project_id,
            "contract": persisted_blueprint_contract_summary(),
            "gaps": gaps,
            "safety": persisted_blueprint_safety_summary(
                operation_receipts_validated=operation_receipts_validated
            ),
        }

    def get_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        revision: int | None = None,
    ) -> dict[str, Any]:
        normalized = validate_project_identifier(project_id)
        if revision is not None and not _valid_revision(revision):
            raise MemoLensError("revision is invalid.", code="invalid_argument")
        self._require_project(connection, normalized)
        capabilities = self.schema_capabilities(connection)
        if not capabilities["persisted_blueprint_read_available"]:
            if capabilities["persisted_blueprint_relation_available"]:
                head_exists = connection.execute(
                    "SELECT 1 FROM creative_blueprint_heads WHERE project_id=? LIMIT 1",
                    (normalized,),
                ).fetchone()
                if head_exists is not None:
                    raise MemoLensError(
                        "The frozen persisted Creative Blueprint schema is unavailable.",
                        code="blueprint_schema_unavailable",
                    )
            return {
                **self._base(
                    "memolens.creative_blueprint_read",
                    status="capability_unavailable",
                    capability_available=False,
                    project_id=normalized,
                    gaps=[{"code": "persisted_blueprint_read_unavailable", "message": "The frozen persisted Blueprint schema or relation set is unavailable."}],
                ),
                "requested_revision": revision,
                "selection": None,
                "blueprint": None,
                "operation": None,
            }
        current = self._current_bundle(connection, normalized)
        if current is None:
            if revision is not None:
                raise MemoLensError("The project has no persisted Creative Blueprint head.", code="blueprint_head_not_found")
            return {
                **self._base(
                    "memolens.creative_blueprint_read",
                    status="incomplete",
                    capability_available=True,
                    project_id=normalized,
                    gaps=[{"code": "blueprint_head_unavailable", "message": "This project has no persisted Creative Blueprint head."}],
                ),
                "requested_revision": None,
                "selection": {"kind": "current", "explicit_head": True, "is_current": False, "current_head": None},
                "blueprint": None,
                "operation": None,
            }
        head, revision_row, document, operation, revision_chain = current
        operation_receipts_validated = False
        if capabilities["blueprint_decision_authority_available"]:
            receipt_validation = validate_agent_receipt_ledger(
                connection,
                project_id=normalized,
            )
            operation_receipts_validated = receipt_validation["validated"] is True
        if revision is not None and revision != head["revision"]:
            exists = connection.execute(
                "SELECT 1 FROM creative_blueprint_revisions "
                "WHERE project_id=? AND revision=? LIMIT 1",
                (normalized, revision),
            ).fetchone()
            if exists is None:
                raise MemoLensError(
                    "The exact Creative Blueprint revision was not found.",
                    code="blueprint_revision_not_found",
                )
            revision_row, document, operation = revision_chain[revision]
        if revision is None:
            self._admit_executable_material(connection, document)
        is_current = revision_row["revision"] == head["revision"] and revision_row["content_sha256"] == head["content_sha256"]
        authority_projection = validate_and_project_authority(
            connection,
            project_id=normalized,
            current_head=head,
            revision_chain=revision_chain,
            selected_revision=revision_row["revision"],
            authority_available=capabilities["blueprint_decision_authority_available"],
        )
        return {
            **self._base(
                "memolens.creative_blueprint_read",
                status="completed",
                capability_available=True,
                project_id=normalized,
                gaps=_blueprint_read_gaps(authority_projection, document),
                operation_receipts_validated=operation_receipts_validated,
            ),
            "requested_revision": revision,
            "selection": {
                "kind": "exact_revision" if revision is not None else "current",
                "explicit_head": True,
                "is_current": is_current,
                "current_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                    "semantic_sha256": head["semantic_sha256"],
                    "operation_id": head["operation_id"],
                },
            },
            "blueprint": _blueprint_projection(
                revision_row,
                document,
                authority_projection,
            ),
            "operation": _operation_projection(operation["row"], operation["result"]),
        }

    def get(self, project_id: str, revision: int | None = None) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            return self.get_in_connection(connection, project_id, revision)

    def _validate_operation_chain(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        head: dict[str, Any],
        *,
        expected_operation_count: int,
    ) -> None:
        try:
            rows = connection.execute(
                f"SELECT {self._operation_select()},"
                f"{self._revision_identity_select()} "
                "FROM creative_blueprint_operations o "
                "JOIN creative_blueprint_revisions r "
                "ON r.project_id=o.project_id AND r.revision=o.result_revision "
                "WHERE o.project_id=? "
                "ORDER BY o.sequence",
                (project_id,),
            )
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.DataError):
                raise _corrupt() from exc
            raise MemoLensError("The Blueprint operation chain could not be verified.", code="database_unavailable") from exc
        previous_id: str | None = None
        state_head: dict[str, Any] | None = None
        observed = 0
        for expected_sequence, raw in enumerate(rows, start=1):
            observed += 1
            operation_row = self._row_with_prefix(raw, "o_")
            if not (
                operation_row.get("sequence") == expected_sequence
                and operation_row.get("parent_operation_id") == previous_id
            ):
                raise _corrupt()
            result = _decode_result(operation_row.get("result_json"))
            result_head = result["result_head"]
            result_revision = self._row_with_prefix(raw, "i_")
            if result_revision.get("revision") != result_head["revision"]:
                raise _corrupt()
            validated = _validate_operation(operation_row, result_revision)
            if validated != result:
                raise _corrupt()
            if result["kind"] == "no_change":
                if state_head is None or result_head != state_head:
                    raise _corrupt()
            else:
                expected_revision = 1 if state_head is None else state_head["revision"] + 1
                expected_parent = (
                    None
                    if state_head is None
                    else {
                        "revision": state_head["revision"],
                        "content_sha256": state_head["content_sha256"],
                    }
                )
                if not (
                    result_head["revision"] == expected_revision
                    and result_revision.get("parent_revision")
                    == (expected_parent["revision"] if expected_parent else None)
                    and result_revision.get("parent_content_sha256")
                    == (expected_parent["content_sha256"] if expected_parent else None)
                ):
                    raise _corrupt()
                state_head = result_head
            previous_id = operation_row["id"]
        if observed != expected_operation_count:
            raise _corrupt()
        if state_head != {
            "revision": head["revision"],
            "content_sha256": head["content_sha256"],
            "semantic_sha256": head["semantic_sha256"],
            "operation_id": head["operation_id"],
        }:
            raise _corrupt()

    def history_in_connection(
        self, connection: sqlite3.Connection, project_id: str, limit: int
    ) -> dict[str, Any]:
        normalized = validate_project_identifier(project_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MemoLensError("limit must be an integer between 1 and 100.", code="invalid_argument")
        self._require_project(connection, normalized)
        capabilities = self.schema_capabilities(connection)
        if not capabilities["blueprint_operation_ledger_available"]:
            return {
                **self._base(
                    "memolens.creative_blueprint_history",
                    status="capability_unavailable",
                    capability_available=False,
                    project_id=normalized,
                    gaps=[{"code": "blueprint_operation_ledger_unavailable", "message": "The Blueprint-only operation ledger is unavailable."}],
                ),
                "coverage_scope": "creative_blueprint",
                "complete_project_history": False,
                "current_head": None,
                "result_count": 0,
                "operations": [],
                "truncated": False,
            }
        current = self._current_bundle(connection, normalized)
        if current is None:
            return {
                **self._base(
                    "memolens.creative_blueprint_history",
                    status="incomplete",
                    capability_available=True,
                    project_id=normalized,
                    gaps=[{"code": "blueprint_head_unavailable", "message": "This project has no persisted Creative Blueprint head."}],
                ),
                "coverage_scope": "creative_blueprint",
                "complete_project_history": False,
                "current_head": None,
                "result_count": 0,
                "operations": [],
                "truncated": False,
            }
        head, _revision_row, _document, _operation, revision_chain = current
        operation_receipts_validated = False
        if capabilities["blueprint_decision_authority_available"]:
            receipt_validation = validate_agent_receipt_ledger(
                connection,
                project_id=normalized,
            )
            operation_receipts_validated = receipt_validation["validated"] is True
        validated_authority_events = validate_authority_ledger(
            connection,
            project_id=normalized,
            current_head=head,
            revision_chain=revision_chain,
            authority_available=capabilities[
                "blueprint_decision_authority_available"
            ],
        )
        current_authority_projection = project_validated_authority(
            revision_chain=revision_chain,
            validated_events=validated_authority_events,
            selected_revision=head["revision"],
        )
        try:
            rows = connection.execute(
                f"SELECT {self._operation_select()} FROM creative_blueprint_operations o "
                "WHERE o.project_id=? AND o.coverage_scope='creative_blueprint' "
                "ORDER BY o.sequence DESC LIMIT ?",
                (normalized, limit + 1),
            )
        except sqlite3.Error as exc:
            if isinstance(exc, sqlite3.DataError):
                raise _corrupt() from exc
            raise MemoLensError("Creative Blueprint history could not be read.", code="database_unavailable") from exc
        projected: list[dict[str, Any]] = []
        truncated = False
        for index, raw in enumerate(rows):
            if index >= limit:
                truncated = True
                break
            operation_row = self._row_with_prefix(raw, "o_")
            result = _decode_result(operation_row.get("result_json"))
            try:
                result_revision, result_document, _creation = revision_chain[
                    result["result_head"]["revision"]
                ]
            except KeyError as exc:
                raise _corrupt() from exc
            if not (
                result_revision["content_sha256"]
                == result["result_head"]["content_sha256"]
                and result_revision["semantic_sha256"]
                == result["result_head"]["semantic_sha256"]
                and result_revision["operation_id"]
                == result["result_head"]["operation_id"]
            ):
                raise _corrupt()
            validated_result = _validate_operation(operation_row, result_revision)
            semantic = result_document["semantic"]
            authority_projection = project_validated_authority(
                revision_chain=revision_chain,
                validated_events=validated_authority_events,
                selected_revision=result_revision["revision"],
            )
            projected.append(
                {
                    **_operation_projection(operation_row, validated_result),
                    "result_head": {
                        "revision": result_revision["revision"],
                        "content_sha256": result_revision["content_sha256"],
                        "semantic_sha256": result_revision["semantic_sha256"],
                        "operation_id": result_revision["operation_id"],
                        "status": "proposal",
                        "creation_authority": {
                            "state": "unverified",
                            "verified": False,
                        },
                        "authority_projection": authority_projection,
                        "outline": _outline(semantic),
                    },
                }
            )
        if not projected:
            raise _corrupt()
        latest_result = projected[0]["result_head"]
        if not (
            latest_result["revision"] == head["revision"]
            and latest_result["content_sha256"] == head["content_sha256"]
            and latest_result["semantic_sha256"] == head["semantic_sha256"]
            and latest_result["operation_id"] == head["operation_id"]
        ):
            raise _corrupt()
        return {
            **self._base(
                "memolens.creative_blueprint_history",
                status="completed",
                capability_available=True,
                project_id=normalized,
                gaps=[
                    *_authority_gaps(current_authority_projection),
                    {"code": "complete_project_history_unavailable", "message": "This ledger covers Creative Blueprint operations only, not the complete project history."},
                ],
                operation_receipts_validated=operation_receipts_validated,
            ),
            "coverage_scope": "creative_blueprint",
            "complete_project_history": False,
            "current_head": {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
                "semantic_sha256": head["semantic_sha256"],
                "operation_id": head["operation_id"],
            },
            "result_count": len(projected),
            "operations": projected,
            "truncated": truncated,
        }

    def history(self, project_id: str, limit: int = 50) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            return self.history_in_connection(connection, project_id, limit)


__all__ = [
    "PERSISTED_BLUEPRINT_SCHEMA_AVAILABLE",
    "PERSISTED_BLUEPRINT_SCHEMA_SHA256",
    "PersistedBlueprintReader",
    "persisted_blueprint_contract_summary",
]
