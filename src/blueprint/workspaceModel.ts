import {
  BLUEPRINT_DECISION_UNITS,
} from "./authorityTypes";
import type {
  CanonicalTimelineBlueprintBinding,
  CanonicalTimelineCoverageBinding,
  CanonicalTimelineHead,
} from "./timelineTypes";
import {
  isActiveCanonicalExportJobStatus,
  isCanonicalExportJobStatus,
  normalizeCanonicalExportPackageBasename,
} from "./exportModel";
import type {
  BlueprintAssumption,
  BlueprintAuthorityEventSummary,
  BlueprintAuthorityProjection,
  BlueprintConstraint,
  BlueprintCreatorContextBinding,
  BlueprintCurrentRevision,
  BlueprintDocumentAuthority,
  BlueprintEvidenceManifestItem,
  BlueprintMaterialHint,
  BlueprintMissingEvidence,
  BlueprintOpenDecision,
  BlueprintOperationSummary,
  BlueprintProjectWorkspace,
  BlueprintReference,
  BlueprintRevisionRef,
  BlueprintSectionDiff,
  BlueprintSemantic,
  BlueprintSemanticSection,
  BlueprintTechniqueReference,
  BlueprintWikiGenerationBinding,
  CreativeBlueprintDocument,
  LegacyBriefSummary,
  LegacyTimelineSummary,
} from "./workspaceTypes";

const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const EVIDENCE_REF = /^memolens:\/\/evidence\/(asset|span)\/[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const TIMELINE_EXECUTABLE_REASON = {
  current: "canonical_timeline_current",
  stale_blueprint: "canonical_timeline_stale_blueprint",
  stale_coverage: "canonical_timeline_stale_coverage",
  stale_source_binding: "canonical_timeline_stale_source_binding",
} as const;
const EXECUTABLE_REASON_CODES = [
  "coverage_plan_missing",
  "coverage_plan_stale_blueprint",
  "coverage_plan_stale_evidence",
  "coverage_not_lowerable",
  "canonical_timeline_materialization_available",
  ...Object.values(TIMELINE_EXECUTABLE_REASON),
] as const;

export const BLUEPRINT_SEMANTIC_SECTIONS: readonly BlueprintSemanticSection[] = [
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
];

function fail(path: string): never {
  throw new Error(`Invalid canonical Blueprint workspace at ${path}.`);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fail(path);
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  path: string,
): void {
  const actual = Object.keys(value).sort();
  const frozen = [...expected].sort();
  if (
    actual.length !== frozen.length
    || actual.some((key, index) => key !== frozen[index])
  ) fail(path);
}

function closedRecord(
  value: unknown,
  expected: readonly string[],
  path: string,
): Record<string, unknown> {
  const raw = record(value, path);
  exactKeys(raw, expected, path);
  return raw;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) return fail(path);
  return value;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) return fail(path);
  return value;
}

function exactString<T extends string>(value: unknown, expected: T, path: string): T {
  if (value !== expected) return fail(path);
  return expected;
}

function oneOf<T extends string>(value: unknown, choices: readonly T[], path: string): T {
  if (typeof value !== "string" || !choices.includes(value as T)) return fail(path);
  return value as T;
}

function nullableString(value: unknown, path: string): string | null {
  if (value === null) return null;
  return string(value, path);
}

function integer(value: unknown, path: string, minimum = 0): number {
  if (!Number.isInteger(value) || Number(value) < minimum) return fail(path);
  return Number(value);
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") return fail(path);
  return value;
}

function digest(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (!SHA256.test(normalized)) return fail(path);
  return normalized;
}

function identifier(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (!IDENTIFIER.test(normalized)) return fail(path);
  return normalized;
}

function timestamp(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (Number.isNaN(Date.parse(normalized))) return fail(path);
  return normalized;
}

function nullableIdentifier(value: unknown, path: string): string | null {
  return value === null ? null : identifier(value, path);
}

function stringList(value: unknown, path: string): string[] {
  return array(value, path).map((item, index) => string(item, `${path}[${index}]`));
}

function identifierList(value: unknown, path: string): string[] {
  const result = array(value, path).map((item, index) => identifier(item, `${path}[${index}]`));
  if (new Set(result).size !== result.length) fail(path);
  return result;
}

function uniqueBy<T>(
  values: readonly T[],
  key: (value: T) => string | number,
  path: string,
): void {
  const keys = values.map(key);
  if (new Set(keys).size !== keys.length) fail(path);
}

function revisionRef(value: unknown, path: string): BlueprintRevisionRef {
  const raw = closedRecord(value, ["revision", "content_sha256"], path);
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
  };
}

function nullableRevisionRef(value: unknown, path: string): BlueprintRevisionRef | null {
  return value === null ? null : revisionRef(value, path);
}

function timelineBlueprintBinding(
  value: unknown,
  path: string,
): CanonicalTimelineBlueprintBinding {
  const raw = closedRecord(
    value,
    ["revision", "content_sha256", "semantic_sha256", "operation_id"],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
    semantic_sha256: digest(raw.semantic_sha256, `${path}.semantic_sha256`),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function timelineCoverageBinding(
  value: unknown,
  path: string,
): CanonicalTimelineCoverageBinding {
  const raw = closedRecord(
    value,
    ["revision", "content_sha256", "evidence_manifest_sha256", "operation_id"],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
    evidence_manifest_sha256: digest(
      raw.evidence_manifest_sha256,
      `${path}.evidence_manifest_sha256`,
    ),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function canonicalTimelineHead(value: unknown, path: string): CanonicalTimelineHead {
  const raw = closedRecord(
    value,
    [
      "revision",
      "revision_sha256",
      "timeline_id",
      "timeline_content_sha256",
      "blueprint_binding",
      "coverage_binding",
      "operation_id",
    ],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    revision_sha256: digest(raw.revision_sha256, `${path}.revision_sha256`),
    timeline_id: identifier(raw.timeline_id, `${path}.timeline_id`),
    timeline_content_sha256: digest(
      raw.timeline_content_sha256,
      `${path}.timeline_content_sha256`,
    ),
    blueprint_binding: timelineBlueprintBinding(
      raw.blueprint_binding,
      `${path}.blueprint_binding`,
    ),
    coverage_binding: timelineCoverageBinding(
      raw.coverage_binding,
      `${path}.coverage_binding`,
    ),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function constraint(value: unknown, path: string): BlueprintConstraint {
  const raw = closedRecord(
    value,
    ["constraint_id", "text", "evidence_ref", "script_block_ids"],
    path,
  );
  const evidenceRef = nullableString(raw.evidence_ref, `${path}.evidence_ref`);
  if (evidenceRef !== null && !EVIDENCE_REF.test(evidenceRef)) fail(`${path}.evidence_ref`);
  const result = {
    constraint_id: identifier(raw.constraint_id, `${path}.constraint_id`),
    text: nullableString(raw.text, `${path}.text`),
    evidence_ref: evidenceRef,
    script_block_ids: identifierList(raw.script_block_ids, `${path}.script_block_ids`),
  };
  if (result.text === null && result.evidence_ref === null) fail(path);
  return result;
}

function normalizeSemantic(value: unknown, path: string): BlueprintSemantic {
  const raw = closedRecord(value, BLUEPRINT_SEMANTIC_SECTIONS, path);
  const intent = closedRecord(
    raw.intent,
    ["goal", "stance", "audience", "platform"],
    `${path}.intent`,
  );
  const script = closedRecord(raw.script, ["blocks"], `${path}.script`);
  const direction = closedRecord(
    raw.direction,
    ["theme", "narrative_arc", "emotion", "tone", "pace"],
    `${path}.direction`,
  );
  const output = closedRecord(
    raw.output,
    ["duration_target_ms", "aspect_ratio"],
    `${path}.output`,
  );
  const constraints = closedRecord(
    raw.constraints,
    ["must_include", "must_exclude"],
    `${path}.constraints`,
  );
  const bindings = closedRecord(
    raw.bindings,
    ["creator_context", "wiki_generation"],
    `${path}.bindings`,
  );
  const duration = output.duration_target_ms === null
    ? null
    : integer(output.duration_target_ms, `${path}.output.duration_target_ms`, 1_000);
  const aspect = output.aspect_ratio === null
    ? null
    : oneOf(output.aspect_ratio, ["16:9", "9:16", "1:1", "4:5"] as const, `${path}.output.aspect_ratio`);
  const materialHints: BlueprintMaterialHint[] = array(raw.material_hints, `${path}.material_hints`).map((item, index) => {
    const hintPath = `${path}.material_hints[${index}]`;
    const hint = closedRecord(
      item,
      ["hint_id", "evidence_ref", "script_block_ids", "reason"],
      hintPath,
    );
    const evidenceRef = string(hint.evidence_ref, `${hintPath}.evidence_ref`);
    if (!EVIDENCE_REF.test(evidenceRef)) fail(`${hintPath}.evidence_ref`);
    return {
      hint_id: identifier(hint.hint_id, `${hintPath}.hint_id`),
      evidence_ref: evidenceRef,
      script_block_ids: identifierList(hint.script_block_ids, `${hintPath}.script_block_ids`),
      reason: nullableString(hint.reason, `${hintPath}.reason`),
    };
  });
  const references: BlueprintReference[] = array(raw.reference_refs, `${path}.reference_refs`).map((item, index) => {
    const itemPath = `${path}.reference_refs[${index}]`;
    const reference = closedRecord(
      item,
      ["reference_id", "kind", "locator", "note"],
      itemPath,
    );
    return {
      reference_id: identifier(reference.reference_id, `${itemPath}.reference_id`),
      kind: oneOf(reference.kind, ["project", "research_snapshot", "evidence", "external_https", "user_text"] as const, `${itemPath}.kind`),
      locator: string(reference.locator, `${itemPath}.locator`),
      note: nullableString(reference.note, `${itemPath}.note`),
    };
  });
  const techniques: BlueprintTechniqueReference[] = array(raw.technique_refs, `${path}.technique_refs`).map((item, index) => {
    const itemPath = `${path}.technique_refs[${index}]`;
    const technique = closedRecord(
      item,
      ["card_id", "revision", "declared_state"],
      itemPath,
    );
    return {
      card_id: identifier(technique.card_id, `${itemPath}.card_id`),
      revision: integer(technique.revision, `${itemPath}.revision`, 1),
      declared_state: oneOf(technique.declared_state, ["proposed", "selected", "rejected"] as const, `${itemPath}.declared_state`),
    };
  });
  const assumptions: BlueprintAssumption[] = array(raw.assumptions, `${path}.assumptions`).map((item, index) => {
    const itemPath = `${path}.assumptions[${index}]`;
    const assumption = closedRecord(item, ["assumption_id", "text"], itemPath);
    return {
      assumption_id: identifier(assumption.assumption_id, `${itemPath}.assumption_id`),
      text: string(assumption.text, `${itemPath}.text`),
    };
  });
  const requiredBefore = ["coverage", "timeline", "render", "publish", "not_blocking"] as const;
  const missingEvidence: BlueprintMissingEvidence[] = array(raw.missing_evidence, `${path}.missing_evidence`).map((item, index) => {
    const itemPath = `${path}.missing_evidence[${index}]`;
    const gap = closedRecord(
      item,
      ["gap_id", "description", "script_block_ids", "required_before"],
      itemPath,
    );
    return {
      gap_id: identifier(gap.gap_id, `${itemPath}.gap_id`),
      description: string(gap.description, `${itemPath}.description`),
      script_block_ids: identifierList(gap.script_block_ids, `${itemPath}.script_block_ids`),
      required_before: oneOf(gap.required_before, requiredBefore, `${itemPath}.required_before`),
    };
  });
  const openDecisions: BlueprintOpenDecision[] = array(raw.open_decisions, `${path}.open_decisions`).map((item, index) => {
    const itemPath = `${path}.open_decisions[${index}]`;
    const decision = closedRecord(
      item,
      ["decision_id", "question", "scope", "required_before"],
      itemPath,
    );
    return {
      decision_id: identifier(decision.decision_id, `${itemPath}.decision_id`),
      question: string(decision.question, `${itemPath}.question`),
      scope: oneOf(decision.scope, ["intent", "stance", "script", "direction", "output", "material", "reference", "technique", "other"] as const, `${itemPath}.scope`),
      required_before: oneOf(decision.required_before, requiredBefore, `${itemPath}.required_before`),
    };
  });
  const scriptBlocks = array(script.blocks, `${path}.script.blocks`).map((item, index) => {
    const itemPath = `${path}.script.blocks[${index}]`;
    const block = closedRecord(item, ["block_id", "text"], itemPath);
    return {
      block_id: identifier(block.block_id, `${itemPath}.block_id`),
      text: string(block.text, `${itemPath}.text`),
    };
  });
  const creatorContext: BlueprintCreatorContextBinding | null = bindings.creator_context === null
    ? null
    : (() => {
      const itemPath = `${path}.bindings.creator_context`;
      const binding = closedRecord(
        bindings.creator_context,
        ["profile_id", "revision", "content_sha256"],
        itemPath,
      );
      return {
        profile_id: identifier(binding.profile_id, `${itemPath}.profile_id`),
        revision: integer(binding.revision, `${itemPath}.revision`, 1),
        content_sha256: digest(binding.content_sha256, `${itemPath}.content_sha256`),
      };
    })();
  const wikiGeneration: BlueprintWikiGenerationBinding | null = bindings.wiki_generation === null
    ? null
    : (() => {
      const itemPath = `${path}.bindings.wiki_generation`;
      const binding = closedRecord(
        bindings.wiki_generation,
        ["generation_id", "content_sha256"],
        itemPath,
      );
      return {
        generation_id: identifier(binding.generation_id, `${itemPath}.generation_id`),
        content_sha256: digest(binding.content_sha256, `${itemPath}.content_sha256`),
      };
    })();
  const result: BlueprintSemantic = {
    intent: {
      goal: nullableString(intent.goal, `${path}.intent.goal`),
      stance: nullableString(intent.stance, `${path}.intent.stance`),
      audience: nullableString(intent.audience, `${path}.intent.audience`),
      platform: nullableString(intent.platform, `${path}.intent.platform`),
    },
    script: {
      blocks: scriptBlocks,
    },
    direction: {
      theme: nullableString(direction.theme, `${path}.direction.theme`),
      narrative_arc: nullableString(direction.narrative_arc, `${path}.direction.narrative_arc`),
      emotion: nullableString(direction.emotion, `${path}.direction.emotion`),
      tone: nullableString(direction.tone, `${path}.direction.tone`),
      pace: nullableString(direction.pace, `${path}.direction.pace`),
    },
    output: { duration_target_ms: duration, aspect_ratio: aspect },
    constraints: {
      must_include: array(constraints.must_include, `${path}.constraints.must_include`).map((item, index) => constraint(item, `${path}.constraints.must_include[${index}]`)),
      must_exclude: array(constraints.must_exclude, `${path}.constraints.must_exclude`).map((item, index) => constraint(item, `${path}.constraints.must_exclude[${index}]`)),
    },
    material_hints: materialHints,
    reference_refs: references,
    technique_refs: techniques,
    bindings: {
      creator_context: creatorContext,
      wiki_generation: wikiGeneration,
    },
    assumptions,
    missing_evidence: missingEvidence,
    open_decisions: openDecisions,
  };
  uniqueBy(scriptBlocks, (item) => item.block_id, `${path}.script.blocks`);
  uniqueBy(materialHints, (item) => item.hint_id, `${path}.material_hints`);
  uniqueBy(references, (item) => item.reference_id, `${path}.reference_refs`);
  uniqueBy(techniques, (item) => `${item.card_id}:${item.revision}`, `${path}.technique_refs`);
  uniqueBy(assumptions, (item) => item.assumption_id, `${path}.assumptions`);
  uniqueBy(missingEvidence, (item) => item.gap_id, `${path}.missing_evidence`);
  uniqueBy(openDecisions, (item) => item.decision_id, `${path}.open_decisions`);
  const allConstraints = [
    ...result.constraints.must_include,
    ...result.constraints.must_exclude,
  ];
  uniqueBy(allConstraints, (item) => item.constraint_id, `${path}.constraints`);
  return result;
}

function authorityProjection(value: unknown, path: string): BlueprintAuthorityProjection {
  const raw = closedRecord(
    value,
    [
      "state",
      "confirmed_decision_unit_count",
      "decision_unit_count",
      "as_of_revision",
      "as_of_content_sha256",
      "decision_units",
    ],
    path,
  );
  const rawUnits = closedRecord(
    raw.decision_units,
    BLUEPRINT_DECISION_UNITS,
    `${path}.decision_units`,
  );
  if (Object.keys(rawUnits).length !== BLUEPRINT_DECISION_UNITS.length) fail(`${path}.decision_units`);
  const units = Object.fromEntries(BLUEPRINT_DECISION_UNITS.map((name) => {
    const unitPath = `${path}.decision_units.${name}`;
    const unit = closedRecord(
      rawUnits[name],
      ["semantic_sha256", "state", "confirmed", "active_confirmation"],
      unitPath,
    );
    const confirmed = boolean(unit.confirmed, `${unitPath}.confirmed`);
    const state = oneOf(unit.state, ["confirmed", "unverified"] as const, `${unitPath}.state`);
    if (confirmed !== (state === "confirmed")) fail(unitPath);
    let activeConfirmation = null;
    if (unit.active_confirmation !== null) {
      const confirmation = closedRecord(
        unit.active_confirmation,
        ["event_id", "confirmed_at", "observed_revision", "presentation_sha256"],
        `${unitPath}.active_confirmation`,
      );
      activeConfirmation = {
        event_id: identifier(confirmation.event_id, `${unitPath}.active_confirmation.event_id`),
        confirmed_at: timestamp(confirmation.confirmed_at, `${unitPath}.active_confirmation.confirmed_at`),
        observed_revision: integer(confirmation.observed_revision, `${unitPath}.active_confirmation.observed_revision`, 1),
        presentation_sha256: digest(confirmation.presentation_sha256, `${unitPath}.active_confirmation.presentation_sha256`),
      };
    }
    if (confirmed !== (activeConfirmation !== null)) fail(`${unitPath}.active_confirmation`);
    return [name, {
      semantic_sha256: digest(unit.semantic_sha256, `${unitPath}.semantic_sha256`),
      state,
      confirmed,
      active_confirmation: activeConfirmation,
    }];
  })) as BlueprintAuthorityProjection["decision_units"];
  const count = integer(raw.confirmed_decision_unit_count, `${path}.confirmed_decision_unit_count`);
  if (count !== Object.values(units).filter((unit) => unit.confirmed).length) fail(`${path}.confirmed_decision_unit_count`);
  const unitCount = integer(raw.decision_unit_count, `${path}.decision_unit_count`, 8);
  if (unitCount !== 8) fail(`${path}.decision_unit_count`);
  const state = oneOf(raw.state, ["unverified", "partially_confirmed", "confirmed"] as const, `${path}.state`);
  const expectedState = count === 0 ? "unverified" : count === 8 ? "confirmed" : "partially_confirmed";
  if (state !== expectedState) fail(`${path}.state`);
  return {
    state,
    confirmed_decision_unit_count: count,
    decision_unit_count: unitCount as 8,
    as_of_revision: integer(raw.as_of_revision, `${path}.as_of_revision`, 1),
    as_of_content_sha256: digest(raw.as_of_content_sha256, `${path}.as_of_content_sha256`),
    decision_units: units,
  };
}

function documentAuthority(value: unknown, path: string): BlueprintDocumentAuthority {
  const raw = closedRecord(value, ["state", "decision_units"], path);
  const rawUnits = closedRecord(
    raw.decision_units,
    BLUEPRINT_DECISION_UNITS,
    `${path}.decision_units`,
  );
  const decisionUnits = Object.fromEntries(BLUEPRINT_DECISION_UNITS.map((name) => {
    const unitPath = `${path}.decision_units.${name}`;
    const unit = closedRecord(
      rawUnits[name],
      ["semantic_sha256", "claim", "verified"],
      unitPath,
    );
    return [name, {
      semantic_sha256: digest(unit.semantic_sha256, `${unitPath}.semantic_sha256`),
      claim: exactString(unit.claim, "agent_proposal", `${unitPath}.claim`),
      verified: unit.verified === false ? false : fail(`${unitPath}.verified`),
    }];
  })) as BlueprintDocumentAuthority["decision_units"];
  return {
    state: exactString(raw.state, "unverified", `${path}.state`),
    decision_units: decisionUnits,
  };
}

function document(value: unknown, path: string): CreativeBlueprintDocument {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "schema_sha256",
      "project_id",
      "revision",
      "parent",
      "created_by_operation_id",
      "status",
      "semantic_sha256",
      "semantic",
      "lineage",
      "evidence_manifest",
      "authority",
    ],
    path,
  );
  const lineage = closedRecord(
    raw.lineage,
    ["compiler", "source_candidate_sha256", "initial_legacy_brief"],
    `${path}.lineage`,
  );
  const evidenceManifest: BlueprintEvidenceManifestItem[] = array(raw.evidence_manifest, `${path}.evidence_manifest`).map((item, index) => {
    const itemPath = `${path}.evidence_manifest[${index}]`;
    const evidence = closedRecord(
      item,
      ["evidence_ref", "status", "proof_sha256"],
      itemPath,
    );
    const evidenceRef = string(evidence.evidence_ref, `${itemPath}.evidence_ref`);
    if (!EVIDENCE_REF.test(evidenceRef)) fail(`${itemPath}.evidence_ref`);
    return {
      evidence_ref: evidenceRef,
      status: oneOf(evidence.status, ["verified", "unresolved"] as const, `${itemPath}.status`),
      proof_sha256: evidence.proof_sha256 === null ? null : digest(evidence.proof_sha256, `${itemPath}.proof_sha256`),
    };
  });
  for (const [index, evidence] of evidenceManifest.entries()) {
    if ((evidence.status === "verified") !== (evidence.proof_sha256 !== null)) {
      fail(`${path}.evidence_manifest[${index}].proof_sha256`);
    }
  }
  uniqueBy(evidenceManifest, (item) => item.evidence_ref, `${path}.evidence_manifest`);
  return {
    object: exactString(raw.object, "memolens.creative_blueprint", `${path}.object`),
    schema_version: exactString(raw.schema_version, "1", `${path}.schema_version`),
    schema_sha256: digest(raw.schema_sha256, `${path}.schema_sha256`),
    project_id: identifier(raw.project_id, `${path}.project_id`),
    revision: integer(raw.revision, `${path}.revision`, 1),
    parent: nullableRevisionRef(raw.parent, `${path}.parent`),
    created_by_operation_id: identifier(raw.created_by_operation_id, `${path}.created_by_operation_id`),
    status: exactString(raw.status, "proposal", `${path}.status`),
    semantic_sha256: digest(raw.semantic_sha256, `${path}.semantic_sha256`),
    semantic: normalizeSemantic(raw.semantic, `${path}.semantic`),
    lineage: {
      compiler: exactString(lineage.compiler, "memolens.blueprint-proposal-compiler/v1", `${path}.lineage.compiler`),
      source_candidate_sha256: lineage.source_candidate_sha256 === null ? null : digest(lineage.source_candidate_sha256, `${path}.lineage.source_candidate_sha256`),
      initial_legacy_brief: revisionRef(lineage.initial_legacy_brief, `${path}.lineage.initial_legacy_brief`),
    },
    evidence_manifest: evidenceManifest,
    authority: documentAuthority(raw.authority, `${path}.authority`),
  };
}

function revisionResource(
  value: unknown,
  path: string,
  options: { requireCurrent?: boolean } = {},
): BlueprintCurrentRevision {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "database_uuid",
      "project_id",
      "revision",
      "content_sha256",
      "semantic_sha256",
      "operation_id",
      "current",
      "blueprint",
      "authority",
      "creation_authority",
      "authority_projection",
    ],
    path,
  );
  const authority = closedRecord(
    raw.authority,
    ["state", "verified"],
    `${path}.authority`,
  );
  const creationAuthority = closedRecord(
    raw.creation_authority,
    ["state", "verified"],
    `${path}.creation_authority`,
  );
  const result: BlueprintCurrentRevision = {
    object: exactString(raw.object, "creative_blueprint.revision", `${path}.object`),
    schema_version: exactString(raw.schema_version, "1", `${path}.schema_version`),
    database_uuid: identifier(raw.database_uuid, `${path}.database_uuid`),
    project_id: identifier(raw.project_id, `${path}.project_id`),
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
    semantic_sha256: digest(raw.semantic_sha256, `${path}.semantic_sha256`),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
    current: boolean(raw.current, `${path}.current`),
    blueprint: document(raw.blueprint, `${path}.blueprint`),
    authority: {
      state: exactString(authority.state, "unverified", `${path}.authority.state`),
      verified: authority.verified === false ? false : fail(`${path}.authority.verified`),
    },
    creation_authority: {
      state: exactString(creationAuthority.state, "unverified", `${path}.creation_authority.state`),
      verified: creationAuthority.verified === false ? false : fail(`${path}.creation_authority.verified`),
    },
    authority_projection: authorityProjection(raw.authority_projection, `${path}.authority_projection`),
  };
  if (
    (options.requireCurrent && !result.current)
    || result.project_id !== result.blueprint.project_id
    || result.revision !== result.blueprint.revision
    || result.semantic_sha256 !== result.blueprint.semantic_sha256
    || result.operation_id !== result.blueprint.created_by_operation_id
    || result.authority_projection.as_of_revision !== result.revision
    || result.authority_projection.as_of_content_sha256 !== result.content_sha256
    || BLUEPRINT_DECISION_UNITS.some((name) => {
      const active = result.authority_projection.decision_units[name].active_confirmation;
      return active !== null && active.observed_revision > result.revision;
    })
    || BLUEPRINT_DECISION_UNITS.some((name) => (
      result.blueprint.authority.decision_units[name].semantic_sha256
      !== result.authority_projection.decision_units[name].semantic_sha256
    ))
  ) fail(path);
  return result;
}

export function normalizeBlueprintRevisionResource(value: unknown): BlueprintCurrentRevision {
  return revisionResource(value, "blueprint_revision");
}

function operation(value: unknown, path: string): BlueprintOperationSummary {
  const raw = closedRecord(
    value,
    [
      "operation_id",
      "sequence",
      "parent_operation_id",
      "command_type",
      "intent_code",
      "result_kind",
      "result_revision",
      "result_content_sha256",
      "changed_sections",
      "restore_from",
      "input_sha256",
      "output_sha256",
      "created_at",
    ],
    path,
  );
  const changedSections = stringList(raw.changed_sections, `${path}.changed_sections`).map((section, index) => oneOf(section, BLUEPRINT_SEMANTIC_SECTIONS, `${path}.changed_sections[${index}]`));
  if (new Set(changedSections).size !== changedSections.length) fail(`${path}.changed_sections`);
  return {
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
    sequence: integer(raw.sequence, `${path}.sequence`, 1),
    parent_operation_id: nullableIdentifier(raw.parent_operation_id, `${path}.parent_operation_id`),
    command_type: oneOf(raw.command_type, ["blueprint.commit_proposal", "blueprint.restore_revision"] as const, `${path}.command_type`),
    intent_code: oneOf(raw.intent_code, ["propose_blueprint", "restore_blueprint"] as const, `${path}.intent_code`),
    result_kind: oneOf(raw.result_kind, ["revision_created", "revision_restored", "no_change"] as const, `${path}.result_kind`),
    result_revision: integer(raw.result_revision, `${path}.result_revision`, 1),
    result_content_sha256: digest(raw.result_content_sha256, `${path}.result_content_sha256`),
    changed_sections: changedSections,
    restore_from: nullableRevisionRef(raw.restore_from, `${path}.restore_from`),
    input_sha256: digest(raw.input_sha256, `${path}.input_sha256`),
    output_sha256: digest(raw.output_sha256, `${path}.output_sha256`),
    created_at: timestamp(raw.created_at, `${path}.created_at`),
  };
}

function authorityEvent(value: unknown, path: string): BlueprintAuthorityEventSummary {
  const raw = closedRecord(
    value,
    [
      "event_id",
      "sequence",
      "authority_operation",
      "observed_revision",
      "decision_units",
      "created_at",
    ],
    path,
  );
  const decisionUnits = stringList(raw.decision_units, `${path}.decision_units`).map((name, index) => oneOf(name, BLUEPRINT_DECISION_UNITS, `${path}.decision_units[${index}]`));
  if (new Set(decisionUnits).size !== decisionUnits.length) fail(`${path}.decision_units`);
  return {
    event_id: identifier(raw.event_id, `${path}.event_id`),
    sequence: integer(raw.sequence, `${path}.sequence`, 1),
    authority_operation: oneOf(raw.authority_operation, ["confirm", "revoke"] as const, `${path}.authority_operation`),
    observed_revision: integer(raw.observed_revision, `${path}.observed_revision`, 1),
    decision_units: decisionUnits,
    created_at: timestamp(raw.created_at, `${path}.created_at`),
  };
}

export function normalizeBlueprintWorkspace(value: unknown): BlueprintProjectWorkspace {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "database_uuid",
      "id",
      "project_id",
      "title",
      "status",
      "canonical_source",
      "workspace_mode",
      "current_blueprint",
      "coverage",
      "timeline",
      "canonical_export",
      "executable",
      "history",
      "capabilities",
      "created_at",
      "updated_at",
    ],
    "workspace",
  );
  const history = closedRecord(
    raw.history,
    [
      "complete_project_history",
      "global_total_order",
      "replay_scope",
      "blueprint",
      "decision_authority",
      "legacy_artifacts",
    ],
    "workspace.history",
  );
  const blueprintHistory = closedRecord(
    history.blueprint,
    [
      "lane",
      "operations",
      "available_revision_count",
      "available_operation_count",
      "operations_truncated",
      "ledger",
      "order",
    ],
    "workspace.history.blueprint",
  );
  const authorityHistory = closedRecord(
    history.decision_authority,
    [
      "lane",
      "events",
      "available_event_count",
      "events_truncated",
      "projection",
      "coverage_scope",
      "complete_project_history",
      "order",
    ],
    "workspace.history.decision_authority",
  );
  const legacy = closedRecord(
    history.legacy_artifacts,
    [
      "lane",
      "status",
      "reason_code",
      "role",
      "coverage_scope",
      "order",
      "authoritative_timeline_head",
      "briefs",
      "brief_count",
      "briefs_truncated",
      "timelines",
      "timeline_revision_count",
      "timelines_truncated",
    ],
    "workspace.history.legacy_artifacts",
  );
  const ledger = closedRecord(
    blueprintHistory.ledger,
    ["coverage_scope", "complete_project_history"],
    "workspace.history.blueprint.ledger",
  );
  const capabilities = closedRecord(
    raw.capabilities,
    [
      "complete_project_history",
      "authoritative_timeline_head",
      "coverage_plan_materialization",
      "blueprint_timeline_compiler",
      "timeline_materialization",
      "timeline_reconciliation",
      "timeline_mutation",
      "preview",
      "render",
      "export",
      "canonical_export",
    ],
    "workspace.capabilities",
  );
  const executable = closedRecord(
    raw.executable,
    ["state", "current_timeline", "reason_code"],
    "workspace.executable",
  );
  const coverage = closedRecord(
    raw.coverage,
    [
      "state",
      "head",
      "blueprint_binding",
      "beat_count",
      "selected_assignment_count",
      "gap_count",
    ],
    "workspace.coverage",
  );
  const coverageState = oneOf(
    coverage.state,
    ["missing", "current", "stale_blueprint", "stale_evidence"] as const,
    "workspace.coverage.state",
  );
  const coverageHead = nullableRevisionRef(coverage.head, "workspace.coverage.head");
  const coverageBinding = coverage.blueprint_binding === null
    ? null
    : (() => {
      const binding = closedRecord(
        coverage.blueprint_binding,
        ["revision", "content_sha256", "semantic_sha256"],
        "workspace.coverage.blueprint_binding",
      );
      return {
        revision: integer(binding.revision, "workspace.coverage.blueprint_binding.revision", 1),
        content_sha256: digest(binding.content_sha256, "workspace.coverage.blueprint_binding.content_sha256"),
        semantic_sha256: digest(binding.semantic_sha256, "workspace.coverage.blueprint_binding.semantic_sha256"),
      };
    })();
  const timeline = closedRecord(
    raw.timeline,
    [
      "state",
      "head",
      "blueprint_binding",
      "coverage_binding",
      "clip_count",
      "source_binding_count",
      "lifecycle",
    ],
    "workspace.timeline",
  );
  const timelineState = oneOf(
    timeline.state,
    ["missing", "current", "stale_blueprint", "stale_coverage", "stale_source_binding"] as const,
    "workspace.timeline.state",
  );
  const timelineHead = timeline.head === null
    ? null
    : canonicalTimelineHead(timeline.head, "workspace.timeline.head");
  const timelineBlueprint = timeline.blueprint_binding === null
    ? null
    : timelineBlueprintBinding(
      timeline.blueprint_binding,
      "workspace.timeline.blueprint_binding",
    );
  const timelineCoverage = timeline.coverage_binding === null
    ? null
    : timelineCoverageBinding(
      timeline.coverage_binding,
      "workspace.timeline.coverage_binding",
    );
  const timelineLifecycle = closedRecord(
    timeline.lifecycle,
    ["state", "approval", "exportable"],
    "workspace.timeline.lifecycle",
  );
  const canonicalExport = closedRecord(
    raw.canonical_export,
    ["state", "reason_code", "latest_job"],
    "workspace.canonical_export",
  );
  const canonicalExportState = oneOf(
    canonicalExport.state,
    ["blocked", "available", "in_progress"] as const,
    "workspace.canonical_export.state",
  );
  const canonicalExportReason = oneOf(
    canonicalExport.reason_code,
    [
      "timeline_missing",
      "timeline_stale",
      "export_recovery_required",
      "requires_native_confirmation",
      "export_in_progress",
    ] as const,
    "workspace.canonical_export.reason_code",
  );
  const latestExportJob = canonicalExport.latest_job === null
    ? null
    : (() => {
      const job = closedRecord(
        canonicalExport.latest_job,
        ["job_id", "status", "package_basename"],
        "workspace.canonical_export.latest_job",
      );
      if (!isCanonicalExportJobStatus(job.status)) {
        fail("workspace.canonical_export.latest_job.status");
      }
      if (
        typeof job.package_basename !== "string"
        || normalizeCanonicalExportPackageBasename(job.package_basename) !== job.package_basename
      ) fail("workspace.canonical_export.latest_job.package_basename");
      return {
        job_id: identifier(job.job_id, "workspace.canonical_export.latest_job.job_id"),
        status: job.status,
        package_basename: job.package_basename,
      };
    })();
  const executableTimeline = executable.current_timeline === null
    ? null
    : canonicalTimelineHead(
      executable.current_timeline,
      "workspace.executable.current_timeline",
    );
  const current = revisionResource(
    raw.current_blueprint,
    "workspace.current_blueprint",
    { requireCurrent: true },
  );
  const projection = authorityProjection(authorityHistory.projection, "workspace.history.decision_authority.projection");
  const briefs: LegacyBriefSummary[] = array(legacy.briefs, "workspace.history.legacy_artifacts.briefs").map((item, index) => {
    const itemPath = `workspace.history.legacy_artifacts.briefs[${index}]`;
    const brief = closedRecord(
      item,
      ["revision", "content_sha256", "created_at", "role"],
      itemPath,
    );
    return {
      revision: integer(brief.revision, `${itemPath}.revision`, 1),
      content_sha256: digest(brief.content_sha256, `${itemPath}.content_sha256`),
      created_at: timestamp(brief.created_at, `${itemPath}.created_at`),
      role: exactString(brief.role, "migration_context_only", `${itemPath}.role`),
    };
  });
  const timelines: LegacyTimelineSummary[] = array(legacy.timelines, "workspace.history.legacy_artifacts.timelines").map((item, index) => {
    const itemPath = `workspace.history.legacy_artifacts.timelines[${index}]`;
    const timeline = closedRecord(
      item,
      [
        "timeline_id",
        "revision",
        "parent_revision",
        "brief_revision",
        "schema_version",
        "content_sha256",
        "validation_status",
        "created_at",
        "role",
      ],
      itemPath,
    );
    return {
      timeline_id: identifier(timeline.timeline_id, `${itemPath}.timeline_id`),
      revision: integer(timeline.revision, `${itemPath}.revision`, 1),
      parent_revision: timeline.parent_revision === null ? null : integer(timeline.parent_revision, `${itemPath}.parent_revision`, 1),
      brief_revision: integer(timeline.brief_revision, `${itemPath}.brief_revision`, 1),
      schema_version: exactString(timeline.schema_version, "1.0", `${itemPath}.schema_version`),
      content_sha256: digest(timeline.content_sha256, `${itemPath}.content_sha256`),
      validation_status: oneOf(timeline.validation_status, ["valid", "invalid"] as const, `${itemPath}.validation_status`),
      created_at: timestamp(timeline.created_at, `${itemPath}.created_at`),
      role: exactString(timeline.role, "historical_observed_context_only", `${itemPath}.role`),
    };
  });
  const result: BlueprintProjectWorkspace = {
    object: exactString(raw.object, "creative.project_workspace", "workspace.object"),
    schema_version: exactString(raw.schema_version, "1", "workspace.schema_version"),
    database_uuid: identifier(raw.database_uuid, "workspace.database_uuid"),
    id: identifier(raw.id, "workspace.id"),
    project_id: identifier(raw.project_id, "workspace.project_id"),
    title: string(raw.title, "workspace.title"),
    status: oneOf(raw.status, ["draft", "active", "archived"] as const, "workspace.status"),
    canonical_source: exactString(raw.canonical_source, "creative_blueprint", "workspace.canonical_source"),
    workspace_mode: exactString(raw.workspace_mode, "canonical_blueprint", "workspace.workspace_mode"),
    current_blueprint: current,
    coverage: {
      state: coverageState,
      head: coverageHead,
      blueprint_binding: coverageBinding,
      beat_count: integer(coverage.beat_count, "workspace.coverage.beat_count"),
      selected_assignment_count: integer(
        coverage.selected_assignment_count,
        "workspace.coverage.selected_assignment_count",
      ),
      gap_count: integer(coverage.gap_count, "workspace.coverage.gap_count"),
    },
    timeline: {
      state: timelineState,
      head: timelineHead,
      blueprint_binding: timelineBlueprint,
      coverage_binding: timelineCoverage,
      clip_count: integer(timeline.clip_count, "workspace.timeline.clip_count"),
      source_binding_count: integer(
        timeline.source_binding_count,
        "workspace.timeline.source_binding_count",
      ),
      lifecycle: {
        state: oneOf(
          timelineLifecycle.state,
          ["not_materialized", "draft"] as const,
          "workspace.timeline.lifecycle.state",
        ),
        approval: exactString(
          timelineLifecycle.approval,
          "not_established",
          "workspace.timeline.lifecycle.approval",
        ),
        exportable: timelineLifecycle.exportable === false
          ? false
          : fail("workspace.timeline.lifecycle.exportable"),
      },
    },
    canonical_export: {
      state: canonicalExportState,
      reason_code: canonicalExportReason,
      latest_job: latestExportJob,
    },
    executable: {
      state: oneOf(
        executable.state,
        ["not_compiled", "compiled_draft"] as const,
        "workspace.executable.state",
      ),
      current_timeline: executableTimeline,
      reason_code: oneOf(
        executable.reason_code,
        EXECUTABLE_REASON_CODES,
        "workspace.executable.reason_code",
      ),
    },
    history: {
      complete_project_history: history.complete_project_history === false ? false : fail("workspace.history.complete_project_history"),
      global_total_order: history.global_total_order === false ? false : fail("workspace.history.global_total_order"),
      replay_scope: exactString(history.replay_scope, "per_lane_only", "workspace.history.replay_scope"),
      blueprint: {
        lane: exactString(blueprintHistory.lane, "semantic_changes", "workspace.history.blueprint.lane"),
        operations: array(blueprintHistory.operations, "workspace.history.blueprint.operations").map((item, index) => operation(item, `workspace.history.blueprint.operations[${index}]`)),
        available_revision_count: integer(blueprintHistory.available_revision_count, "workspace.history.blueprint.available_revision_count", 1),
        available_operation_count: integer(blueprintHistory.available_operation_count, "workspace.history.blueprint.available_operation_count", 1),
        operations_truncated: boolean(blueprintHistory.operations_truncated, "workspace.history.blueprint.operations_truncated"),
        ledger: {
          coverage_scope: exactString(ledger.coverage_scope, "creative_blueprint", "workspace.history.blueprint.ledger.coverage_scope"),
          complete_project_history: ledger.complete_project_history === false ? false : fail("workspace.history.blueprint.ledger.complete_project_history"),
        },
        order: exactString(blueprintHistory.order, "sequence_desc", "workspace.history.blueprint.order"),
      },
      decision_authority: {
        lane: exactString(authorityHistory.lane, "decision_authority", "workspace.history.decision_authority.lane"),
        events: array(authorityHistory.events, "workspace.history.decision_authority.events").map((item, index) => authorityEvent(item, `workspace.history.decision_authority.events[${index}]`)),
        available_event_count: integer(authorityHistory.available_event_count, "workspace.history.decision_authority.available_event_count"),
        events_truncated: boolean(authorityHistory.events_truncated, "workspace.history.decision_authority.events_truncated"),
        projection,
        coverage_scope: exactString(authorityHistory.coverage_scope, "blueprint_decision_authority", "workspace.history.decision_authority.coverage_scope"),
        complete_project_history: authorityHistory.complete_project_history === false ? false : fail("workspace.history.decision_authority.complete_project_history"),
        order: exactString(authorityHistory.order, "sequence_asc", "workspace.history.decision_authority.order"),
      },
      legacy_artifacts: {
        lane: exactString(legacy.lane, "legacy_artifacts", "workspace.history.legacy_artifacts.lane"),
        status: oneOf(legacy.status, ["available", "unavailable"] as const, "workspace.history.legacy_artifacts.status"),
        reason_code: legacy.reason_code === null
          ? null
          : exactString(legacy.reason_code, "legacy_artifact_history_integrity_error", "workspace.history.legacy_artifacts.reason_code"),
        role: exactString(legacy.role, "migration_context_only", "workspace.history.legacy_artifacts.role"),
        coverage_scope: exactString(legacy.coverage_scope, "legacy_briefs_and_timelines", "workspace.history.legacy_artifacts.coverage_scope"),
        order: exactString(
          legacy.order,
          "brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc",
          "workspace.history.legacy_artifacts.order",
        ),
        authoritative_timeline_head: legacy.authoritative_timeline_head === false ? false : fail("workspace.history.legacy_artifacts.authoritative_timeline_head"),
        briefs,
        brief_count: integer(legacy.brief_count, "workspace.history.legacy_artifacts.brief_count"),
        briefs_truncated: boolean(legacy.briefs_truncated, "workspace.history.legacy_artifacts.briefs_truncated"),
        timelines,
        timeline_revision_count: integer(legacy.timeline_revision_count, "workspace.history.legacy_artifacts.timeline_revision_count"),
        timelines_truncated: boolean(legacy.timelines_truncated, "workspace.history.legacy_artifacts.timelines_truncated"),
      },
    },
    capabilities: {
      complete_project_history: capabilities.complete_project_history === false ? false : fail("workspace.capabilities.complete_project_history"),
      authoritative_timeline_head: boolean(capabilities.authoritative_timeline_head, "workspace.capabilities.authoritative_timeline_head"),
      coverage_plan_materialization: capabilities.coverage_plan_materialization === true ? true : fail("workspace.capabilities.coverage_plan_materialization"),
      blueprint_timeline_compiler: capabilities.blueprint_timeline_compiler === true ? true : fail("workspace.capabilities.blueprint_timeline_compiler"),
      timeline_materialization: boolean(capabilities.timeline_materialization, "workspace.capabilities.timeline_materialization"),
      timeline_reconciliation: boolean(
        capabilities.timeline_reconciliation,
        "workspace.capabilities.timeline_reconciliation",
      ),
      timeline_mutation: boolean(
        capabilities.timeline_mutation,
        "workspace.capabilities.timeline_mutation",
      ),
      preview: capabilities.preview === false ? false : fail("workspace.capabilities.preview"),
      render: capabilities.render === false ? false : fail("workspace.capabilities.render"),
      export: capabilities.export === false ? false : fail("workspace.capabilities.export"),
      canonical_export: boolean(
        capabilities.canonical_export,
        "workspace.capabilities.canonical_export",
      ),
    },
    created_at: timestamp(raw.created_at, "workspace.created_at"),
    updated_at: timestamp(raw.updated_at, "workspace.updated_at"),
  };
  if (
    result.id !== result.project_id
    || result.id !== result.current_blueprint.project_id
    || result.database_uuid !== result.current_blueprint.database_uuid
    || projection.as_of_revision !== current.revision
    || projection.as_of_content_sha256 !== current.content_sha256
    || canonical(result.current_blueprint.authority_projection) !== canonical(projection)
  ) fail("workspace.identity");
  const coverageResult = result.coverage;
  const coverageBindingMatchesCurrent = coverageResult.blueprint_binding !== null
    && coverageResult.blueprint_binding.revision === current.revision
    && coverageResult.blueprint_binding.content_sha256 === current.content_sha256
    && coverageResult.blueprint_binding.semantic_sha256 === current.semantic_sha256;
  if (
    (
      coverageResult.state === "missing"
      && (
        coverageResult.head !== null
        || coverageResult.blueprint_binding !== null
        || coverageResult.beat_count !== 0
        || coverageResult.selected_assignment_count !== 0
        || coverageResult.gap_count !== 0
      )
    )
    || (
      coverageResult.state !== "missing"
      && (
        coverageResult.head === null
        || coverageResult.blueprint_binding === null
        || coverageResult.beat_count < 1
        || coverageResult.selected_assignment_count + coverageResult.gap_count
          !== coverageResult.beat_count
      )
    )
    || (
      (coverageResult.state === "current" || coverageResult.state === "stale_evidence")
      && !coverageBindingMatchesCurrent
    )
    || (
      coverageResult.state === "stale_blueprint"
      && coverageBindingMatchesCurrent
    )
  ) fail("workspace.coverage.consistency");
  const timelineResult = result.timeline;
  const hasTimeline = timelineResult.head !== null;
  const timelineBlueprintBindingMatchesCurrent = (
    timelineResult.blueprint_binding !== null
    && timelineResult.blueprint_binding.revision === current.revision
    && timelineResult.blueprint_binding.content_sha256 === current.content_sha256
    && timelineResult.blueprint_binding.semantic_sha256 === current.semantic_sha256
    && timelineResult.blueprint_binding.operation_id === current.operation_id
  );
  const timelineCoverageBindingMatchesObservedHead = (
    timelineResult.coverage_binding !== null
    && coverageResult.head !== null
    && timelineResult.coverage_binding.revision === coverageResult.head.revision
    && timelineResult.coverage_binding.content_sha256 === coverageResult.head.content_sha256
  );
  const timelineUpstreamStateConsistent = timelineResult.state === "missing"
    ? !hasTimeline
    : timelineResult.state === "current"
      ? timelineBlueprintBindingMatchesCurrent
        && timelineCoverageBindingMatchesObservedHead
        && coverageResult.state === "current"
      : timelineResult.state === "stale_blueprint"
        ? !timelineBlueprintBindingMatchesCurrent
        : timelineResult.state === "stale_coverage"
          ? timelineBlueprintBindingMatchesCurrent
            && (
              !timelineCoverageBindingMatchesObservedHead
              || coverageResult.state !== "current"
            )
          : timelineBlueprintBindingMatchesCurrent
            && timelineCoverageBindingMatchesObservedHead;
  const timelineMaterializationAvailable = (
    !hasTimeline
    && coverageResult.state === "current"
    && coverageResult.gap_count === 0
  );
  const timelineReconciliationAvailable = (
    hasTimeline
    && timelineResult.state !== "current"
    && coverageResult.state === "current"
    && coverageResult.gap_count === 0
  );
  const timelineMutationAvailable = (
    hasTimeline
    && timelineResult.state === "current"
  );
  const noTimelineReason = coverageResult.state === "missing"
    ? "coverage_plan_missing"
    : coverageResult.state === "stale_blueprint"
      ? "coverage_plan_stale_blueprint"
      : coverageResult.state === "stale_evidence"
        ? "coverage_plan_stale_evidence"
        : coverageResult.gap_count > 0
          ? "coverage_not_lowerable"
          : "canonical_timeline_materialization_available";
  const timelineReason = timelineResult.state === "missing"
    ? null
    : TIMELINE_EXECUTABLE_REASON[timelineResult.state];
  if (
    result.capabilities.timeline_materialization !== timelineMaterializationAvailable
    || result.capabilities.timeline_reconciliation !== timelineReconciliationAvailable
    || result.capabilities.timeline_mutation !== timelineMutationAvailable
    || result.capabilities.authoritative_timeline_head !== hasTimeline
    || !timelineUpstreamStateConsistent
    || (
      !hasTimeline
      && (
        timelineResult.state !== "missing"
        || timelineResult.blueprint_binding !== null
        || timelineResult.coverage_binding !== null
        || timelineResult.clip_count !== 0
        || timelineResult.source_binding_count !== 0
        || timelineResult.lifecycle.state !== "not_materialized"
        || result.executable.state !== "not_compiled"
        || result.executable.current_timeline !== null
        || result.executable.reason_code !== noTimelineReason
      )
    )
    || (
      hasTimeline
      && (
        timelineResult.state === "missing"
        || timelineResult.blueprint_binding === null
        || timelineResult.coverage_binding === null
        || timelineResult.clip_count < 1
        || timelineResult.source_binding_count !== timelineResult.clip_count
        || timelineResult.lifecycle.state !== "draft"
        || canonical(timelineResult.head?.blueprint_binding)
          !== canonical(timelineResult.blueprint_binding)
        || canonical(timelineResult.head?.coverage_binding)
          !== canonical(timelineResult.coverage_binding)
        || result.executable.state !== "compiled_draft"
        || canonical(result.executable.current_timeline) !== canonical(timelineResult.head)
        || result.executable.reason_code !== timelineReason
      )
    )
  ) fail("workspace.timeline.consistency");
  const exportResult = result.canonical_export;
  const latestExportActive = exportResult.latest_job !== null
    && isActiveCanonicalExportJobStatus(exportResult.latest_job.status);
  const latestExportRecoveryRequired = exportResult.latest_job?.status === "interrupted";
  const canonicalExportConsistent = latestExportRecoveryRequired
    ? exportResult.state === "blocked"
      && exportResult.reason_code === "export_recovery_required"
      && !result.capabilities.canonical_export
    : timelineResult.state === "missing"
      ? exportResult.state === "blocked"
        && exportResult.reason_code === "timeline_missing"
        && !result.capabilities.canonical_export
      : timelineResult.state !== "current"
        ? exportResult.state === "blocked"
          && exportResult.reason_code === "timeline_stale"
          && !result.capabilities.canonical_export
        : latestExportActive
          ? exportResult.state === "in_progress"
            && exportResult.reason_code === "export_in_progress"
            && !result.capabilities.canonical_export
          : exportResult.state === "available"
            && exportResult.reason_code === "requires_native_confirmation"
            && result.capabilities.canonical_export;
  if (!canonicalExportConsistent) fail("workspace.canonical_export.consistency");
  const legacyResult = result.history.legacy_artifacts;
  if (
    (legacyResult.status === "available") !== (legacyResult.reason_code === null)
    || (
      legacyResult.status === "unavailable"
      && (legacyResult.briefs.length !== 0 || legacyResult.timelines.length !== 0)
    )
  ) fail("workspace.history.legacy_artifacts.status");
  const blueprintHistoryResult = result.history.blueprint;
  const authorityHistoryResult = result.history.decision_authority;
  if (
    blueprintHistoryResult.available_revision_count !== result.current_blueprint.revision
    || blueprintHistoryResult.available_operation_count
      < blueprintHistoryResult.available_revision_count
    || blueprintHistoryResult.operations.length === 0
    || blueprintHistoryResult.operations[0].sequence
      !== blueprintHistoryResult.available_operation_count
    || blueprintHistoryResult.operations[0].result_revision
      !== result.current_blueprint.revision
    || blueprintHistoryResult.operations[0].result_content_sha256
      !== result.current_blueprint.content_sha256
  ) fail("workspace.history.blueprint.coverage");
  uniqueBy(
    blueprintHistoryResult.operations,
    (item) => item.operation_id,
    "workspace.history.blueprint.operations",
  );
  uniqueBy(
    blueprintHistoryResult.operations,
    (item) => item.sequence,
    "workspace.history.blueprint.operations",
  );
  for (let index = 1; index < blueprintHistoryResult.operations.length; index += 1) {
    if (
      blueprintHistoryResult.operations[index - 1].sequence
      !== blueprintHistoryResult.operations[index].sequence + 1
      || blueprintHistoryResult.operations[index - 1].parent_operation_id
        !== blueprintHistoryResult.operations[index].operation_id
    ) fail("workspace.history.blueprint.operations");
  }
  if (
    (authorityHistoryResult.available_event_count === 0)
      !== (authorityHistoryResult.events.length === 0)
    || (
      authorityHistoryResult.events.length > 0
      && authorityHistoryResult.events[authorityHistoryResult.events.length - 1].sequence
        !== authorityHistoryResult.available_event_count
    )
  ) fail("workspace.history.decision_authority.coverage");
  uniqueBy(
    authorityHistoryResult.events,
    (item) => item.event_id,
    "workspace.history.decision_authority.events",
  );
  uniqueBy(
    authorityHistoryResult.events,
    (item) => item.sequence,
    "workspace.history.decision_authority.events",
  );
  for (let index = 1; index < authorityHistoryResult.events.length; index += 1) {
    if (
      authorityHistoryResult.events[index - 1].sequence + 1
      !== authorityHistoryResult.events[index].sequence
    ) fail("workspace.history.decision_authority.events");
  }
  uniqueBy(briefs, (item) => item.revision, "workspace.history.legacy_artifacts.briefs");
  for (let index = 1; index < briefs.length; index += 1) {
    if (briefs[index - 1].revision <= briefs[index].revision) {
      fail("workspace.history.legacy_artifacts.briefs");
    }
  }
  uniqueBy(
    timelines,
    (item) => `${item.timeline_id}:${item.revision}`,
    "workspace.history.legacy_artifacts.timelines",
  );
  for (let index = 1; index < timelines.length; index += 1) {
    const before = timelines[index - 1];
    const after = timelines[index];
    const beforeTime = Date.parse(before.created_at);
    const afterTime = Date.parse(after.created_at);
    if (
      beforeTime < afterTime
      || (
        beforeTime === afterTime
        && (
          before.timeline_id > after.timeline_id
          || (
            before.timeline_id === after.timeline_id
            && before.revision <= after.revision
          )
        )
      )
    ) fail("workspace.history.legacy_artifacts.timelines");
  }
  if (
    blueprintHistoryResult.operations.length > 100
    || blueprintHistoryResult.available_operation_count < blueprintHistoryResult.operations.length
    || blueprintHistoryResult.operations_truncated
      !== (blueprintHistoryResult.available_operation_count > blueprintHistoryResult.operations.length)
    || authorityHistoryResult.events.length > 100
    || authorityHistoryResult.available_event_count < authorityHistoryResult.events.length
    || authorityHistoryResult.events_truncated
      !== (authorityHistoryResult.available_event_count > authorityHistoryResult.events.length)
    || briefs.length + timelines.length > 100
    || result.history.legacy_artifacts.brief_count < briefs.length
    || result.history.legacy_artifacts.briefs_truncated
      !== (result.history.legacy_artifacts.brief_count > briefs.length)
    || result.history.legacy_artifacts.timeline_revision_count < timelines.length
    || result.history.legacy_artifacts.timelines_truncated
      !== (result.history.legacy_artifacts.timeline_revision_count > timelines.length)
  ) fail("workspace.history.coverage");
  return result;
}

export function isBlueprintProjectWorkspace(value: unknown): value is BlueprintProjectWorkspace {
  if (!value || typeof value !== "object") return false;
  const raw = value as Record<string, unknown>;
  return raw.canonical_source === "creative_blueprint"
    && raw.object === "creative.project_workspace";
}

export function isSameBlueprintWorkspaceIdentity(
  expected: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id">,
  candidate: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id">,
): boolean {
  return expected.database_uuid === candidate.database_uuid
    && expected.project_id === candidate.project_id;
}

export function isSameBlueprintWorkspaceHead(
  expected: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id" | "current_blueprint">,
  candidate: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id" | "current_blueprint">,
): boolean {
  return isSameBlueprintWorkspaceIdentity(expected, candidate)
    && expected.current_blueprint.revision === candidate.current_blueprint.revision
    && expected.current_blueprint.content_sha256
      === candidate.current_blueprint.content_sha256;
}

export function canApplyBlueprintWorkspaceRequest(input: {
  captured: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id" | "current_blueprint">;
  current: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id" | "current_blueprint">;
  capturedGeneration: number;
  currentGeneration: number;
}): boolean {
  return input.capturedGeneration === input.currentGeneration
    && isSameBlueprintWorkspaceHead(input.captured, input.current);
}

export type BlueprintWorkspaceInputDisposition =
  | "unchanged"
  | "self_echo"
  | "external";

/**
 * Classify a parent prop update without mistaking a child's own accepted
 * canonical snapshot for an external workspace replacement.
 */
export function classifyBlueprintWorkspaceInput(input: {
  previousIncoming: BlueprintProjectWorkspace;
  incoming: BlueprintProjectWorkspace;
  lastEmitted: BlueprintProjectWorkspace | null;
}): BlueprintWorkspaceInputDisposition {
  if (input.previousIncoming === input.incoming) return "unchanged";
  return input.lastEmitted === input.incoming ? "self_echo" : "external";
}

export function canAdoptBlueprintWorkspaceResponse(input: {
  expected: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id">;
  candidate: Pick<BlueprintProjectWorkspace, "database_uuid" | "project_id">;
  capturedScopeKey: string;
  currentScopeKey: string;
}): boolean {
  return input.capturedScopeKey === input.currentScopeKey
    && isSameBlueprintWorkspaceIdentity(input.expected, input.candidate);
}

export function hasCanonicalWorkspaceMarker(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const raw = value as Record<string, unknown>;
  return raw.canonical_source === "creative_blueprint"
    || Object.prototype.hasOwnProperty.call(raw, "workspace_mode")
    || Object.prototype.hasOwnProperty.call(raw, "current_blueprint")
    || raw.object === "creative.project_workspace";
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    const raw = value as Record<string, unknown>;
    return `{${Object.keys(raw).sort().map((key) => `${JSON.stringify(key)}:${canonical(raw[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function clip(value: string | null, fallback: string): string {
  const normalized = value?.replace(/\s+/g, " ").trim() ?? "";
  if (!normalized) return fallback;
  return normalized.length > 92 ? `${normalized.slice(0, 89)}…` : normalized;
}

function sectionSummary(section: BlueprintSemanticSection, semantic: BlueprintSemantic): string {
  switch (section) {
    case "intent":
      return [clip(semantic.intent.goal, "No goal"), semantic.intent.stance ? `Stance: ${clip(semantic.intent.stance, "")}` : null].filter(Boolean).join(" · ");
    case "script":
      return `${semantic.script.blocks.length} blocks · ${clip(semantic.script.blocks.map((block) => block.text).join(" "), "No script")}`;
    case "direction":
      return [semantic.direction.theme, semantic.direction.emotion, semantic.direction.tone, semantic.direction.pace].filter(Boolean).join(" · ") || "No direction";
    case "output":
      return `${semantic.output.aspect_ratio ?? "Open aspect"} · ${semantic.output.duration_target_ms === null ? "Open duration" : `${Math.round(semantic.output.duration_target_ms / 1000)}s`}`;
    case "constraints":
      return `${semantic.constraints.must_include.length} include · ${semantic.constraints.must_exclude.length} exclude`;
    case "material_hints":
      return `${semantic.material_hints.length} grounded material hints`;
    case "reference_refs":
      return `${semantic.reference_refs.length} references`;
    case "technique_refs":
      return `${semantic.technique_refs.length} technique cards`;
    case "bindings":
      return `${semantic.bindings.creator_context ? "Creator context pinned" : "No creator context"} · ${semantic.bindings.wiki_generation ? "Wiki pinned" : "No Wiki pin"}`;
    case "assumptions":
      return `${semantic.assumptions.length} assumptions`;
    case "missing_evidence":
      return `${semantic.missing_evidence.length} evidence gaps`;
    case "open_decisions":
      return `${semantic.open_decisions.length} open decisions`;
  }
}

export function compareBlueprintSemantics(
  before: BlueprintSemantic,
  after: BlueprintSemantic,
): BlueprintSectionDiff[] {
  return BLUEPRINT_SEMANTIC_SECTIONS.map((section) => ({
    section,
    changed: canonical(before[section]) !== canonical(after[section]),
    before_summary: sectionSummary(section, before),
    after_summary: sectionSummary(section, after),
  }));
}
