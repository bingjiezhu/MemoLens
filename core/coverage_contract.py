"""Pure deterministic contract for canonical Coverage Plan revisions.

The A1 compiler is intentionally modest: it normalizes the script blocks and
material hints already fixed by a canonical Creative Blueprint.  It does not
perform retrieval, semantic classification, or global optimization.  Keeping
that boundary explicit prevents Timeline lowering from becoming a second,
hidden footage planner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from .blueprint_contract import require_valid_blueprint_document
from .residual_identity_contract import (
    ResidualIdentityContractError,
    require_valid_residual_binding,
)


COVERAGE_PLAN_OBJECT = "memolens.coverage_plan"
COVERAGE_PLAN_SCHEMA_VERSION = "1"
COVERAGE_BASELINE_COMPILER = "memolens.coverage-baseline/v1"
COVERAGE_TIMING_BASIS = "estimated_text"
COVERAGE_MATCH_TYPE = "depiction_unspecified"
COVERAGE_MAX_CANONICAL_BYTES = 1_048_576
COVERAGE_MAX_BEATS = 256
COVERAGE_MAX_EVIDENCE = 512

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ASSET_ID = re.compile(r"^(?:asset|img)_[0-9a-f]{24}$")
_OPAQUE_VIDEO_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_OPAQUE_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_OPAQUE_SPAN_ID = re.compile(
    r"^seg_(?P<asset_digest>[0-9a-f]{24})_"
    r"(?P<analysis_revision>[1-9][0-9]{0,6})_"
    r"(?P<ordinal>0|[1-9][0-9]{0,6})$"
)
_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/(?:asset/(?:asset|img)_[0-9a-f]{24}|"
    r"span/(?:seg_[0-9a-f]{24}_[1-9][0-9]{0,6}_(?:0|[1-9][0-9]{0,6})|"
    r"rseg_[0-9a-f]{64}))$"
)
_RESIDUAL_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<residual_id>rseg_[0-9a-f]{64})$"
)

_LEGACY_IMAGE_PROOF_FIELDS = {"kind", "asset_id", "asset_sha256"}
_CURRENT_IMAGE_PROOF_FIELDS = _LEGACY_IMAGE_PROOF_FIELDS | {
    "analysis_run_id",
    "analysis_revision",
    "analysis_content_sha256",
    "source_binding_sha256",
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoverageContractError(ValueError):
    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]):
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_coverage_plan"),
                "path": str(item.get("path") or ""),
                "message": str(item.get("message") or "Coverage Plan is invalid."),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"] if normalized else "Coverage Plan is invalid.",
        )


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) < 64:
            self.items.append({"code": code, "path": path, "message": message})


def _closed(
    value: object,
    *,
    path: str,
    fields: set[str],
    issues: _Issues,
) -> dict[str, Any] | None:
    if type(value) is not dict:
        issues.add("schema_type", path, "Value must be an object.")
        return None
    row = value
    missing = fields - set(row)
    unknown = set(row) - fields
    for field in sorted(missing):
        issues.add("required_field_missing", f"{path}/{field}", "A required field is missing.")
    if unknown:
        issues.add("unknown_field", f"{path}/*", "Object contains an unsupported field.")
    return row


def _identifier(value: object, path: str, issues: _Issues) -> bool:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        issues.add("invalid_identifier", path, "Identifier is invalid.")
        return False
    return True


def _sha(value: object, path: str, issues: _Issues) -> bool:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        issues.add("invalid_sha256", path, "SHA-256 digest is invalid.")
        return False
    return True


def _positive_int(value: object, path: str, issues: _Issues, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    if type(value) is not int or value < 1 or value > 1_800_000:
        issues.add("invalid_positive_integer", path, "Value must be a bounded positive integer.")
        return False
    return True


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _content_address_matches(asset_id: str, asset_sha256: str) -> bool:
    namespace = asset_id.split("_", 1)[0]
    return asset_id == f"{namespace}_{asset_sha256[:24]}"


def _estimated_durations(texts: Sequence[str], target_ms: int) -> list[int]:
    """Allocate the exact target deterministically using visible text weight."""

    if not texts:
        return []
    weights = [max(1, len(text.strip())) for text in texts]
    total_weight = sum(weights)
    durations: list[int] = []
    allocated = 0
    for index, weight in enumerate(weights):
        remaining_items = len(weights) - index
        remaining_time = target_ms - allocated
        if remaining_items == 1:
            duration = remaining_time
        else:
            proportional = (target_ms * weight) // total_weight
            duration = max(1, min(proportional, remaining_time - (remaining_items - 1)))
        durations.append(duration)
        allocated += duration
    return durations


def _normalize_proof(
    value: object,
    *,
    evidence_ref: str,
    allow_legacy_image: bool = False,
) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    kind = value.get("kind")
    image_fields = set(value)
    is_legacy_image = kind == "asset" and image_fields == _LEGACY_IMAGE_PROOF_FIELDS
    is_current_image = kind == "asset" and image_fields == _CURRENT_IMAGE_PROOF_FIELDS
    if is_current_image or (allow_legacy_image and is_legacy_image):
        expected_asset_id = (
            evidence_ref.removeprefix("memolens://evidence/asset/")
            if evidence_ref.startswith("memolens://evidence/asset/")
            else None
        )
        image_identity_valid = (
            expected_asset_id is not None
            and type(value.get("asset_id")) is str
            and _OPAQUE_ASSET_ID.fullmatch(value["asset_id"]) is not None
            and value["asset_id"] == expected_asset_id
            and type(value.get("asset_sha256")) is str
            and _SHA256.fullmatch(value["asset_sha256"]) is not None
            and _content_address_matches(value["asset_id"], value["asset_sha256"])
        )
        image_analysis_valid = is_legacy_image or (
            type(value.get("analysis_run_id")) is str
            and _OPAQUE_ANALYSIS_RUN_ID.fullmatch(value["analysis_run_id"]) is not None
            and type(value.get("analysis_revision")) is int
            and 1 <= value["analysis_revision"] <= 1_800_000
            and type(value.get("analysis_content_sha256")) is str
            and _SHA256.fullmatch(value["analysis_content_sha256"]) is not None
            and type(value.get("source_binding_sha256")) is str
            and _SHA256.fullmatch(value["source_binding_sha256"]) is not None
        )
        if image_identity_valid and image_analysis_valid:
            return deepcopy(value)
        return None
    residual_match = _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
    if (
        kind == "residual_span"
        and set(value) == {"kind", "residual_binding"}
        and residual_match is not None
    ):
        try:
            binding = require_valid_residual_binding(
                value.get("residual_binding")  # type: ignore[arg-type]
            )
        except (ResidualIdentityContractError, TypeError):
            return None
        if binding["residual_id"] != residual_match.group("residual_id"):
            return None
        return {
            "kind": "residual_span",
            "residual_binding": binding,
        }
    span_fields = {
        "kind",
        "span_id",
        "asset_id",
        "asset_sha256",
        "analysis_run_id",
        "analysis_revision",
        "input_asset_sha256",
        "start_ms",
        "end_ms",
    }
    if kind != "span" or set(value) != span_fields:
        return None
    expected_span_id = (
        evidence_ref.removeprefix("memolens://evidence/span/")
        if evidence_ref.startswith("memolens://evidence/span/")
        else None
    )
    span_id = value.get("span_id")
    asset_id = value.get("asset_id")
    analysis_run_id = value.get("analysis_run_id")
    span_match = _OPAQUE_SPAN_ID.fullmatch(span_id) if type(span_id) is str else None
    if (
        span_match is None
        or type(asset_id) is not str
        or _OPAQUE_VIDEO_ASSET_ID.fullmatch(asset_id) is None
        or type(analysis_run_id) is not str
        or _OPAQUE_ANALYSIS_RUN_ID.fullmatch(analysis_run_id) is None
        or expected_span_id is None
        or span_id != expected_span_id
        or span_match.group("asset_digest") != asset_id.removeprefix("asset_")
    ):
        return None
    if not all(
        type(value.get(field)) is str and _SHA256.fullmatch(value[field]) is not None
        for field in ("asset_sha256", "input_asset_sha256")
    ):
        return None
    if (
        type(value.get("analysis_revision")) is not int
        or value["analysis_revision"] < 1
        or value["analysis_revision"] > 1_800_000
        or span_match is None
        or int(span_match.group("analysis_revision")) != value["analysis_revision"]
        or type(value.get("start_ms")) is not int
        or value["start_ms"] < 0
        or type(value.get("end_ms")) is not int
        or value["end_ms"] <= value["start_ms"]
        or value["asset_sha256"] != value["input_asset_sha256"]
        or not _content_address_matches(asset_id, value["asset_sha256"])
    ):
        return None
    return deepcopy(value)


def normalize_evidence_proofs(
    evidence_rows: Sequence[Mapping[str, object]],
    *,
    allow_legacy_image: bool = False,
) -> tuple[dict[str, object], ...]:
    """Normalize Core-resolved evidence without admitting locators or free fields."""

    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    resolver_fields = {"evidence_ref", "proof"}
    canonical_fields = {"evidence_ref", "status", "proof_sha256", "proof"}
    for index, raw in enumerate(evidence_rows):
        path = f"/evidence_manifest/{index}"
        if not isinstance(raw, Mapping):
            raise CoverageContractError(
                [{"code": "invalid_evidence_entry", "path": path, "message": "Evidence entry must be an object."}]
            )
        fields = set(raw)
        if frozenset(fields) not in {
            frozenset(resolver_fields),
            frozenset(canonical_fields),
        }:
            raise CoverageContractError(
                [
                    {
                        "code": "invalid_evidence_entry",
                        "path": path,
                        "message": "Evidence entry fields are not closed.",
                    }
                ]
            )
        evidence_ref = raw.get("evidence_ref")
        if type(evidence_ref) is not str or _EVIDENCE_REF.fullmatch(evidence_ref) is None:
            raise CoverageContractError(
                [
                    {
                        "code": "invalid_evidence_reference",
                        "path": f"{path}/evidence_ref",
                        "message": "Evidence reference must contain an opaque Core media identity.",
                    }
                ]
            )
        if evidence_ref in seen:
            raise CoverageContractError(
                [
                    {
                        "code": "duplicate_evidence_reference",
                        "path": f"{path}/evidence_ref",
                        "message": "Evidence references must be unique.",
                    }
                ]
            )
        seen.add(evidence_ref)
        raw_proof = raw.get("proof")
        proof = (
            None
            if raw_proof is None
            else _normalize_proof(
                raw_proof,
                evidence_ref=evidence_ref,
                allow_legacy_image=allow_legacy_image,
            )
        )
        if raw_proof is not None and proof is None:
            legacy_image = (
                type(raw_proof) is dict
                and raw_proof.get("kind") == "asset"
                and set(raw_proof) == _LEGACY_IMAGE_PROOF_FIELDS
            )
            raise CoverageContractError(
                [
                    {
                        "code": (
                            "legacy_image_evidence_non_current"
                            if legacy_image and not allow_legacy_image
                            else "invalid_evidence_proof"
                        ),
                        "path": f"{path}/proof",
                        "message": (
                            "Identity-only image evidence is historical and cannot be used as current evidence."
                            if legacy_image and not allow_legacy_image
                            else "A non-null evidence proof must satisfy its closed identity contract."
                        ),
                    }
                ]
            )
        status = "verified" if proof is not None else "unresolved"
        normalized_row = {
            "evidence_ref": evidence_ref,
            "status": status,
            "proof_sha256": canonical_sha256(proof) if proof is not None else None,
            "proof": proof,
        }
        if fields == canonical_fields and any(
            raw.get(field) != normalized_row[field] for field in ("status", "proof_sha256")
        ):
            raise CoverageContractError(
                [
                    {
                        "code": "invalid_evidence_attestation",
                        "path": path,
                        "message": "Evidence status and proof digest must match the enclosed proof.",
                    }
                ]
            )
        normalized.append(normalized_row)
    normalized.sort(key=lambda item: str(item["evidence_ref"]))
    if len(normalized) > COVERAGE_MAX_EVIDENCE:
        raise CoverageContractError(
            [
                {
                    "code": "evidence_limit_exceeded",
                    "path": "/evidence_manifest",
                    "message": "Evidence manifest is too large.",
                }
            ]
        )
    return tuple(normalized)


def _proof_interval(proof: object) -> tuple[int, int] | None:
    if type(proof) is not dict:
        return None
    if proof.get("kind") == "span":
        start = proof.get("start_ms")
        end = proof.get("end_ms")
    elif proof.get("kind") == "residual_span":
        binding = proof.get("residual_binding")
        if type(binding) is not dict:
            return None
        start = binding.get("source_in_ms")
        end = binding.get("source_out_ms")
    else:
        return None
    if type(start) is not int or type(end) is not int or end <= start:
        return None
    return start, end


def compile_baseline_coverage_plan(
    *,
    project_id: str,
    revision: int,
    parent: Mapping[str, object] | None,
    blueprint: Mapping[str, object],
    blueprint_binding: Mapping[str, object],
    evidence_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Compile a deterministic, non-semantic baseline from one exact Blueprint."""

    if _IDENTIFIER.fullmatch(project_id) is None:
        raise CoverageContractError(
            [{"code": "invalid_project_id", "path": "/project_id", "message": "Project identity is invalid."}]
        )
    if type(revision) is not int or revision < 1:
        raise CoverageContractError(
            [{"code": "invalid_revision", "path": "/revision", "message": "Revision is invalid."}]
        )
    document = require_valid_blueprint_document(blueprint)
    if document["project_id"] != project_id:
        raise CoverageContractError(
            [
                {
                    "code": "blueprint_project_mismatch",
                    "path": "/blueprint_binding",
                    "message": "Blueprint belongs to a different project.",
                }
            ]
        )
    expected_binding = {
        "revision": document["revision"],
        "content_sha256": canonical_sha256(document),
        "semantic_sha256": canonical_sha256(document["semantic"]),
    }
    if dict(blueprint_binding) != expected_binding:
        raise CoverageContractError(
            [
                {
                    "code": "blueprint_binding_mismatch",
                    "path": "/blueprint_binding",
                    "message": "Blueprint binding is not exact.",
                }
            ]
        )

    manifest = normalize_evidence_proofs(evidence_rows)
    semantic = document["semantic"]
    expected_evidence_refs = tuple(sorted({str(hint["evidence_ref"]) for hint in semantic["material_hints"]}))
    observed_evidence_refs = tuple(str(item["evidence_ref"]) for item in manifest)
    if observed_evidence_refs != expected_evidence_refs:
        raise CoverageContractError(
            [
                {
                    "code": "evidence_manifest_mismatch",
                    "path": "/evidence_manifest",
                    "message": (
                        "Evidence manifest must exactly cover the Creative Blueprint material-hint evidence references."
                    ),
                }
            ]
        )
    proofs = {str(item["evidence_ref"]): item for item in manifest}
    blocks = semantic["script"]["blocks"]
    if not blocks or len(blocks) > COVERAGE_MAX_BEATS:
        raise CoverageContractError(
            [
                {
                    "code": "invalid_script_block_count",
                    "path": "/beats",
                    "message": "Coverage baseline requires one to 256 script blocks.",
                }
            ]
        )
    target_ms = semantic["output"]["duration_target_ms"]
    if target_ms is None:
        target_ms = max(3_000, 3_000 * len(blocks))
    durations = _estimated_durations([str(block["text"]) for block in blocks], target_ms)

    hints_by_block: dict[str, list[Mapping[str, object]]] = {str(block["block_id"]): [] for block in blocks}
    for hint in semantic["material_hints"]:
        for block_id in hint["script_block_ids"]:
            hints_by_block[str(block_id)].append(hint)

    reserved_evidence: set[str] = set()
    beats: list[dict[str, object]] = []
    cursor = 0
    for ordinal, (block, duration_ms) in enumerate(zip(blocks, durations, strict=True)):
        block_id = str(block["block_id"])
        text_digest = canonical_sha256(str(block["text"]))
        beat_id = _stable_id("beat", project_id, block_id, text_digest)
        need_id = _stable_id("need", beat_id, COVERAGE_MATCH_TYPE)
        selected: list[dict[str, object]] = []
        alternatives: list[dict[str, object]] = []
        saw_unresolved = False
        saw_duplicate = False
        saw_short_span = False
        for hint in hints_by_block[block_id]:
            evidence_ref = str(hint["evidence_ref"])
            evidence = proofs.get(evidence_ref)
            verified = evidence is not None and evidence["status"] == "verified"
            assignment_id = _stable_id("assign", beat_id, str(hint["hint_id"]), evidence_ref)
            row = {
                "assignment_id": assignment_id,
                "need_id": need_id,
                "hint_id": str(hint["hint_id"]),
                "evidence_ref": evidence_ref,
                "match_type": COVERAGE_MATCH_TYPE,
                "timeline_duration_ms": duration_ms,
                "reason_sha256": canonical_sha256(hint["reason"]),
                "locked": False,
            }
            if not verified:
                saw_unresolved = True
                alternatives.append({**row, "not_selected_reason": "evidence_unresolved"})
            elif (
                (proof_interval := _proof_interval(evidence.get("proof")))
                is not None
                and proof_interval[1] - proof_interval[0] < duration_ms
            ):
                saw_short_span = True
                alternatives.append({**row, "not_selected_reason": "evidence_span_too_short"})
            elif evidence_ref in reserved_evidence:
                saw_duplicate = True
                alternatives.append({**row, "not_selected_reason": "duplicate_evidence_reserved"})
            elif not selected:
                selected.append(row)
                reserved_evidence.add(evidence_ref)
            else:
                alternatives.append({**row, "not_selected_reason": "baseline_first_verified_selected"})
        gap: dict[str, object] | None = None
        if not selected:
            code = (
                "evidence_unresolved"
                if saw_unresolved
                else "evidence_span_too_short"
                if saw_short_span
                else "duplicate_evidence_reserved"
                if saw_duplicate
                else "no_material_hint"
            )
            gap = {"code": code, "blocking": False}
        beats.append(
            {
                "beat_id": beat_id,
                "script_block_id": block_id,
                "ordinal": ordinal,
                "text_sha256": text_digest,
                "timing": {
                    "basis": COVERAGE_TIMING_BASIS,
                    "start_ms": cursor,
                    "end_ms": cursor + duration_ms,
                },
                "needs": [
                    {
                        "need_id": need_id,
                        "kind": COVERAGE_MATCH_TYPE,
                        "priority": "should",
                        "allow_empty": True,
                    }
                ],
                "selected_assignments": selected,
                "alternatives": alternatives,
                "gap": gap,
            }
        )
        cursor += duration_ms

    parent_copy = None if parent is None else deepcopy(dict(parent))
    result: dict[str, object] = {
        "object": COVERAGE_PLAN_OBJECT,
        "schema_version": COVERAGE_PLAN_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": revision,
        "parent": parent_copy,
        "blueprint_binding": expected_binding,
        "compiler": {
            "id": COVERAGE_BASELINE_COMPILER,
            "timing_capability": COVERAGE_TIMING_BASIS,
            "semantic_matching": False,
            "global_optimization": False,
        },
        "output_binding": {
            "duration_target_ms": target_ms,
            "aspect_ratio": semantic["output"]["aspect_ratio"],
        },
        "beats": beats,
        "evidence_manifest": list(manifest),
    }
    errors = validate_coverage_plan(result)
    if errors:
        raise CoverageContractError(errors)
    return result


def _validate_parent(root: Mapping[str, object], issues: _Issues) -> None:
    parent = root.get("parent")
    revision = root.get("revision")
    if parent is None:
        if revision != 1:
            issues.add("invalid_parent", "/parent", "Only revision one may omit a parent.")
        return
    parent_row = _closed(
        parent,
        path="/parent",
        fields={"revision", "content_sha256"},
        issues=issues,
    )
    if parent_row is None:
        return
    _positive_int(parent_row.get("revision"), "/parent/revision", issues)
    _sha(parent_row.get("content_sha256"), "/parent/content_sha256", issues)
    if type(revision) is int and parent_row.get("revision") != revision - 1:
        issues.add("invalid_parent", "/parent/revision", "Parent must be the preceding revision.")


def _validate_blueprint_binding(root: Mapping[str, object], issues: _Issues) -> None:
    binding = _closed(
        root.get("blueprint_binding"),
        path="/blueprint_binding",
        fields={"revision", "content_sha256", "semantic_sha256"},
        issues=issues,
    )
    if binding is None:
        return
    _positive_int(binding.get("revision"), "/blueprint_binding/revision", issues)
    _sha(binding.get("content_sha256"), "/blueprint_binding/content_sha256", issues)
    _sha(binding.get("semantic_sha256"), "/blueprint_binding/semantic_sha256", issues)


def _validate_compiler(root: Mapping[str, object], issues: _Issues) -> None:
    compiler = _closed(
        root.get("compiler"),
        path="/compiler",
        fields={"id", "timing_capability", "semantic_matching", "global_optimization"},
        issues=issues,
    )
    expected = {
        "id": COVERAGE_BASELINE_COMPILER,
        "timing_capability": COVERAGE_TIMING_BASIS,
        "semantic_matching": False,
        "global_optimization": False,
    }
    if compiler is not None and compiler != expected:
        issues.add("invalid_compiler", "/compiler", "Compiler capability claim is invalid.")


def _validate_output(
    root: Mapping[str, object],
    issues: _Issues,
) -> dict[str, Any] | None:
    output = _closed(
        root.get("output_binding"),
        path="/output_binding",
        fields={"duration_target_ms", "aspect_ratio"},
        issues=issues,
    )
    if output is None:
        return None
    _positive_int(output.get("duration_target_ms"), "/output_binding/duration_target_ms", issues)
    if output.get("aspect_ratio") not in {None, "16:9", "9:16", "1:1", "4:5"}:
        issues.add("invalid_aspect_ratio", "/output_binding/aspect_ratio", "Aspect ratio is unsupported.")
    return output


def _validate_manifest(
    root: Mapping[str, object],
    issues: _Issues,
) -> dict[str, dict[str, object]]:
    raw_manifest = root.get("evidence_manifest")
    manifest: list[dict[str, object]] = []
    if type(raw_manifest) is not list or len(raw_manifest) > COVERAGE_MAX_EVIDENCE:
        issues.add("invalid_evidence_manifest", "/evidence_manifest", "Evidence manifest is invalid.")
    else:
        try:
            manifest = list(
                normalize_evidence_proofs(
                    raw_manifest,
                    allow_legacy_image=True,
                )
            )
            if manifest != raw_manifest:
                issues.add(
                    "noncanonical_evidence_manifest",
                    "/evidence_manifest",
                    "Evidence manifest must be sorted and canonical.",
                )
        except CoverageContractError as exc:
            issues.items.extend(list(exc.errors)[: max(0, 64 - len(issues.items))])
    return {str(item["evidence_ref"]): item for item in manifest}


def _validate_timing(
    beat: Mapping[str, object],
    path: str,
    expected_start: int,
    issues: _Issues,
) -> tuple[int, int | None]:
    timing = _closed(
        beat.get("timing"),
        path=f"{path}/timing",
        fields={"basis", "start_ms", "end_ms"},
        issues=issues,
    )
    if timing is None:
        return expected_start, None
    if timing.get("basis") != COVERAGE_TIMING_BASIS:
        issues.add("invalid_timing_basis", f"{path}/timing/basis", "A1 timing must be estimated text timing.")
    start = timing.get("start_ms")
    end = timing.get("end_ms")
    if type(start) is not int or type(end) is not int or start != expected_start or end <= start:
        issues.add("invalid_timing", f"{path}/timing", "Beat timing must be positive and contiguous.")
        return expected_start, None
    return end, end - start


def _validate_need(
    beat: Mapping[str, object],
    path: str,
    *,
    expected_need_id: str | None,
    issues: _Issues,
) -> set[str]:
    needs = beat.get("needs")
    if type(needs) is not list or len(needs) != 1:
        issues.add("invalid_needs", f"{path}/needs", "A1 requires exactly one baseline Need.")
        return set()
    need = _closed(
        needs[0],
        path=f"{path}/needs/0",
        fields={"need_id", "kind", "priority", "allow_empty"},
        issues=issues,
    )
    if need is None:
        return set()
    need_ids = set()
    if _identifier(need.get("need_id"), f"{path}/needs/0/need_id", issues):
        need_ids.add(str(need["need_id"]))
        if expected_need_id is not None and need["need_id"] != expected_need_id:
            issues.add(
                "unstable_need_id",
                f"{path}/needs/0/need_id",
                "Need identity does not match the deterministic A1 derivation.",
            )
    if (
        need.get("kind") != COVERAGE_MATCH_TYPE
        or need.get("priority") != "should"
        or need.get("allow_empty") is not True
    ):
        issues.add("invalid_baseline_need", f"{path}/needs/0", "A1 Need claim is invalid.")
    return need_ids


def _validate_assignment(
    raw_assignment: object,
    *,
    assignment_path: str,
    selected_state: bool,
    need_ids: set[str],
    assignment_ids: set[str],
    selected_refs: set[str],
    referenced_refs: set[str],
    evidence_by_ref: Mapping[str, Mapping[str, object]],
    expected_beat_id: str | None,
    expected_duration_ms: int | None,
    has_selected_assignment: bool,
    issues: _Issues,
) -> str | None:
    fields = {
        "assignment_id",
        "need_id",
        "hint_id",
        "evidence_ref",
        "match_type",
        "timeline_duration_ms",
        "reason_sha256",
        "locked",
    }
    if not selected_state:
        fields.add("not_selected_reason")
    assignment = _closed(raw_assignment, path=assignment_path, fields=fields, issues=issues)
    if assignment is None:
        return None
    assignment_id = assignment.get("assignment_id")
    if _identifier(assignment_id, f"{assignment_path}/assignment_id", issues):
        if assignment_id in assignment_ids:
            issues.add(
                "duplicate_assignment",
                f"{assignment_path}/assignment_id",
                "Assignment identity must be unique per Beat.",
            )
        assignment_ids.add(str(assignment_id))
    if assignment.get("need_id") not in need_ids:
        issues.add("dangling_need", f"{assignment_path}/need_id", "Assignment references a missing Need.")
    _identifier(assignment.get("hint_id"), f"{assignment_path}/hint_id", issues)
    evidence_ref = assignment.get("evidence_ref")
    evidence_ref_valid = type(evidence_ref) is str and _EVIDENCE_REF.fullmatch(evidence_ref) is not None
    if not evidence_ref_valid:
        issues.add("invalid_evidence_reference", f"{assignment_path}/evidence_ref", "Evidence reference is invalid.")
    else:
        referenced_refs.add(evidence_ref)
    duration_valid = _positive_int(
        assignment.get("timeline_duration_ms"),
        f"{assignment_path}/timeline_duration_ms",
        issues,
    )
    _sha(assignment.get("reason_sha256"), f"{assignment_path}/reason_sha256", issues)
    if (
        expected_beat_id is not None
        and type(assignment.get("hint_id")) is str
        and type(evidence_ref) is str
        and type(assignment_id) is str
        and assignment_id != _stable_id("assign", expected_beat_id, assignment["hint_id"], evidence_ref)
    ):
        issues.add(
            "unstable_assignment_id",
            f"{assignment_path}/assignment_id",
            "Assignment identity does not match the deterministic A1 derivation.",
        )
    if (
        duration_valid
        and expected_duration_ms is not None
        and assignment["timeline_duration_ms"] != expected_duration_ms
    ):
        issues.add(
            "assignment_duration_mismatch",
            f"{assignment_path}/timeline_duration_ms",
            "Assignment duration must cover exactly its Beat slot.",
        )
    if assignment.get("match_type") != COVERAGE_MATCH_TYPE or assignment.get("locked") is not False:
        issues.add("invalid_baseline_assignment", assignment_path, "A1 assignment capability claim is invalid.")
    evidence = evidence_by_ref.get(str(evidence_ref)) if evidence_ref_valid else None
    if evidence_ref_valid and evidence is None:
        issues.add(
            "dangling_evidence_reference",
            f"{assignment_path}/evidence_ref",
            "Assignment references evidence absent from the canonical manifest.",
        )
    proof = evidence.get("proof") if evidence is not None else None
    proof_interval = _proof_interval(proof)
    span_too_short = bool(
        duration_valid
        and proof_interval is not None
        and proof_interval[1] - proof_interval[0]
        < int(assignment["timeline_duration_ms"])
    )
    if selected_state:
        if evidence is None or evidence.get("status") != "verified":
            issues.add(
                "selected_evidence_unverified", f"{assignment_path}/evidence_ref", "Selected evidence must be verified."
            )
        if evidence_ref_valid and evidence_ref in selected_refs:
            issues.add(
                "duplicate_selected_evidence",
                f"{assignment_path}/evidence_ref",
                "Baseline may not select the same evidence twice.",
            )
        if evidence_ref_valid:
            selected_refs.add(evidence_ref)
        if span_too_short:
            issues.add(
                "selected_evidence_too_short",
                f"{assignment_path}/evidence_ref",
                "Selected video span is shorter than the Beat it claims to cover.",
            )
        return None

    claimed_reason = assignment.get("not_selected_reason")
    if claimed_reason not in {
        "evidence_unresolved",
        "duplicate_evidence_reserved",
        "baseline_first_verified_selected",
        "evidence_span_too_short",
    }:
        issues.add(
            "invalid_alternative_reason", f"{assignment_path}/not_selected_reason", "Alternative reason is invalid."
        )
    expected_reason: str | None = None
    if evidence is not None:
        if evidence.get("status") != "verified":
            expected_reason = "evidence_unresolved"
        elif span_too_short:
            expected_reason = "evidence_span_too_short"
        elif evidence_ref in selected_refs:
            expected_reason = "duplicate_evidence_reserved"
        elif has_selected_assignment:
            expected_reason = "baseline_first_verified_selected"
        else:
            issues.add(
                "verified_alternative_should_be_selected",
                f"{assignment_path}/evidence_ref",
                "A viable first verified alternative cannot be left unselected.",
            )
    if expected_reason is not None and claimed_reason != expected_reason:
        issues.add(
            "alternative_reason_mismatch",
            f"{assignment_path}/not_selected_reason",
            "Alternative reason does not match its proof and reservation state.",
        )
    return expected_reason


def _validate_assignments(
    beat: Mapping[str, object],
    *,
    path: str,
    need_ids: set[str],
    selected_refs: set[str],
    referenced_refs: set[str],
    evidence_by_ref: Mapping[str, Mapping[str, object]],
    expected_beat_id: str | None,
    expected_duration_ms: int | None,
    issues: _Issues,
) -> tuple[list[object], list[object], list[str]]:
    selected = beat.get("selected_assignments")
    alternatives = beat.get("alternatives")
    if type(selected) is not list or len(selected) > 1:
        issues.add(
            "invalid_selected_assignments",
            f"{path}/selected_assignments",
            "A1 selects at most one assignment per Beat.",
        )
        selected = []
    if type(alternatives) is not list:
        issues.add("invalid_alternatives", f"{path}/alternatives", "Alternatives must be an array.")
        alternatives = []
    assignment_ids: set[str] = set()
    for index, raw_assignment in enumerate(selected):
        _validate_assignment(
            raw_assignment,
            assignment_path=f"{path}/selected_assignments/{index}",
            selected_state=True,
            need_ids=need_ids,
            assignment_ids=assignment_ids,
            selected_refs=selected_refs,
            referenced_refs=referenced_refs,
            evidence_by_ref=evidence_by_ref,
            expected_beat_id=expected_beat_id,
            expected_duration_ms=expected_duration_ms,
            has_selected_assignment=bool(selected),
            issues=issues,
        )
    expected_alternative_reasons: list[str] = []
    for index, raw_assignment in enumerate(alternatives):
        expected_reason = _validate_assignment(
            raw_assignment,
            assignment_path=f"{path}/alternatives/{index}",
            selected_state=False,
            need_ids=need_ids,
            assignment_ids=assignment_ids,
            selected_refs=selected_refs,
            referenced_refs=referenced_refs,
            evidence_by_ref=evidence_by_ref,
            expected_beat_id=expected_beat_id,
            expected_duration_ms=expected_duration_ms,
            has_selected_assignment=bool(selected),
            issues=issues,
        )
        if expected_reason is not None:
            expected_alternative_reasons.append(expected_reason)
    return selected, alternatives, expected_alternative_reasons


def _validate_gap(
    beat: Mapping[str, object],
    selected: Sequence[object],
    alternatives: Sequence[object],
    expected_alternative_reasons: Sequence[str],
    path: str,
    issues: _Issues,
) -> None:
    gap = beat.get("gap")
    if selected and gap is not None:
        issues.add("contradictory_gap", f"{path}/gap", "Selected Beat cannot also have a Gap.")
    if not selected and gap is None:
        issues.add("missing_gap", f"{path}/gap", "Unselected Beat requires an honest Gap.")
    if gap is None:
        return
    gap_row = _closed(gap, path=f"{path}/gap", fields={"code", "blocking"}, issues=issues)
    allowed_codes = {
        "no_material_hint",
        "evidence_unresolved",
        "duplicate_evidence_reserved",
        "evidence_span_too_short",
    }
    if gap_row is not None and (gap_row.get("code") not in allowed_codes or gap_row.get("blocking") is not False):
        issues.add("invalid_gap", f"{path}/gap", "A1 Gap is invalid.")
        return
    expected_code: str | None = None
    if not selected:
        expected_code = (
            "evidence_unresolved"
            if "evidence_unresolved" in expected_alternative_reasons
            else "evidence_span_too_short"
            if "evidence_span_too_short" in expected_alternative_reasons
            else "duplicate_evidence_reserved"
            if "duplicate_evidence_reserved" in expected_alternative_reasons
            else "no_material_hint"
            if not alternatives
            else None
        )
        if alternatives and expected_code is None:
            issues.add(
                "unjustified_gap",
                f"{path}/gap",
                "Gap cannot be derived from the alternative proof states.",
            )
    if gap_row is not None and expected_code is not None and gap_row.get("code") != expected_code:
        issues.add(
            "gap_reason_mismatch",
            f"{path}/gap/code",
            "Gap code does not match the canonical alternative-state precedence.",
        )


def _validate_beat(
    raw_beat: object,
    *,
    index: int,
    expected_start: int,
    beat_ids: set[str],
    block_ids: set[str],
    selected_refs: set[str],
    referenced_refs: set[str],
    evidence_by_ref: Mapping[str, Mapping[str, object]],
    project_id: str | None,
    issues: _Issues,
) -> int:
    path = f"/beats/{index}"
    beat = _closed(
        raw_beat,
        path=path,
        fields={
            "beat_id",
            "script_block_id",
            "ordinal",
            "text_sha256",
            "timing",
            "needs",
            "selected_assignments",
            "alternatives",
            "gap",
        },
        issues=issues,
    )
    if beat is None:
        return expected_start
    for field, seen, duplicate_code, duplicate_message in (
        ("beat_id", beat_ids, "duplicate_beat", "Beat identity must be unique."),
        (
            "script_block_id",
            block_ids,
            "duplicate_script_block",
            "Script block may appear only once.",
        ),
    ):
        identifier = beat.get(field)
        if _identifier(identifier, f"{path}/{field}", issues):
            if identifier in seen:
                issues.add(duplicate_code, f"{path}/{field}", duplicate_message)
            seen.add(str(identifier))
    if beat.get("ordinal") != index:
        issues.add("invalid_beat_order", f"{path}/ordinal", "Beat ordinal must match canonical order.")
    text_digest_valid = _sha(beat.get("text_sha256"), f"{path}/text_sha256", issues)
    expected_beat_id = None
    if project_id is not None and type(beat.get("script_block_id")) is str and text_digest_valid:
        expected_beat_id = _stable_id("beat", project_id, beat["script_block_id"], beat["text_sha256"])
        if beat.get("beat_id") != expected_beat_id:
            issues.add(
                "unstable_beat_id",
                f"{path}/beat_id",
                "Beat identity does not match the deterministic A1 derivation.",
            )
    next_start, duration_ms = _validate_timing(beat, path, expected_start, issues)
    expected_need_id = (
        _stable_id("need", expected_beat_id, COVERAGE_MATCH_TYPE) if expected_beat_id is not None else None
    )
    need_ids = _validate_need(
        beat,
        path,
        expected_need_id=expected_need_id,
        issues=issues,
    )
    selected, alternatives, expected_alternative_reasons = _validate_assignments(
        beat,
        path=path,
        need_ids=need_ids,
        selected_refs=selected_refs,
        referenced_refs=referenced_refs,
        evidence_by_ref=evidence_by_ref,
        expected_beat_id=expected_beat_id,
        expected_duration_ms=duration_ms,
        issues=issues,
    )
    _validate_gap(
        beat,
        selected,
        alternatives,
        expected_alternative_reasons,
        path,
        issues,
    )
    return next_start


def _validate_beats(
    root: Mapping[str, object],
    evidence_by_ref: Mapping[str, Mapping[str, object]],
    issues: _Issues,
) -> tuple[int, set[str]]:
    beats = root.get("beats")
    if type(beats) is not list or not beats or len(beats) > COVERAGE_MAX_BEATS:
        issues.add("invalid_beats", "/beats", "Coverage Plan requires one to 256 Beats.")
        return 0, set()
    beat_ids: set[str] = set()
    block_ids: set[str] = set()
    selected_refs: set[str] = set()
    referenced_refs: set[str] = set()
    project_id = root.get("project_id")
    normalized_project_id = project_id if type(project_id) is str else None
    expected_start = 0
    for index, raw_beat in enumerate(beats):
        expected_start = _validate_beat(
            raw_beat,
            index=index,
            expected_start=expected_start,
            beat_ids=beat_ids,
            block_ids=block_ids,
            selected_refs=selected_refs,
            referenced_refs=referenced_refs,
            evidence_by_ref=evidence_by_ref,
            project_id=normalized_project_id,
            issues=issues,
        )
    return expected_start, referenced_refs


def _validate_document_size(value: object, issues: _Issues) -> None:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        issues.add("invalid_json", "", "Coverage Plan is not canonical JSON.")
        return
    if len(encoded) > COVERAGE_MAX_CANONICAL_BYTES:
        issues.add("document_too_large", "", "Coverage Plan exceeds the size limit.")


def validate_coverage_plan(value: object) -> list[dict[str, str]]:
    issues = _Issues()
    root = _closed(
        value,
        path="",
        fields={
            "object",
            "schema_version",
            "project_id",
            "revision",
            "parent",
            "blueprint_binding",
            "compiler",
            "output_binding",
            "beats",
            "evidence_manifest",
        },
        issues=issues,
    )
    if root is None:
        return issues.items
    if root.get("object") != COVERAGE_PLAN_OBJECT:
        issues.add("invalid_object", "/object", "Coverage Plan object is invalid.")
    if root.get("schema_version") != COVERAGE_PLAN_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Coverage Plan schema is unsupported.")
    _identifier(root.get("project_id"), "/project_id", issues)
    _positive_int(root.get("revision"), "/revision", issues)
    _validate_parent(root, issues)
    _validate_blueprint_binding(root, issues)
    _validate_compiler(root, issues)
    output = _validate_output(root, issues)
    evidence_by_ref = _validate_manifest(root, issues)
    expected_end, referenced_refs = _validate_beats(root, evidence_by_ref, issues)
    if set(evidence_by_ref) != referenced_refs:
        issues.add(
            "evidence_manifest_reference_mismatch",
            "/evidence_manifest",
            "Evidence manifest must exactly match all selected and alternative references.",
        )
    if output is not None and type(output.get("duration_target_ms")) is int:
        if expected_end != output["duration_target_ms"]:
            issues.add("duration_mismatch", "/beats", "Beat timing must cover the declared target exactly.")
    _validate_document_size(value, issues)
    return issues.items[:64]


def require_valid_coverage_plan(value: object) -> dict[str, Any]:
    """Parse a structurally valid current or historical Coverage document.

    Identity-only image proofs remain readable for historical inspection.  A
    caller that can lower, reconcile, or persist current state must use
    :func:`require_current_coverage_plan` instead.
    """

    errors = validate_coverage_plan(value)
    if errors:
        raise CoverageContractError(errors)
    assert isinstance(value, dict)
    return value


def _legacy_image_proof_path(value: Mapping[str, object]) -> str | None:
    manifest = value.get("evidence_manifest")
    if type(manifest) is not list:
        return None
    for index, row in enumerate(manifest):
        if not isinstance(row, Mapping):
            continue
        proof = row.get("proof")
        if (
            type(proof) is dict
            and proof.get("kind") == "asset"
            and set(proof) == _LEGACY_IMAGE_PROOF_FIELDS
        ):
            return f"/evidence_manifest/{index}/proof"
    return None


def inspect_coverage_plan_for_read_only(value: object) -> dict[str, object]:
    """Parse Coverage for inspection and expose image-evidence currency.

    The status is deliberately outside the canonical document so historical
    bytes and digests remain inspectable without silently repairing authority.
    """

    document = require_valid_coverage_plan(value)
    legacy_path = _legacy_image_proof_path(document)
    return {
        "document": document,
        "evidence_currency": "legacy_non_current" if legacy_path else "current",
        "eligible_for_current_use": legacy_path is None,
        "reason_code": "legacy_image_identity_only" if legacy_path else None,
        "reason_path": legacy_path,
    }


def require_current_coverage_plan(value: object) -> dict[str, Any]:
    """Require exact current image analysis evidence for mutable consumers."""

    document = require_valid_coverage_plan(value)
    legacy_path = _legacy_image_proof_path(document)
    if legacy_path is not None:
        raise CoverageContractError(
            [
                {
                    "code": "legacy_image_evidence_non_current",
                    "path": legacy_path,
                    "message": (
                        "Identity-only image evidence is historical and cannot be lowered, edited, or saved."
                    ),
                }
            ]
        )
    return document


def require_exact_baseline_derivation(
    value: object,
    *,
    blueprint: Mapping[str, object],
) -> dict[str, Any]:
    """Prove a stored A1 plan is exactly reproducible from its pinned Blueprint.

    Shape validation alone cannot prove that a Beat was not dropped, that a
    digest came from the corresponding script or hint, or that an assignment
    was not moved to another Beat.  Persistence uses this stronger check with
    the immutable Blueprint revision and the plan's already-frozen evidence
    proofs.
    """

    plan = require_current_coverage_plan(value)
    manifest = plan["evidence_manifest"]
    assert isinstance(manifest, list)
    evidence_rows = [
        {
            "evidence_ref": item["evidence_ref"],
            "proof": item["proof"],
        }
        for item in manifest
        if isinstance(item, dict)
    ]
    try:
        expected = compile_baseline_coverage_plan(
            project_id=str(plan["project_id"]),
            revision=int(plan["revision"]),
            parent=plan["parent"] if isinstance(plan["parent"], Mapping) else None,
            blueprint=blueprint,
            blueprint_binding=plan["blueprint_binding"],
            evidence_rows=evidence_rows,
        )
    except CoverageContractError as exc:
        raise CoverageContractError(
            [
                {
                    "code": "baseline_derivation_mismatch",
                    "path": "",
                    "message": (
                        "Coverage Plan inputs cannot reproduce the exact deterministic "
                        "baseline for its pinned Creative Blueprint."
                    ),
                }
            ]
        ) from exc
    if expected != plan:
        raise CoverageContractError(
            [
                {
                    "code": "baseline_derivation_mismatch",
                    "path": "",
                    "message": (
                        "Coverage Plan is not the exact deterministic baseline "
                        "for its pinned Creative Blueprint and evidence proofs."
                    ),
                }
            ]
        )
    return plan


def coverage_plan_content_sha256(value: Mapping[str, object]) -> str:
    require_valid_coverage_plan(value)
    return canonical_sha256(value)


__all__ = [
    "COVERAGE_BASELINE_COMPILER",
    "COVERAGE_MATCH_TYPE",
    "COVERAGE_PLAN_OBJECT",
    "COVERAGE_PLAN_SCHEMA_VERSION",
    "COVERAGE_TIMING_BASIS",
    "CoverageContractError",
    "canonical_sha256",
    "compile_baseline_coverage_plan",
    "coverage_plan_content_sha256",
    "inspect_coverage_plan_for_read_only",
    "normalize_evidence_proofs",
    "require_current_coverage_plan",
    "require_exact_baseline_derivation",
    "require_valid_coverage_plan",
    "validate_coverage_plan",
]
