import type {
  CanonicalCoveragePlan,
  CoverageAlternative,
  CoverageAssignment,
  CoverageBeat,
  CoverageEvidenceManifestItem,
  CoverageEvidenceProof,
  CoverageFreshnessReason,
  CoverageGap,
  CoverageMaterializeCommandResult,
  CoverageNeed,
  CoverageOperation,
  CoverageOperationResult,
  CoveragePlanWorkspace,
  CoverageResultHead,
} from "./coverageTypes";
import type {
  BlueprintCoverageSummary,
  BlueprintProjectWorkspace,
  BlueprintRevisionRef,
} from "./workspaceTypes";

const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const DATABASE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const OPAQUE_ASSET_ID = /^(?:asset|img)_[0-9a-f]{24}$/;
const OPAQUE_VIDEO_ASSET_ID = /^asset_[0-9a-f]{24}$/;
const OPAQUE_ANALYSIS_RUN_ID = /^arun_[0-9a-f]{32}$/;
const OPAQUE_SPAN_ID = /^seg_([0-9a-f]{24})_([1-9][0-9]{0,6})_(?:0|[1-9][0-9]{0,6})$/;
const STABLE_BEAT_ID = /^beat_[0-9a-f]{24}$/;
const STABLE_NEED_ID = /^need_[0-9a-f]{24}$/;
const STABLE_ASSIGNMENT_ID = /^assign_[0-9a-f]{24}$/;
const MAX_BEATS = 256;
const MAX_EVIDENCE = 512;
const MAX_OPERATIONS = 50;
const IMAGE_ANALYSIS_FIELDS = [
  "analysis_run_id", "analysis_revision", "analysis_content_sha256", "source_binding_sha256",
];

function fail(path: string): never {
  throw new Error(`Invalid canonical Coverage workspace at ${path}.`);
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fail(path);
  return value as Record<string, unknown>;
}

function closedRecord(
  value: unknown,
  expected: readonly string[],
  path: string,
): Record<string, unknown> {
  const raw = record(value, path);
  const actual = Object.keys(raw).sort();
  const frozen = [...expected].sort();
  if (
    actual.length !== frozen.length
    || actual.some((key, index) => key !== frozen[index])
  ) return fail(path);
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

function integer(
  value: unknown,
  path: string,
  minimum = 0,
  maximum = 1_800_000,
): number {
  if (!Number.isInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    return fail(path);
  }
  return Number(value);
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") return fail(path);
  return value;
}

function identifier(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (!IDENTIFIER.test(normalized)) return fail(path);
  return normalized;
}

function nullableIdentifier(value: unknown, path: string): string | null {
  return value === null ? null : identifier(value, path);
}

function digest(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (!SHA256.test(normalized)) return fail(path);
  return normalized;
}

function timestamp(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (Number.isNaN(Date.parse(normalized))) return fail(path);
  return normalized;
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

function sameRevisionRef(
  left: BlueprintRevisionRef | null,
  right: BlueprintRevisionRef | null,
): boolean {
  return left === null || right === null
    ? left === right
    : left.revision === right.revision && left.content_sha256 === right.content_sha256;
}

function evidenceReference(value: unknown, path: string): {
  value: string;
  kind: "asset" | "span";
  id: string;
} {
  const normalized = string(value, path);
  const assetPrefix = "memolens://evidence/asset/";
  const spanPrefix = "memolens://evidence/span/";
  if (normalized.startsWith(assetPrefix)) {
    const id = normalized.slice(assetPrefix.length);
    if (!OPAQUE_ASSET_ID.test(id)) return fail(path);
    return { value: normalized, kind: "asset", id };
  }
  if (normalized.startsWith(spanPrefix)) {
    const id = normalized.slice(spanPrefix.length);
    if (!OPAQUE_SPAN_ID.test(id)) return fail(path);
    return { value: normalized, kind: "span", id };
  }
  return fail(path);
}

function isContentAddressed(assetId: string, assetDigest: string): boolean {
  return assetId === `${assetId.split("_", 1)[0]}_${assetDigest.slice(0, 24)}`;
}

function evidenceProof(
  value: unknown,
  evidenceRef: ReturnType<typeof evidenceReference>,
  path: string,
): CoverageEvidenceProof {
  const candidate = record(value, path);
  const kind = candidate.kind;
  if (kind === "asset") {
    const hasAnalysis = IMAGE_ANALYSIS_FIELDS.some((field) => Object.hasOwn(candidate, field));
    const raw = closedRecord(value, [
      "kind", "asset_id", "asset_sha256", ...(hasAnalysis ? IMAGE_ANALYSIS_FIELDS : []),
    ], path);
    const assetId = identifier(raw.asset_id, `${path}.asset_id`);
    const assetDigest = digest(raw.asset_sha256, `${path}.asset_sha256`);
    if (
      evidenceRef.kind !== "asset"
      || evidenceRef.id !== assetId
      || !OPAQUE_ASSET_ID.test(assetId)
      || !isContentAddressed(assetId, assetDigest)
    ) return fail(path);
    const base = {
      kind: exactString(raw.kind, "asset", `${path}.kind`),
      asset_id: assetId,
      asset_sha256: assetDigest,
    };
    if (!hasAnalysis) return base;
    const analysisRunId = identifier(raw.analysis_run_id, `${path}.analysis_run_id`);
    if (!OPAQUE_ANALYSIS_RUN_ID.test(analysisRunId)) return fail(`${path}.analysis_run_id`);
    return {
      ...base,
      analysis_run_id: analysisRunId,
      analysis_revision: integer(raw.analysis_revision, `${path}.analysis_revision`, 1),
      analysis_content_sha256: digest(raw.analysis_content_sha256, `${path}.analysis_content_sha256`),
      source_binding_sha256: digest(raw.source_binding_sha256, `${path}.source_binding_sha256`),
    };
  }
  const raw = closedRecord(
    value,
    [
      "kind",
      "span_id",
      "asset_id",
      "asset_sha256",
      "analysis_run_id",
      "analysis_revision",
      "input_asset_sha256",
      "start_ms",
      "end_ms",
    ],
    path,
  );
  const spanId = identifier(raw.span_id, `${path}.span_id`);
  const spanMatch = OPAQUE_SPAN_ID.exec(spanId);
  const assetId = identifier(raw.asset_id, `${path}.asset_id`);
  const analysisRunId = identifier(raw.analysis_run_id, `${path}.analysis_run_id`);
  const analysisRevision = integer(raw.analysis_revision, `${path}.analysis_revision`, 1);
  const assetDigest = digest(raw.asset_sha256, `${path}.asset_sha256`);
  const inputDigest = digest(raw.input_asset_sha256, `${path}.input_asset_sha256`);
  const start = integer(raw.start_ms, `${path}.start_ms`, 0, Number.MAX_SAFE_INTEGER);
  const end = integer(raw.end_ms, `${path}.end_ms`, 1, Number.MAX_SAFE_INTEGER);
  if (
    evidenceRef.kind !== "span"
    || evidenceRef.id !== spanId
    || spanMatch === null
    || !OPAQUE_VIDEO_ASSET_ID.test(assetId)
    || spanMatch[1] !== assetId.slice("asset_".length)
    || Number(spanMatch[2]) !== analysisRevision
    || !OPAQUE_ANALYSIS_RUN_ID.test(analysisRunId)
    || assetDigest !== inputDigest
    || !isContentAddressed(assetId, assetDigest)
    || end <= start
  ) return fail(path);
  return {
    kind: exactString(raw.kind, "span", `${path}.kind`),
    span_id: spanId,
    asset_id: assetId,
    asset_sha256: assetDigest,
    analysis_run_id: analysisRunId,
    analysis_revision: analysisRevision,
    input_asset_sha256: inputDigest,
    start_ms: start,
    end_ms: end,
  };
}

function manifestItem(value: unknown, path: string): CoverageEvidenceManifestItem {
  const raw = closedRecord(
    value,
    ["evidence_ref", "status", "proof_sha256", "proof"],
    path,
  );
  const reference = evidenceReference(raw.evidence_ref, `${path}.evidence_ref`);
  const status = oneOf(raw.status, ["verified", "unresolved"] as const, `${path}.status`);
  if (status === "unresolved") {
    if (raw.proof_sha256 !== null || raw.proof !== null) return fail(path);
    return {
      evidence_ref: reference.value,
      status,
      proof_sha256: null,
      proof: null,
    };
  }
  if (raw.proof === null) return fail(`${path}.proof`);
  return {
    evidence_ref: reference.value,
    status,
    proof_sha256: digest(raw.proof_sha256, `${path}.proof_sha256`),
    proof: evidenceProof(raw.proof, reference, `${path}.proof`),
  };
}

function coverageNeed(value: unknown, path: string): CoverageNeed {
  const raw = closedRecord(value, ["need_id", "kind", "priority", "allow_empty"], path);
  const needId = identifier(raw.need_id, `${path}.need_id`);
  if (!STABLE_NEED_ID.test(needId)) return fail(`${path}.need_id`);
  return {
    need_id: needId,
    kind: exactString(raw.kind, "depiction_unspecified", `${path}.kind`),
    priority: exactString(raw.priority, "should", `${path}.priority`),
    allow_empty: raw.allow_empty === true ? true : fail(`${path}.allow_empty`),
  };
}

function coverageAssignment(
  value: unknown,
  path: string,
  options: {
    alternative: false;
    needId: string;
    beatDurationMs: number;
    manifest: ReadonlyMap<string, CoverageEvidenceManifestItem>;
  },
): CoverageAssignment;
function coverageAssignment(
  value: unknown,
  path: string,
  options: {
    alternative: true;
    needId: string;
    beatDurationMs: number;
    manifest: ReadonlyMap<string, CoverageEvidenceManifestItem>;
  },
): CoverageAlternative;
function coverageAssignment(
  value: unknown,
  path: string,
  options: {
    alternative: boolean;
    needId: string;
    beatDurationMs: number;
    manifest: ReadonlyMap<string, CoverageEvidenceManifestItem>;
  },
): CoverageAssignment | CoverageAlternative {
  const fields = [
    "assignment_id",
    "need_id",
    "hint_id",
    "evidence_ref",
    "match_type",
    "timeline_duration_ms",
    "reason_sha256",
    "locked",
  ];
  if (options.alternative) fields.push("not_selected_reason");
  const raw = closedRecord(value, fields, path);
  const evidenceRef = evidenceReference(raw.evidence_ref, `${path}.evidence_ref`).value;
  const needId = identifier(raw.need_id, `${path}.need_id`);
  const duration = integer(raw.timeline_duration_ms, `${path}.timeline_duration_ms`, 1);
  const assignmentId = identifier(raw.assignment_id, `${path}.assignment_id`);
  if (needId !== options.needId || duration !== options.beatDurationMs) return fail(path);
  if (!STABLE_ASSIGNMENT_ID.test(assignmentId)) return fail(`${path}.assignment_id`);
  const manifestItemForAssignment = options.manifest.get(evidenceRef);
  if (!manifestItemForAssignment) return fail(`${path}.evidence_ref`);
  const base: CoverageAssignment = {
    assignment_id: assignmentId,
    need_id: needId,
    hint_id: identifier(raw.hint_id, `${path}.hint_id`),
    evidence_ref: evidenceRef,
    match_type: exactString(raw.match_type, "depiction_unspecified", `${path}.match_type`),
    timeline_duration_ms: duration,
    reason_sha256: digest(raw.reason_sha256, `${path}.reason_sha256`),
    locked: raw.locked === false ? false : fail(`${path}.locked`),
  };
  if (!options.alternative) {
    if (manifestItemForAssignment.status !== "verified") return fail(`${path}.evidence_ref`);
    if (
      manifestItemForAssignment.proof?.kind === "span"
      && manifestItemForAssignment.proof.end_ms - manifestItemForAssignment.proof.start_ms
        < duration
    ) return fail(`${path}.evidence_ref`);
    return base;
  }
  const notSelectedReason = oneOf(
    raw.not_selected_reason,
    [
      "evidence_unresolved",
      "duplicate_evidence_reserved",
      "baseline_first_verified_selected",
      "evidence_span_too_short",
    ] as const,
    `${path}.not_selected_reason`,
  );
  const proofIsShortSpan = manifestItemForAssignment.proof?.kind === "span"
    && manifestItemForAssignment.proof.end_ms - manifestItemForAssignment.proof.start_ms
      < duration;
  if (
    (notSelectedReason === "evidence_unresolved")
      !== (manifestItemForAssignment.status === "unresolved")
    || (notSelectedReason === "evidence_span_too_short") !== proofIsShortSpan
    || (
      notSelectedReason !== "evidence_unresolved"
      && manifestItemForAssignment.status !== "verified"
    )
  ) return fail(`${path}.not_selected_reason`);
  return {
    ...base,
    not_selected_reason: notSelectedReason,
  };
}

function coverageBeat(
  value: unknown,
  index: number,
  expectedStartMs: number,
  manifest: ReadonlyMap<string, CoverageEvidenceManifestItem>,
  reservedEvidence: Set<string>,
): CoverageBeat {
  const path = `coverage.coverage_plan.beats[${index}]`;
  const raw = closedRecord(
    value,
    [
      "beat_id",
      "script_block_id",
      "ordinal",
      "text_sha256",
      "timing",
      "needs",
      "selected_assignments",
      "alternatives",
      "gap",
    ],
    path,
  );
  if (integer(raw.ordinal, `${path}.ordinal`) !== index) return fail(`${path}.ordinal`);
  const timing = closedRecord(raw.timing, ["basis", "start_ms", "end_ms"], `${path}.timing`);
  const start = integer(timing.start_ms, `${path}.timing.start_ms`);
  const end = integer(timing.end_ms, `${path}.timing.end_ms`, 1);
  if (start !== expectedStartMs || end <= start) return fail(`${path}.timing`);
  const needs = array(raw.needs, `${path}.needs`);
  if (needs.length !== 1) return fail(`${path}.needs`);
  const need = coverageNeed(needs[0], `${path}.needs[0]`);
  const selected = array(raw.selected_assignments, `${path}.selected_assignments`).map(
    (item, assignmentIndex) => coverageAssignment(
      item,
      `${path}.selected_assignments[${assignmentIndex}]`,
      {
        alternative: false,
        needId: need.need_id,
        beatDurationMs: end - start,
        manifest,
      },
    ),
  );
  if (selected.length > 1) return fail(`${path}.selected_assignments`);
  const alternatives = array(raw.alternatives, `${path}.alternatives`).map(
    (item, assignmentIndex) => coverageAssignment(
      item,
      `${path}.alternatives[${assignmentIndex}]`,
      {
        alternative: true,
        needId: need.need_id,
        beatDurationMs: end - start,
        manifest,
      },
    ),
  );
  const assignmentIds = [...selected, ...alternatives].map((item) => item.assignment_id);
  if (new Set(assignmentIds).size !== assignmentIds.length) return fail(`${path}.assignments`);
  if (selected.some((assignment) => reservedEvidence.has(assignment.evidence_ref))) {
    return fail(`${path}.selected_assignments`);
  }
  alternatives.forEach((alternative, alternativeIndex) => {
    const evidence = manifest.get(alternative.evidence_ref);
    const proofIsShortSpan = evidence?.proof?.kind === "span"
      && evidence.proof.end_ms - evidence.proof.start_ms
        < alternative.timeline_duration_ms;
    const expectedReason = evidence?.status !== "verified"
      ? "evidence_unresolved"
      : proofIsShortSpan
        ? "evidence_span_too_short"
        : reservedEvidence.has(alternative.evidence_ref)
          || selected.some((assignment) => assignment.evidence_ref === alternative.evidence_ref)
          ? "duplicate_evidence_reserved"
          : selected.length > 0
            ? "baseline_first_verified_selected"
            : null;
    if (
      expectedReason === null
      || alternative.not_selected_reason !== expectedReason
    ) return fail(`${path}.alternatives[${alternativeIndex}].not_selected_reason`);
  });
  const gap: CoverageGap | null = raw.gap === null
    ? null
    : (() => {
      const gapRaw = closedRecord(raw.gap, ["code", "blocking"], `${path}.gap`);
      return {
        code: oneOf(
          gapRaw.code,
          [
            "no_material_hint",
            "evidence_unresolved",
            "duplicate_evidence_reserved",
            "evidence_span_too_short",
          ] as const,
          `${path}.gap.code`,
        ),
        blocking: gapRaw.blocking === false ? false : fail(`${path}.gap.blocking`),
      };
    })();
  if ((selected.length === 0) !== (gap !== null)) return fail(`${path}.gap`);
  if (selected.length === 0 && gap !== null) {
    const reasons = new Set(alternatives.map((item) => item.not_selected_reason));
    const expectedGap = reasons.has("evidence_unresolved")
      ? "evidence_unresolved"
      : reasons.has("evidence_span_too_short")
        ? "evidence_span_too_short"
        : reasons.has("duplicate_evidence_reserved")
          ? "duplicate_evidence_reserved"
          : "no_material_hint";
    if (
      gap.code !== expectedGap
      || reasons.has("baseline_first_verified_selected")
    ) return fail(`${path}.gap`);
  }
  selected.forEach((assignment) => reservedEvidence.add(assignment.evidence_ref));
  const beatId = identifier(raw.beat_id, `${path}.beat_id`);
  if (!STABLE_BEAT_ID.test(beatId)) return fail(`${path}.beat_id`);
  return {
    beat_id: beatId,
    script_block_id: identifier(raw.script_block_id, `${path}.script_block_id`),
    ordinal: index,
    text_sha256: digest(raw.text_sha256, `${path}.text_sha256`),
    timing: {
      basis: exactString(timing.basis, "estimated_text", `${path}.timing.basis`),
      start_ms: start,
      end_ms: end,
    },
    needs: [need],
    selected_assignments: selected,
    alternatives,
    gap,
  };
}

function coveragePlan(value: unknown): CanonicalCoveragePlan {
  const raw = closedRecord(
    value,
    [
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
    ],
    "coverage.coverage_plan",
  );
  const revision = integer(raw.revision, "coverage.coverage_plan.revision", 1);
  const parent = nullableRevisionRef(raw.parent, "coverage.coverage_plan.parent");
  if ((revision === 1) !== (parent === null) || (parent !== null && parent.revision !== revision - 1)) {
    return fail("coverage.coverage_plan.parent");
  }
  const bindingRaw = closedRecord(
    raw.blueprint_binding,
    ["revision", "content_sha256", "semantic_sha256"],
    "coverage.coverage_plan.blueprint_binding",
  );
  const compiler = closedRecord(
    raw.compiler,
    ["id", "timing_capability", "semantic_matching", "global_optimization"],
    "coverage.coverage_plan.compiler",
  );
  const output = closedRecord(
    raw.output_binding,
    ["duration_target_ms", "aspect_ratio"],
    "coverage.coverage_plan.output_binding",
  );
  const rawManifest = array(raw.evidence_manifest, "coverage.coverage_plan.evidence_manifest");
  if (rawManifest.length > MAX_EVIDENCE) return fail("coverage.coverage_plan.evidence_manifest");
  const manifest = rawManifest.map((item, index) => (
    manifestItem(item, `coverage.coverage_plan.evidence_manifest[${index}]`)
  ));
  const refs = manifest.map((item) => item.evidence_ref);
  if (
    new Set(refs).size !== refs.length
    || refs.some((item, index) => index > 0 && refs[index - 1] >= item)
  ) return fail("coverage.coverage_plan.evidence_manifest");
  const manifestByRef = new Map(manifest.map((item) => [item.evidence_ref, item]));
  const rawBeats = array(raw.beats, "coverage.coverage_plan.beats");
  if (rawBeats.length < 1 || rawBeats.length > MAX_BEATS) {
    return fail("coverage.coverage_plan.beats");
  }
  const beats: CoverageBeat[] = [];
  const reservedEvidence = new Set<string>();
  let cursor = 0;
  rawBeats.forEach((item, index) => {
    const beat = coverageBeat(item, index, cursor, manifestByRef, reservedEvidence);
    beats.push(beat);
    cursor = beat.timing.end_ms;
  });
  const beatIds = beats.map((item) => item.beat_id);
  const blockIds = beats.map((item) => item.script_block_id);
  const selectedRefs = beats.flatMap((beat) => (
    beat.selected_assignments.map((assignment) => assignment.evidence_ref)
  ));
  const assignedRefs = new Set(beats.flatMap((beat) => (
    [...beat.selected_assignments, ...beat.alternatives].map((assignment) => assignment.evidence_ref)
  )));
  if (
    new Set(beatIds).size !== beatIds.length
    || new Set(blockIds).size !== blockIds.length
    || new Set(selectedRefs).size !== selectedRefs.length
    || assignedRefs.size !== manifestByRef.size
    || refs.some((ref) => !assignedRefs.has(ref))
  ) return fail("coverage.coverage_plan.beats");
  const targetDuration = integer(
    output.duration_target_ms,
    "coverage.coverage_plan.output_binding.duration_target_ms",
    1,
  );
  if (cursor !== targetDuration) return fail("coverage.coverage_plan.output_binding.duration_target_ms");
  return {
    object: exactString(raw.object, "memolens.coverage_plan", "coverage.coverage_plan.object"),
    schema_version: exactString(raw.schema_version, "1", "coverage.coverage_plan.schema_version"),
    project_id: identifier(raw.project_id, "coverage.coverage_plan.project_id"),
    revision,
    parent,
    blueprint_binding: {
      revision: integer(bindingRaw.revision, "coverage.coverage_plan.blueprint_binding.revision", 1),
      content_sha256: digest(bindingRaw.content_sha256, "coverage.coverage_plan.blueprint_binding.content_sha256"),
      semantic_sha256: digest(bindingRaw.semantic_sha256, "coverage.coverage_plan.blueprint_binding.semantic_sha256"),
    },
    compiler: {
      id: exactString(compiler.id, "memolens.coverage-baseline/v1", "coverage.coverage_plan.compiler.id"),
      timing_capability: exactString(compiler.timing_capability, "estimated_text", "coverage.coverage_plan.compiler.timing_capability"),
      semantic_matching: compiler.semantic_matching === false ? false : fail("coverage.coverage_plan.compiler.semantic_matching"),
      global_optimization: compiler.global_optimization === false ? false : fail("coverage.coverage_plan.compiler.global_optimization"),
    },
    output_binding: {
      duration_target_ms: targetDuration,
      aspect_ratio: output.aspect_ratio === null
        ? null
        : oneOf(output.aspect_ratio, ["16:9", "9:16", "1:1", "4:5"] as const, "coverage.coverage_plan.output_binding.aspect_ratio"),
    },
    beats,
    evidence_manifest: manifest,
  };
}

function resultHead(value: unknown, path: string): CoverageResultHead {
  const raw = closedRecord(
    value,
    [
      "revision",
      "content_sha256",
      "blueprint_revision",
      "blueprint_content_sha256",
      "blueprint_semantic_sha256",
      "operation_id",
    ],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
    blueprint_revision: integer(raw.blueprint_revision, `${path}.blueprint_revision`, 1),
    blueprint_content_sha256: digest(raw.blueprint_content_sha256, `${path}.blueprint_content_sha256`),
    blueprint_semantic_sha256: digest(raw.blueprint_semantic_sha256, `${path}.blueprint_semantic_sha256`),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function operationResult(value: unknown, path: string): CoverageOperationResult {
  const raw = closedRecord(value, ["kind", "result_head"], path);
  return {
    kind: exactString(raw.kind, "revision_created", `${path}.kind`),
    result_head: resultHead(raw.result_head, `${path}.result_head`),
  };
}

function coverageOperation(value: unknown, index: number): CoverageOperation {
  const path = `coverage.operations[${index}]`;
  const raw = closedRecord(
    value,
    [
      "id",
      "project_id",
      "sequence",
      "parent_operation_id",
      "command_type",
      "command_version",
      "effect_class",
      "expected_blueprint_revision",
      "expected_blueprint_content_sha256",
      "expected_plan_head",
      "request_sha256",
      "result_revision",
      "result_content_sha256",
      "result",
      "created_at",
    ],
    path,
  );
  const id = identifier(raw.id, `${path}.id`);
  const sequence = integer(raw.sequence, `${path}.sequence`, 1);
  const resultRevision = integer(raw.result_revision, `${path}.result_revision`, 1);
  const resultDigest = digest(raw.result_content_sha256, `${path}.result_content_sha256`);
  const result = operationResult(raw.result, `${path}.result`);
  if (
    result.result_head.operation_id !== id
    || result.result_head.revision !== resultRevision
    || result.result_head.content_sha256 !== resultDigest
    || result.result_head.blueprint_revision !== raw.expected_blueprint_revision
    || result.result_head.blueprint_content_sha256 !== raw.expected_blueprint_content_sha256
  ) return fail(`${path}.result`);
  const parentOperation = nullableIdentifier(raw.parent_operation_id, `${path}.parent_operation_id`);
  if ((sequence === 1) !== (parentOperation === null)) return fail(`${path}.parent_operation_id`);
  return {
    id,
    project_id: identifier(raw.project_id, `${path}.project_id`),
    sequence,
    parent_operation_id: parentOperation,
    command_type: exactString(raw.command_type, "coverage.materialize_baseline", `${path}.command_type`),
    command_version: exactString(raw.command_version, "1", `${path}.command_version`),
    effect_class: exactString(raw.effect_class, "reversible_project_write", `${path}.effect_class`),
    expected_blueprint_revision: integer(raw.expected_blueprint_revision, `${path}.expected_blueprint_revision`, 1),
    expected_blueprint_content_sha256: digest(raw.expected_blueprint_content_sha256, `${path}.expected_blueprint_content_sha256`),
    expected_plan_head: nullableRevisionRef(raw.expected_plan_head, `${path}.expected_plan_head`),
    request_sha256: digest(raw.request_sha256, `${path}.request_sha256`),
    result_revision: resultRevision,
    result_content_sha256: resultDigest,
    result,
    created_at: timestamp(raw.created_at, `${path}.created_at`),
  };
}

function normalizeFreshness(value: unknown): CoveragePlanWorkspace["freshness"] {
  const raw = closedRecord(value, ["state", "reasons"], "coverage.freshness");
  const state = oneOf(
    raw.state,
    ["missing", "current", "stale_blueprint", "stale_evidence"] as const,
    "coverage.freshness.state",
  );
  const reasons = array(raw.reasons, "coverage.freshness.reasons").map((reason, index) => (
    oneOf(
      reason,
      ["blueprint_head_changed", "evidence_head_changed"] as const,
      `coverage.freshness.reasons[${index}]`,
    )
  ));
  if (new Set(reasons).size !== reasons.length) return fail("coverage.freshness.reasons");
  const expected: CoverageFreshnessReason[][] = state === "missing" || state === "current"
    ? [[]]
    : state === "stale_evidence"
      ? [["evidence_head_changed"]]
      : [["blueprint_head_changed"], ["blueprint_head_changed", "evidence_head_changed"]];
  if (!expected.some((candidate) => (
    candidate.length === reasons.length
    && candidate.every((reason, index) => reason === reasons[index])
  ))) return fail("coverage.freshness");
  return { state, reasons };
}

export function normalizeCoverageWorkspace(value: unknown): CoveragePlanWorkspace {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "database_uuid",
      "project_id",
      "head",
      "selected_revision",
      "freshness",
      "coverage_plan",
      "operations",
    ],
    "coverage",
  );
  const databaseUuid = string(raw.database_uuid, "coverage.database_uuid");
  if (!DATABASE_UUID.test(databaseUuid)) return fail("coverage.database_uuid");
  const head = nullableRevisionRef(raw.head, "coverage.head");
  const selected = raw.selected_revision === null
    ? null
    : (() => {
      const row = closedRecord(
        raw.selected_revision,
        [
          "revision",
          "content_sha256",
          "evidence_manifest_sha256",
          "operation_id",
          "is_head",
        ],
        "coverage.selected_revision",
      );
      return {
        revision: integer(row.revision, "coverage.selected_revision.revision", 1),
        content_sha256: digest(row.content_sha256, "coverage.selected_revision.content_sha256"),
        evidence_manifest_sha256: digest(
          row.evidence_manifest_sha256,
          "coverage.selected_revision.evidence_manifest_sha256",
        ),
        operation_id: identifier(
          row.operation_id,
          "coverage.selected_revision.operation_id",
        ),
        is_head: boolean(row.is_head, "coverage.selected_revision.is_head"),
      };
    })();
  const freshness = normalizeFreshness(raw.freshness);
  const plan = raw.coverage_plan === null ? null : coveragePlan(raw.coverage_plan);
  const rawOperations = array(raw.operations, "coverage.operations");
  if (rawOperations.length > MAX_OPERATIONS) return fail("coverage.operations");
  const operations = rawOperations.map(coverageOperation);
  const projectId = identifier(raw.project_id, "coverage.project_id");
  if (
    (plan === null) !== (selected === null)
    || (plan === null) !== (head === null)
    || (plan === null) !== (freshness.state === "missing")
    || (plan === null) !== (operations.length === 0)
  ) return fail("coverage.consistency");
  if (plan !== null && selected !== null && head !== null) {
    const relatedSelectedOperations = operations.filter((operation) => (
      operation.result_revision === selected.revision
      || operation.result_content_sha256 === selected.content_sha256
      || operation.id === selected.operation_id
    ));
    const selectedOperationMatches = relatedSelectedOperations.length === 1
      && relatedSelectedOperations[0].result_revision === selected.revision
      && relatedSelectedOperations[0].result_content_sha256 === selected.content_sha256
      && relatedSelectedOperations[0].id === selected.operation_id;
    const selectedFallsBeforeBoundedWindow = !selected.is_head
      && operations.length === MAX_OPERATIONS
      && selected.revision < operations[operations.length - 1].result_revision
      && relatedSelectedOperations.length === 0;
    if (
      plan.project_id !== projectId
      || plan.revision !== selected.revision
      || selected.is_head !== sameRevisionRef(selected, head)
      || operations.length === 0
      || operations[0].result_revision !== head.revision
      || operations[0].result_content_sha256 !== head.content_sha256
      || (!selectedOperationMatches && !selectedFallsBeforeBoundedWindow)
    ) return fail("coverage.consistency");
  }
  const operationIds = operations.map((operation) => operation.id);
  const operationSequences = operations.map((operation) => operation.sequence);
  if (
    new Set(operationIds).size !== operationIds.length
    || new Set(operationSequences).size !== operationSequences.length
    || operations.some((operation) => operation.project_id !== projectId)
  ) return fail("coverage.operations");
  for (let index = 1; index < operations.length; index += 1) {
    if (
      operations[index - 1].sequence !== operations[index].sequence + 1
      || operations[index - 1].parent_operation_id !== operations[index].id
    ) return fail("coverage.operations");
  }
  return {
    object: exactString(raw.object, "coverage_plan.workspace", "coverage.object"),
    schema_version: exactString(raw.schema_version, "1", "coverage.schema_version"),
    database_uuid: databaseUuid,
    project_id: projectId,
    head,
    selected_revision: selected,
    freshness,
    coverage_plan: plan,
    operations,
  };
}

export function normalizeCoverageMaterializeCommandResult(
  value: unknown,
): CoverageMaterializeCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "command_type", "operation_id", "result"],
    "coverage_command",
  );
  const operationId = identifier(raw.operation_id, "coverage_command.operation_id");
  const result = operationResult(raw.result, "coverage_command.result");
  if (result.result_head.operation_id !== operationId) return fail("coverage_command.result");
  return {
    object: exactString(raw.object, "coverage_plan.command_result", "coverage_command.object"),
    schema_version: exactString(raw.schema_version, "1", "coverage_command.schema_version"),
    project_id: identifier(raw.project_id, "coverage_command.project_id"),
    command_type: exactString(raw.command_type, "coverage.materialize_baseline", "coverage_command.command_type"),
    operation_id: operationId,
    result,
  };
}

function planSummary(plan: CanonicalCoveragePlan): Pick<
  BlueprintCoverageSummary,
  "beat_count" | "selected_assignment_count" | "gap_count"
> {
  return {
    beat_count: plan.beats.length,
    selected_assignment_count: plan.beats.reduce(
      (total, beat) => total + beat.selected_assignments.length,
      0,
    ),
    gap_count: plan.beats.filter((beat) => beat.gap !== null).length,
  };
}

export function isCoverageWorkspaceProjectionForBlueprint(
  workspace: Pick<
    BlueprintProjectWorkspace,
    "database_uuid" | "project_id" | "coverage" | "current_blueprint"
  >,
  coverage: CoveragePlanWorkspace,
): boolean {
  if (
    coverage.database_uuid !== workspace.database_uuid
    || coverage.project_id !== workspace.project_id
    || coverage.freshness.state !== workspace.coverage.state
    || !sameRevisionRef(coverage.head, workspace.coverage.head)
  ) return false;
  if (workspace.coverage.state === "missing") {
    return coverage.selected_revision === null && coverage.coverage_plan === null;
  }
  const plan = coverage.coverage_plan;
  const selected = coverage.selected_revision;
  const binding = workspace.coverage.blueprint_binding;
  if (!plan || !selected || !selected.is_head || !binding) return false;
  const planBindsDisplayedBlueprint = (
    plan.blueprint_binding.revision === workspace.current_blueprint.revision
    && plan.blueprint_binding.content_sha256 === workspace.current_blueprint.content_sha256
    && plan.blueprint_binding.semantic_sha256 === workspace.current_blueprint.semantic_sha256
  );
  if (planBindsDisplayedBlueprint) {
    const blockIds = workspace.current_blueprint.blueprint.semantic.script.blocks.map(
      (block) => block.block_id,
    );
    if (
      blockIds.length !== plan.beats.length
      || blockIds.some((blockId, index) => plan.beats[index].script_block_id !== blockId)
    ) return false;
  }
  const summary = planSummary(plan);
  return (
    selected.revision === workspace.coverage.head?.revision
    && selected.content_sha256 === workspace.coverage.head?.content_sha256
    && plan.blueprint_binding.revision === binding.revision
    && plan.blueprint_binding.content_sha256 === binding.content_sha256
    && plan.blueprint_binding.semantic_sha256 === binding.semantic_sha256
    && summary.beat_count === workspace.coverage.beat_count
    && summary.selected_assignment_count === workspace.coverage.selected_assignment_count
    && summary.gap_count === workspace.coverage.gap_count
  );
}
