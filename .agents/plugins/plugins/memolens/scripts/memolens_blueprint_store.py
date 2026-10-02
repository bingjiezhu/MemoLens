"""Single-snapshot Blueprint shadow and candidate binding validation."""

from __future__ import annotations

from contextlib import closing
import re
import sqlite3
from typing import Any

from memolens_contracts import MemoLensError
from memolens_blueprint_authority import validate_exact_managed_schema_manifest
from memolens_creative_blueprint import (
    BLUEPRINT_SCHEMA_AVAILABLE,
    BLUEPRINT_SHADOW_OBJECT,
    BLUEPRINT_VALIDATION_OBJECT,
    blueprint_contract_summary,
    blueprint_safety_summary,
    candidate_outline,
    canonical_sha256,
    declared_sources,
    has_starting_input,
    inspect_blueprint_candidate,
    project_legacy_brief_to_candidate,
    stable_codes,
    strict_persisted_object,
    valid_user_text_reference,
)
from memolens_creator_store import CreatorMemoryReader
from memolens_export_authority import project_validated_derivative_facts
from memolens_media_store import MediaIndexReader
from memolens_project_store import ProjectIndexReader, validate_project_identifier
from memolens_residual_authority import (
    ResidualAuthorityError,
    current_candidate_usage_facts,
    revalidate_residual_proof_in_connection,
)
from memolens_sqlite import ReadOnlyDatabase
from memolens_timeline import TIMELINE_SCHEMA_VERSION, validate_timeline
from memolens_wiki import parse_evidence_id


_GAP_MESSAGES = {
    "authoritative_decision_ledger_unavailable": "No operation ledger can verify user decisions in this slice.",
    "blueprint_shadow_non_authoritative": "The candidate is a derived legacy projection, not a persisted Blueprint revision.",
    "candidate_non_authoritative": "Schema-valid candidate data is not an authoritative project decision.",
    "candidate_not_persisted": "Validation does not persist a Blueprint or modify the project.",
    "complete_operation_ledger_unavailable": "The current project schema has no complete replayable operation ledger.",
    "explicit_project_head_unavailable": "The current project schema has no authoritative project head.",
    "legacy_candidate_ref_conflict": "Legacy candidate identifier representations conflict; only allowlisted evidence was retained.",
    "legacy_candidate_ref_omitted": "An untyped legacy candidate reference was omitted.",
    "legacy_candidate_evidence_truncated": "Legacy candidate evidence exceeded the bounded projection.",
    "legacy_creator_context_unverified": "The legacy Creator Memory reference could not be verified as confirmed context.",
    "legacy_candidate_refs_truncated": "Legacy candidate references exceeded the bounded projection.",
    "legacy_list_truncated": "A legacy list exceeded the bounded projection.",
    "legacy_missing_evidence_truncated": "Legacy missing-evidence entries exceeded the bounded projection.",
    "legacy_private_locator_redacted": "A private locator in legacy display text was redacted.",
    "legacy_text_over_limit": "Oversized legacy text was omitted instead of being silently rewritten.",
    "material_block_relation_unavailable": "Material hints are not related to specific script blocks.",
    "observed_timeline_unavailable": "No fully verified latest observed Timeline baseline is available.",
    "observed_timeline_brief_provenance_mismatch": "The Timeline payload and persisted row do not bind the same brief revision.",
    "reference_set_unavailable": "The legacy brief does not preserve a typed reference set.",
    "schema_unavailable": "The bundled Blueprint schema failed its package integrity check.",
    "script_unavailable": "The legacy brief does not preserve script blocks.",
    "stance_unavailable": "The legacy brief does not preserve the creator stance.",
    "technique_set_unavailable": "The legacy brief does not preserve Technique Card selections.",
    "user_decision_authority_unverified": "Declared sources cannot prove that a user confirmed a decision.",
    "wiki_generation_unpinned": "The current live Media Wiki has no project-pinned generation.",
}

_OPAQUE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_ISSUE_LIMIT = 64
_EXPORT_AUTHORITY_MARKERS = (
    "database_meta",
    "schema_migrations",
    "output_roots",
    "canonical_export_operations",
    "canonical_export_jobs",
    "canonical_export_revisions",
    "canonical_usage_occurrences",
    "canonical_export_receipts",
)

_SHADOW_GAPS = {
    "blueprint_shadow_non_authoritative",
    "user_decision_authority_unverified",
    "script_unavailable",
    "stance_unavailable",
    "reference_set_unavailable",
    "technique_set_unavailable",
    "material_block_relation_unavailable",
    "wiki_generation_unpinned",
    "explicit_project_head_unavailable",
    "complete_operation_ledger_unavailable",
}

_NON_READINESS_GAPS = {
    "authoritative_decision_ledger_unavailable",
    "candidate_non_authoritative",
    "candidate_not_persisted",
    "explicit_project_head_unavailable",
    "complete_operation_ledger_unavailable",
    "user_decision_authority_unverified",
    "wiki_generation_unpinned",
}


def _gap_objects(codes: list[str]) -> list[dict[str, str]]:
    return [
        {
            "code": code,
            "message": _GAP_MESSAGES.get(
                code, "A bounded Blueprint capability or source is unavailable."
            ),
        }
        for code in stable_codes(codes)
    ]


def _binding_result(
    status: str,
    issues: list[str],
    *,
    checked: bool,
    capability_unavailable: bool = False,
) -> dict[str, Any]:
    return {
        "status": status,
        "checked_in_single_snapshot": checked,
        "issues": stable_codes(issues),
        "_capability_unavailable": capability_unavailable,
    }


def _reference_result(
    *,
    total: int,
    verified: int,
    issues: list[dict[str, str]],
    checked: bool,
    capability_unavailable: bool = False,
    executable_material_authority_unavailable: bool = False,
) -> dict[str, Any]:
    unresolved = max(0, total - verified)
    if total == 0:
        status = "not_applicable"
    elif unresolved == 0:
        status = "verified"
    elif verified:
        status = "partial"
    else:
        status = "unresolved"
    ordered_issues = sorted(issues, key=lambda item: (item["path"], item["code"]))
    return {
        "status": status,
        "checked_in_single_snapshot": checked,
        "identity_and_current_availability_only": True,
        "total_count": total,
        "verified_count": verified,
        "unresolved_count": unresolved,
        "issues": ordered_issues[:_REFERENCE_ISSUE_LIMIT],
        "issues_truncated": len(ordered_issues) > _REFERENCE_ISSUE_LIMIT,
        "_issue_codes": stable_codes(item["code"] for item in ordered_issues),
        "_required_evidence_unresolved": any(
            item["path"].startswith("/constraints/must_include/")
            for item in ordered_issues
        ),
        "_derivative_material_rejected": any(
            item["code"] == "canonical_derivative_executable_material_forbidden"
            for item in ordered_issues
        ),
        "_residual_material_stale": any(
            item["code"] == "blueprint_residual_evidence_stale"
            for item in ordered_issues
        ),
        "_executable_material_authority_unavailable": (
            executable_material_authority_unavailable
        ),
        "_capability_unavailable": capability_unavailable,
    }


def _reference_targets(
    candidate: dict[str, Any],
) -> list[tuple[str, str, Any]]:
    """Enumerate bounded declared references without following any locator."""

    result: list[tuple[str, str, Any]] = []
    constraints = candidate.get("constraints")
    if isinstance(constraints, dict):
        for group in ("must_include", "must_exclude"):
            items = constraints.get(group)
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if (
                    isinstance(item, dict)
                    and item.get("evidence_ref") is not None
                ):
                    result.append(
                        (
                            f"/constraints/{group}/{index}/evidence_ref",
                            "evidence",
                            item.get("evidence_ref"),
                        )
                    )
    materials = candidate.get("material_hints")
    if isinstance(materials, list):
        for index, item in enumerate(materials):
            result.append(
                (
                    f"/material_hints/{index}/evidence_ref",
                    "evidence" if isinstance(item, dict) else "invalid",
                    item.get("evidence_ref") if isinstance(item, dict) else None,
                )
            )
    references = candidate.get("reference_refs")
    if isinstance(references, list):
        for index, item in enumerate(references):
            result.append(
                (
                    f"/reference_refs/{index}/locator",
                    str(item.get("kind")) if isinstance(item, dict) else "invalid",
                    item.get("locator") if isinstance(item, dict) else None,
                )
            )
    techniques = candidate.get("technique_refs")
    if isinstance(techniques, list):
        for index, item in enumerate(techniques):
            result.append(
                (f"/technique_refs/{index}/card_id", "technique", item)
            )
    bindings = candidate.get("bindings")
    if isinstance(bindings, dict):
        if bindings.get("creator_context") is not None:
            result.append(
                ("/bindings/creator_context", "creator", bindings["creator_context"])
            )
        if bindings.get("wiki_generation") is not None:
            result.append(
                ("/bindings/wiki_generation", "wiki", bindings["wiki_generation"])
            )
    return result


def _valid_revision(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 1_000_000


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_brief_reference(value: Any) -> bool:
    return isinstance(value, dict) and _valid_revision(
        value.get("revision")
    ) and _valid_digest(value.get("content_sha256"))


def _valid_timeline_reference(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("timeline_id"), str)
        and _OPAQUE_IDENTIFIER.fullmatch(value["timeline_id"]) is not None
        and _valid_revision(value.get("revision"))
        and _valid_digest(value.get("content_sha256"))
    )


def _database_reference_target(kind: str, value: Any) -> bool:
    if kind == "project":
        return bool(
            isinstance(value, str)
            and _OPAQUE_IDENTIFIER.fullmatch(value) is not None
        )
    if kind == "creator":
        return bool(
            isinstance(value, dict)
            and isinstance(value.get("profile_id"), str)
            and _OPAQUE_IDENTIFIER.fullmatch(value["profile_id"]) is not None
            and _valid_revision(value.get("revision"))
            and _valid_digest(value.get("content_sha256"))
        )
    if kind != "evidence" or not isinstance(value, str):
        return False
    try:
        parse_evidence_id(value)
        return True
    except MemoLensError:
        return False


def _timeline_baseline(
    raw: Any,
    *,
    project_id: str,
    expected_brief_revision: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict):
        return None, "observed_timeline_missing"
    timeline_id = raw.get("timeline_id")
    revision = raw.get("revision")
    digest = raw.get("content_sha256")
    if (
        not isinstance(timeline_id, str)
        or type(revision) is not int
        or not 1 <= revision <= 1_000_000
        or not isinstance(digest, str)
    ):
        return None, "observed_timeline_identity_invalid"
    if raw.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        return None, "observed_timeline_schema_unsupported"
    parsed, _computed, integrity = strict_persisted_object(
        raw.get("timeline_json"), digest
    )
    if integrity != "verified" or parsed is None:
        return None, f"observed_timeline_{integrity}"
    if not (
        parsed.get("id") == timeline_id
        and parsed.get("project_id") == project_id
        and type(parsed.get("revision")) is int
        and parsed.get("revision") == revision
        and parsed.get("schema_version") == raw.get("schema_version")
    ):
        return None, "observed_timeline_identity_mismatch"
    if not validate_timeline(parsed)["valid"]:
        return None, "observed_timeline_contract_invalid"
    provenance = parsed.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("brief_revision") != expected_brief_revision
    ):
        return None, "observed_timeline_brief_provenance_mismatch"
    if raw.get("validation_status") != "valid":
        return None, "observed_timeline_not_valid"
    if raw.get("brief_binding_exists") is not True:
        return None, "observed_timeline_brief_missing"
    if (
        type(raw.get("brief_revision")) is not int
        or raw.get("brief_revision") != expected_brief_revision
    ):
        return None, "observed_timeline_brief_stale"
    return {
        "timeline_id": timeline_id,
        "revision": revision,
        "content_sha256": digest.casefold(),
    }, None


class CreativeBlueprintService:
    """Coordinate Blueprint reads and validation through one private snapshot."""

    def __init__(
        self,
        database: ReadOnlyDatabase,
        projects: ProjectIndexReader,
        media: MediaIndexReader,
        creator: CreatorMemoryReader,
    ) -> None:
        self.database = database
        self.projects = projects
        self.media = media
        self.creator = creator

    def shadow(self, project_id: Any) -> dict[str, Any]:
        normalized = validate_project_identifier(project_id)
        if not BLUEPRINT_SCHEMA_AVAILABLE:
            return {
                "object": BLUEPRINT_SHADOW_OBJECT,
                "schema_version": "1",
                "status": "capability_unavailable",
                "source": "sqlite_read_only",
                "mode": "safe_default_read_only",
                "projection_mode": "legacy_shadow_v1",
                "contract": blueprint_contract_summary(),
                "project_id": normalized,
                "is_authoritative": False,
                "is_persisted": False,
                "shadow": None,
                "shadow_digest": None,
                "validation": None,
                "authority": self._authority([]),
                "data_trust": "untrusted_data",
                "untrusted_data_fields": [],
                "gaps": _gap_objects(["schema_unavailable"]),
                "safety": blueprint_safety_summary(),
            }
        try:
            with closing(self.database.connection()) as connection:
                raw = self.projects.open_in_connection(connection, normalized)
                return self._shadow_in_connection(connection, raw, normalized)
        except MemoLensError as exc:
            if exc.code in {"invalid_argument", "project_not_found"}:
                raise
            return {
                "object": BLUEPRINT_SHADOW_OBJECT,
                "schema_version": "1",
                "status": "capability_unavailable",
                "source": "sqlite_read_only",
                "mode": "safe_default_read_only",
                "projection_mode": "legacy_shadow_v1",
                "contract": blueprint_contract_summary(),
                "project_id": normalized,
                "is_authoritative": False,
                "is_persisted": False,
                "shadow": None,
                "shadow_digest": None,
                "validation": None,
                "authority": self._authority([]),
                "data_trust": "untrusted_data",
                "untrusted_data_fields": [],
                "gaps": _gap_objects(
                    ["blueprint_shadow_non_authoritative", "database_unavailable"]
                ),
                "safety": blueprint_safety_summary(),
            }

    def _shadow_in_connection(
        self,
        connection: sqlite3.Connection,
        raw: dict[str, Any],
        project_id: str,
    ) -> dict[str, Any]:
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, dict) or not capabilities.get(
            "project_resume_available"
        ):
            return {
                "object": BLUEPRINT_SHADOW_OBJECT,
                "schema_version": "1",
                "status": "capability_unavailable",
                "source": "sqlite_read_only",
                "mode": "safe_default_read_only",
                "projection_mode": "legacy_shadow_v1",
                "contract": blueprint_contract_summary(),
                "project_id": project_id,
                "is_authoritative": False,
                "is_persisted": False,
                "shadow": None,
                "shadow_digest": None,
                "validation": None,
                "authority": self._authority([]),
                "data_trust": "untrusted_data",
                "untrusted_data_fields": [],
                "gaps": _gap_objects(
                    ["blueprint_shadow_non_authoritative", "legacy_brief_unavailable"]
                ),
                "safety": blueprint_safety_summary(),
            }
        brief_row = raw.get("brief")
        if not isinstance(brief_row, dict):
            return self._empty_shadow(project_id, "legacy_brief_unavailable")
        revision = brief_row.get("revision")
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            return self._empty_shadow(project_id, "legacy_brief_revision_invalid")
        brief, _computed, integrity = strict_persisted_object(
            brief_row.get("brief_json"), brief_row.get("content_sha256")
        )
        if brief is None:
            return self._empty_shadow(project_id, f"legacy_brief_{integrity}")
        digest = str(brief_row["content_sha256"]).casefold()
        timeline, timeline_gap = _timeline_baseline(
            raw.get("timeline_head"),
            project_id=project_id,
            expected_brief_revision=revision,
        )
        projection_gaps = set(_SHADOW_GAPS)
        if timeline_gap is not None:
            projection_gaps.add("observed_timeline_unavailable")
            projection_gaps.add(timeline_gap)
        candidate, legacy_gaps = project_legacy_brief_to_candidate(
            project_id=project_id,
            brief=brief,
            brief_revision=revision,
            brief_content_sha256=digest,
            observed_timeline=timeline,
        )
        projection_gaps.update(legacy_gaps)
        creator_binding = candidate["bindings"]["creator_context"]
        if isinstance(creator_binding, dict) and not self.creator.binding_exists_in_connection(
            connection, creator_binding
        ):
            candidate["bindings"]["creator_context"] = None
            projection_gaps.add("legacy_creator_context_unverified")
        validation = self._validate_candidate(
            candidate,
            connection=connection,
            cached_project=raw,
            extra_gaps=projection_gaps,
            admit_executable_material=False,
        )
        if not validation["schema_valid"]:
            return self._empty_shadow(project_id, "legacy_projection_invalid")
        untrusted = [
            "/intent/goal",
            "/intent/stance",
            "/intent/audience",
            "/intent/platform",
            "/script/blocks/*/text",
            "/direction/*",
            "/constraints/*/*/text",
            "/assumptions/*/text",
            "/missing_evidence/*/description",
        ]
        return {
            "object": BLUEPRINT_SHADOW_OBJECT,
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "projection_mode": "legacy_shadow_v1",
            "contract": blueprint_contract_summary(),
            "project_id": project_id,
            "is_authoritative": False,
            "is_persisted": False,
            "shadow": candidate,
            "shadow_digest": canonical_sha256(candidate),
            "validation": validation,
            "authority": self._authority(["legacy_unknown"]),
            "data_trust": "untrusted_data",
            "untrusted_data_fields": untrusted,
            "gaps": _gap_objects(list(projection_gaps)),
            "safety": blueprint_safety_summary(),
        }

    def _empty_shadow(self, project_id: str, gap: str) -> dict[str, Any]:
        return {
            "object": BLUEPRINT_SHADOW_OBJECT,
            "schema_version": "1",
            "status": "incomplete",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "projection_mode": "legacy_shadow_v1",
            "contract": blueprint_contract_summary(),
            "project_id": project_id,
            "is_authoritative": False,
            "is_persisted": False,
            "shadow": None,
            "shadow_digest": None,
            "validation": None,
            "authority": self._authority([]),
            "data_trust": "untrusted_data",
            "untrusted_data_fields": [],
            "gaps": _gap_objects([*_SHADOW_GAPS, gap]),
            "safety": blueprint_safety_summary(),
        }

    def validate(self, candidate: Any) -> dict[str, Any]:
        inspection = inspect_blueprint_candidate(candidate)
        if type(candidate) is not dict or not inspection["_structurally_safe"]:
            return self._validation_response(
                {},
                inspection,
                base_binding=_binding_result(
                    "not_applicable", [], checked=False
                ),
                reference_resolution=_reference_result(
                    total=0, verified=0, issues=[], checked=False
                ),
                extra_gaps=set(),
            )
        if not self._needs_snapshot(candidate):
            return self._validate_candidate(
                candidate, connection=None, inspection=inspection
            )
        try:
            with closing(self.database.connection()) as connection:
                return self._validate_candidate(
                    candidate, connection=connection, inspection=inspection
                )
        except MemoLensError as exc:
            if exc.code in {
                "derivative_authority_integrity_error",
                "evidence_identity_integrity_error",
                "residual_authority_integrity_error",
                "usage_authority_integrity_error",
            }:
                raise
            return self._validation_response(
                candidate,
                inspection,
                base_binding=self._base_binding(
                    None, candidate, cached_project=None
                ),
                reference_resolution=self._resolve_references(
                    None,
                    candidate,
                    admit_executable_material=bool(inspection["schema_valid"]),
                ),
                extra_gaps={"database_unavailable"},
            )

    @staticmethod
    def _needs_snapshot(candidate: dict[str, Any]) -> bool:
        project_id = candidate.get("project_id")
        if isinstance(project_id, str) and _OPAQUE_IDENTIFIER.fullmatch(project_id):
            return True
        return any(
            _database_reference_target(kind, value)
            for _path, kind, value in _reference_targets(candidate)
        )

    def _validate_candidate(
        self,
        candidate: dict[str, Any],
        *,
        connection: sqlite3.Connection | None,
        cached_project: dict[str, Any] | None = None,
        extra_gaps: set[str] | None = None,
        inspection: dict[str, Any] | None = None,
        admit_executable_material: bool = True,
    ) -> dict[str, Any]:
        inspection = inspection or inspect_blueprint_candidate(candidate)
        base = self._base_binding(
            connection, candidate, cached_project=cached_project
        )
        references = self._resolve_references(
            connection,
            candidate,
            admit_executable_material=(
                admit_executable_material and bool(inspection["schema_valid"])
            ),
        )
        return self._validation_response(
            candidate,
            inspection,
            base_binding=base,
            reference_resolution=references,
            extra_gaps=extra_gaps or set(),
        )

    def _exact_brief_binding_issues(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        latest_revision: int,
        candidate_brief: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str], bool]:
        conflict: list[str] = []
        stale: list[str] = []
        missing: list[str] = []
        if not _valid_brief_reference(candidate_brief):
            conflict.append("legacy_brief_exact_ref_invalid")
            return conflict, stale, missing, False

        revision = candidate_brief["revision"]
        exact = self.projects.brief_revision_in_connection(
            connection, project_id=project_id, revision=revision
        )
        if exact is None:
            missing.append("legacy_brief_exact_ref_missing")
            return conflict, stale, missing, True
        if exact.get("project_id") != project_id or exact.get("revision") != revision:
            conflict.append("legacy_brief_exact_ref_identity_invalid")
            return conflict, stale, missing, True

        parsed, _computed, integrity = strict_persisted_object(
            exact.get("brief_json"), exact.get("content_sha256")
        )
        if parsed is None:
            conflict.append(f"legacy_brief_exact_ref_{integrity}")
            return conflict, stale, missing, True
        if candidate_brief["content_sha256"] != str(
            exact["content_sha256"]
        ).casefold():
            conflict.append("legacy_brief_exact_ref_digest_conflict")
        if revision != latest_revision:
            stale.append("legacy_brief_baseline_stale")
        return conflict, stale, missing, True

    def _exact_timeline_binding_issues(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        capabilities: dict[str, Any],
        candidate_timeline: Any,
        candidate_brief: dict[str, Any],
        latest_timeline: dict[str, Any] | None,
    ) -> tuple[list[str], list[str], list[str], bool]:
        conflict: list[str] = []
        stale: list[str] = []
        missing: list[str] = []
        if candidate_timeline is None:
            if latest_timeline is not None:
                missing.append("observed_timeline_baseline_missing")
            return conflict, stale, missing, False
        if not _valid_timeline_reference(candidate_timeline):
            conflict.append("observed_timeline_exact_ref_invalid")
            return conflict, stale, missing, False
        if not capabilities.get("timeline_schema_available"):
            missing.append("observed_timeline_capability_unavailable")
            return conflict, stale, missing, True

        exact_raw = self.projects.timeline_revision_in_connection(
            connection,
            project_id=project_id,
            timeline_id=candidate_timeline["timeline_id"],
            revision=candidate_timeline["revision"],
        )
        if exact_raw is None:
            missing.append("observed_timeline_exact_ref_missing")
            return conflict, stale, missing, False
        if not _valid_brief_reference(candidate_brief):
            conflict.append("observed_timeline_brief_binding_uncheckable")
            return conflict, stale, missing, False

        exact, issue = _timeline_baseline(
            exact_raw,
            project_id=project_id,
            expected_brief_revision=candidate_brief["revision"],
        )
        if exact is None:
            conflict.append(issue or "observed_timeline_exact_ref_invalid")
            return conflict, stale, missing, False
        if candidate_timeline["content_sha256"] != exact["content_sha256"]:
            conflict.append("observed_timeline_exact_ref_digest_conflict")
        if (
            latest_timeline is None
            or candidate_timeline["timeline_id"] != latest_timeline["timeline_id"]
            or candidate_timeline["revision"] != latest_timeline["revision"]
        ):
            stale.append("observed_timeline_baseline_stale")
        return conflict, stale, missing, False

    def _base_binding(
        self,
        connection: sqlite3.Connection | None,
        candidate: dict[str, Any],
        *,
        cached_project: dict[str, Any] | None,
    ) -> dict[str, Any]:
        project_id = candidate.get("project_id")
        if project_id is None:
            if candidate.get("base") is not None:
                return _binding_result(
                    "conflict", ["baseline_project_missing"], checked=False
                )
            return _binding_result(
                "not_applicable", [], checked=connection is not None
            )
        if not isinstance(project_id, str) or _OPAQUE_IDENTIFIER.fullmatch(project_id) is None:
            return _binding_result(
                "conflict", ["baseline_project_identity_invalid"], checked=False
            )
        if connection is None:
            return _binding_result(
                "missing",
                ["baseline_unchecked"],
                checked=False,
                capability_unavailable=True,
            )
        base = candidate.get("base")
        if not isinstance(base, dict) or not isinstance(base.get("legacy_brief"), dict):
            return _binding_result(
                "missing", ["legacy_brief_baseline_missing"], checked=True
            )
        try:
            raw = cached_project or self.projects.open_in_connection(
                connection, project_id
            )
        except MemoLensError as exc:
            capability_unavailable = exc.code != "project_not_found"
            return _binding_result(
                "missing",
                [f"baseline_{exc.code}"],
                checked=True,
                capability_unavailable=capability_unavailable,
            )
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, dict) or not capabilities.get(
            "project_resume_available"
        ):
            return _binding_result(
                "missing",
                ["legacy_brief_capability_unavailable"],
                checked=True,
                capability_unavailable=True,
            )
        brief_row = raw.get("brief")
        if not isinstance(brief_row, dict):
            return _binding_result(
                "missing", ["latest_legacy_brief_missing"], checked=True
            )
        latest_revision = brief_row.get("revision")
        if (
            brief_row.get("project_id") != project_id
            or not _valid_revision(latest_revision)
        ):
            return _binding_result(
                "conflict", ["latest_legacy_brief_identity_invalid"], checked=True
            )
        parsed, _computed, integrity = strict_persisted_object(
            brief_row.get("brief_json"), brief_row.get("content_sha256")
        )
        if parsed is None:
            return _binding_result(
                "conflict", [f"latest_legacy_brief_{integrity}"], checked=True
            )
        candidate_brief = base["legacy_brief"]
        timeline_raw = raw.get("timeline_head")
        candidate_timeline = base.get("observed_timeline")
        latest_timeline = None
        conflict_issues: list[str] = []
        stale_issues: list[str] = []
        missing_issues: list[str] = []
        capability_unavailable = False

        # The current persisted head is validated first. A corrupt head is a
        # conflict even when the caller also presents an older or missing ref.
        if timeline_raw is not None:
            latest_timeline, issue = _timeline_baseline(
                timeline_raw,
                project_id=project_id,
                expected_brief_revision=latest_revision,
            )
            if latest_timeline is None:
                conflict_issues.append(issue or "observed_timeline_invalid")

        brief_conflict, brief_stale, brief_missing, _brief_exact = (
            self._exact_brief_binding_issues(
                connection,
                project_id=project_id,
                latest_revision=latest_revision,
                candidate_brief=candidate_brief,
            )
        )
        conflict_issues.extend(brief_conflict)
        stale_issues.extend(brief_stale)
        missing_issues.extend(brief_missing)
        timeline_conflict, timeline_stale, timeline_missing, timeline_capability = (
            self._exact_timeline_binding_issues(
                connection,
                project_id=project_id,
                capabilities=capabilities,
                candidate_timeline=candidate_timeline,
                candidate_brief=candidate_brief,
                latest_timeline=latest_timeline,
            )
        )
        conflict_issues.extend(timeline_conflict)
        stale_issues.extend(timeline_stale)
        missing_issues.extend(timeline_missing)
        capability_unavailable = capability_unavailable or timeline_capability

        if conflict_issues:
            return _binding_result(
                "conflict",
                conflict_issues,
                checked=True,
                capability_unavailable=capability_unavailable,
            )
        if stale_issues:
            return _binding_result(
                "stale",
                stale_issues,
                checked=True,
                capability_unavailable=capability_unavailable,
            )
        if missing_issues:
            return _binding_result(
                "missing",
                missing_issues,
                checked=True,
                capability_unavailable=capability_unavailable,
            )
        return _binding_result(
            "verified",
            [],
            checked=True,
            capability_unavailable=capability_unavailable,
        )

    def _resolve_references(
        self,
        connection: sqlite3.Connection | None,
        candidate: dict[str, Any],
        *,
        admit_executable_material: bool = False,
    ) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        verified = 0
        targets = _reference_targets(candidate)
        executable_paths = {
            path
            for path, kind, _value in targets
            if kind == "evidence" and path.startswith("/material_hints/")
        }
        derivative_sha256s: frozenset[str] | None = None
        usage_occurrences: list[dict[str, object]] = []
        usage_revision: str | None = None
        raw_materials = candidate.get("material_hints")
        raw_materials = raw_materials if isinstance(raw_materials, list) else []
        material_proofs = {
            f"/material_hints/{index}/evidence_ref": item.get("proof")
            for index, item in enumerate(raw_materials)
            if isinstance(item, dict)
        }
        residual_paths = {
            path
            for path, proof in material_proofs.items()
            if proof is not None
        }
        executable_authority_unavailable = bool(
            admit_executable_material and executable_paths and connection is None
        )
        if admit_executable_material and executable_paths and connection is not None:
            self.media.validate_no_persisted_residual_rows(connection)
            authority_present = any(
                self.database.relation_exists(connection, relation)
                for relation in _EXPORT_AUTHORITY_MARKERS
            )
            if not authority_present:
                executable_authority_unavailable = True
            else:
                try:
                    validate_exact_managed_schema_manifest(connection)
                    derivative_facts = project_validated_derivative_facts(connection)
                except MemoLensError as exc:
                    if exc.code != "derivative_authority_unavailable":
                        raise
                    executable_authority_unavailable = True
                else:
                    derivative_sha256s = derivative_facts["video_sha256"]
                    if not isinstance(derivative_sha256s, frozenset):
                        raise MemoLensError(
                            "Canonical Export derivative facts are invalid.",
                            code="derivative_authority_integrity_error",
                        )
                    if residual_paths:
                        raw_occurrences = derivative_facts.get("usage_occurrences")
                        if not isinstance(raw_occurrences, list) or any(
                            not isinstance(item, dict) for item in raw_occurrences
                        ):
                            raise MemoLensError(
                                "Canonical Usage facts are invalid.",
                                code="usage_authority_integrity_error",
                            )
                        try:
                            usage_occurrences, usage_revision = (
                                current_candidate_usage_facts(
                                    connection,
                                    raw_occurrences,
                                )
                            )
                        except ResidualAuthorityError as exc:
                            raise MemoLensError(str(exc), code=exc.code) from exc
        database_resolution_required = any(
            _database_reference_target(kind, value)
            for _path, kind, value in targets
        )
        for path, kind, value in targets:
            code = "reference_declaration_invalid"
            if kind == "evidence":
                if path in residual_paths:
                    if (
                        isinstance(value, str)
                        and connection is not None
                        and derivative_sha256s is not None
                        and usage_revision is not None
                    ):
                        try:
                            residual_proof = revalidate_residual_proof_in_connection(
                                connection,
                                evidence_ref=value,
                                presented_proof=material_proofs.get(path),
                                occurrences=usage_occurrences,
                                usage_revision=usage_revision,
                                derivative_sha256s=derivative_sha256s,
                            )
                        except ResidualAuthorityError as exc:
                            raise MemoLensError(str(exc), code=exc.code) from exc
                        if residual_proof is not None:
                            verified += 1
                            continue
                        code = "blueprint_residual_evidence_stale"
                    else:
                        code = "executable_material_authority_unavailable"
                elif (
                    isinstance(value, str)
                    and connection is not None
                    and self._evidence_available(connection, value)
                ):
                    if path in executable_paths and derivative_sha256s is not None:
                        proof = self._evidence_proof(connection, value)
                        if proof is None:
                            code = "evidence_identity_unresolved"
                        elif proof["asset_sha256"] in derivative_sha256s:
                            code = (
                                "canonical_derivative_executable_material_forbidden"
                            )
                        else:
                            verified += 1
                            continue
                    else:
                        verified += 1
                        continue
                else:
                    code = "evidence_unresolved"
            elif kind == "user_text":
                if valid_user_text_reference(value):
                    verified += 1
                    continue
                code = "inline_reference_unsafe"
            elif kind == "project":
                if (
                    isinstance(value, str)
                    and _OPAQUE_IDENTIFIER.fullmatch(value)
                    and connection is not None
                    and self._project_exists(connection, value)
                ):
                    verified += 1
                    continue
                code = "project_reference_unresolved"
            elif kind == "external_https":
                code = "external_reference_unverified"
            elif kind == "research_snapshot":
                code = "research_snapshot_unavailable"
            elif kind == "technique":
                code = "technique_card_resolution_unavailable"
            elif kind == "creator":
                if (
                    isinstance(value, dict)
                    and connection is not None
                    and self.creator.binding_exists_in_connection(connection, value)
                ):
                    verified += 1
                    continue
                code = "creator_context_unresolved"
            elif kind == "wiki":
                code = "wiki_generation_resolution_unavailable"
            issues.append({"path": path, "code": code})
        return _reference_result(
            total=len(targets),
            verified=verified,
            issues=issues,
            checked=connection is not None,
            capability_unavailable=(
                (connection is None and database_resolution_required)
                or executable_authority_unavailable
            ),
            executable_material_authority_unavailable=(
                executable_authority_unavailable
            ),
        )

    def _evidence_available(
        self, connection: sqlite3.Connection, evidence_id: str
    ) -> bool:
        try:
            reference = parse_evidence_id(evidence_id)
        except MemoLensError:
            return False
        return self.media.evidence_available_in_connection(
            connection,
            kind=reference.kind,
            identifier=reference.identifier,
        )

    def _evidence_proof(
        self, connection: sqlite3.Connection, evidence_id: str
    ) -> dict[str, str] | None:
        try:
            reference = parse_evidence_id(evidence_id)
        except MemoLensError:
            return None
        return self.media.evidence_proof_in_connection(
            connection,
            kind=reference.kind,
            identifier=reference.identifier,
        )

    def _project_exists(
        self, connection: sqlite3.Connection, project_id: Any
    ) -> bool:
        if not isinstance(project_id, str):
            return False
        return self.projects.exists_in_connection(connection, project_id)

    @staticmethod
    def _authority(sources: list[str]) -> dict[str, Any]:
        return {
            "declared_sources": sorted(set(sources)),
            "authority_verified": False,
            "user_confirmed": False,
            "decision_ledger_available": False,
            "candidate_can_self_assert_confirmation": False,
        }

    def _validation_response(
        self,
        candidate: dict[str, Any],
        inspection: dict[str, Any],
        *,
        base_binding: dict[str, Any],
        reference_resolution: dict[str, Any],
        extra_gaps: set[str],
    ) -> dict[str, Any]:
        schema_valid = bool(inspection["schema_valid"])
        blockers: set[str] = set()
        gaps = {
            "candidate_non_authoritative",
            "candidate_not_persisted",
            "user_decision_authority_unverified",
            "authoritative_decision_ledger_unavailable",
        }
        gaps.update(extra_gaps)
        if not schema_valid:
            blockers.add("candidate_schema_invalid")
            blockers.update(error["code"] for error in inspection["errors"])
        else:
            if candidate.get("project_id") is not None and base_binding["status"] != "verified":
                blockers.add(f"base_binding_{base_binding['status']}")
            if not has_starting_input(candidate):
                blockers.add("starting_input_missing")
                gaps.add("starting_input_missing")
            intent = candidate.get("intent")
            if isinstance(intent, dict) and intent.get("stance") is None:
                gaps.add("stance_unavailable")
            script = candidate.get("script")
            blocks = script.get("blocks") if isinstance(script, dict) else []
            if not blocks:
                gaps.add("script_unavailable")
            materials = candidate.get("material_hints")
            if (
                isinstance(materials, list)
                and materials
                and any(
                    isinstance(item, dict) and not item.get("script_block_ids")
                    for item in materials
                )
            ):
                gaps.add("material_block_relation_unavailable")
            bindings = candidate.get("bindings")
            if isinstance(bindings, dict) and bindings.get("wiki_generation") is None:
                gaps.add("wiki_generation_unpinned")
            missing = candidate.get("missing_evidence")
            if isinstance(missing, list):
                for item in missing:
                    if isinstance(item, dict):
                        gaps.add("declared_missing_evidence")
                        if item.get("required_before") == "coverage":
                            blockers.add("missing_evidence_required_before_coverage")
            decisions = candidate.get("open_decisions")
            if isinstance(decisions, list):
                for item in decisions:
                    if isinstance(item, dict):
                        gaps.add("open_decision")
                        if item.get("required_before") == "coverage":
                            blockers.add("decision_required_before_coverage")
            gaps.update(reference_resolution["_issue_codes"])
            if reference_resolution["_required_evidence_unresolved"]:
                blockers.add("required_evidence_unresolved")
            if reference_resolution["_derivative_material_rejected"]:
                blockers.add(
                    "canonical_derivative_executable_material_forbidden"
                )
            if reference_resolution["_residual_material_stale"]:
                blockers.add("blueprint_residual_evidence_stale")
            if reference_resolution["_executable_material_authority_unavailable"]:
                blockers.add("executable_material_authority_unavailable")
            for issue in base_binding["issues"]:
                gaps.add(issue)
        if blockers:
            readiness = "blocked"
        elif gaps - _NON_READINESS_GAPS:
            readiness = "ready_with_gaps"
        else:
            readiness = "ready_for_coverage"
        outline = candidate_outline(candidate) if schema_valid else {
            "script_block_count": 0,
            "must_include_count": 0,
            "must_exclude_count": 0,
            "material_hint_count": 0,
            "reference_count": 0,
            "technique_count": 0,
            "assumption_count": 0,
            "missing_evidence_count": 0,
            "open_decision_count": 0,
        }
        public_reference_resolution = {
            key: value
            for key, value in reference_resolution.items()
            if not key.startswith("_")
        }
        public_base_binding = {
            key: value
            for key, value in base_binding.items()
            if not key.startswith("_")
        }
        capability_unavailable = bool(
            not BLUEPRINT_SCHEMA_AVAILABLE
            or base_binding.get("_capability_unavailable")
            or reference_resolution.get("_capability_unavailable")
        )
        return {
            "object": BLUEPRINT_VALIDATION_OBJECT,
            "schema_version": "1",
            "status": (
                "capability_unavailable"
                if capability_unavailable
                else "completed"
            ),
            "source": "in_memory_validation",
            "mode": "safe_default_read_only",
            "contract": blueprint_contract_summary(),
            "schema_valid": schema_valid,
            "candidate_digest": inspection["candidate_digest"],
            "material_constraint_projection_digest_v1": inspection[
                "material_constraint_projection_digest_v1"
            ],
            "errors": inspection["errors"],
            "errors_truncated": inspection["errors_truncated"],
            "base_binding": public_base_binding,
            "reference_resolution": public_reference_resolution,
            "planning_readiness": {
                "status": readiness,
                "blockers": stable_codes(blockers),
                "gaps": stable_codes(gaps),
            },
            "outline": outline,
            "authority": self._authority(
                declared_sources(candidate) if schema_valid else []
            ),
            "data_trust": "untrusted_data",
            "untrusted_data_fields": [
                "/intent/*",
                "/script/blocks/*/text",
                "/direction/*",
                "/constraints/*/*/text",
                "/reference_refs/*",
                "/assumptions/*/text",
                "/missing_evidence/*/description",
                "/open_decisions/*/question",
            ],
            "safety": blueprint_safety_summary(),
        }


__all__ = ["CreativeBlueprintService"]
