import type { CreativeAssetMatch, ResidualBinding } from "./types";

const RESIDUAL_FIELDS = new Set([
  "object",
  "contract_version",
  "residual_id",
  "parent_segment_id",
  "asset_id",
  "asset_sha256",
  "asset_source_id",
  "source_binding_sha256",
  "analysis_run_id",
  "analysis_revision",
  "input_asset_sha256",
  "parent_start_ms",
  "parent_end_ms",
  "source_in_ms",
  "source_out_ms",
  "usage_revision",
  "parent_usage_projection_sha256",
]);
const SHA256 = /^[0-9a-f]{64}$/;
const ASSET_ID = /^asset_[0-9a-f]{24}$/;
const SOURCE_ID = /^src_[0-9a-f]{24}$/;
const ANALYSIS_RUN_ID = /^arun_[0-9a-f]{32}$/;
const PARENT_SEGMENT_ID = /^seg_([0-9a-f]{24})_([1-9][0-9]{0,6})_(?:0|[1-9][0-9]{0,6})$/;
const RESIDUAL_ID = /^rseg_[0-9a-f]{64}$/;

export class ResidualBindingIntegrityError extends Error {
  constructor(detail: string) {
    super(`Residual material response failed integrity validation: ${detail}`);
    this.name = "ResidualBindingIntegrityError";
  }
}

function fail(detail: string): never {
  throw new ResidualBindingIntegrityError(detail);
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail("residual_binding must be an object.");
  }
  const raw = value as Record<string, unknown>;
  const keys = Object.keys(raw);
  if (keys.length !== RESIDUAL_FIELDS.size || keys.some((key) => !RESIDUAL_FIELDS.has(key))) {
    fail("residual_binding has an unsupported shape.");
  }
  return raw;
}

function exactString(value: unknown, expected: string, label: string): string {
  if (value !== expected) fail(`${label} is invalid.`);
  return value as string;
}

function pattern(value: unknown, expression: RegExp, label: string): string {
  if (typeof value !== "string" || !expression.test(value)) fail(`${label} is invalid.`);
  return value;
}

function integer(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) fail(`${label} is invalid.`);
  return value as number;
}

export function isResidualCandidateId(value: unknown): value is string {
  return typeof value === "string" && RESIDUAL_ID.test(value);
}

/**
 * Validate the closed residual proof independently of any presentation layer.
 * Consumers must still bind every field they project (candidate, Timeline
 * clip, source binding) to this normalized proof before trusting it.
 */
export function normalizeClosedResidualBinding(value: unknown): ResidualBinding {
  const raw = record(value);
  const residualId = pattern(raw.residual_id, RESIDUAL_ID, "residual_id");
  const parentSegmentId = pattern(
    raw.parent_segment_id,
    PARENT_SEGMENT_ID,
    "parent_segment_id",
  );
  const parentMatch = PARENT_SEGMENT_ID.exec(parentSegmentId);
  const assetId = pattern(raw.asset_id, ASSET_ID, "asset_id");
  const assetSha256 = pattern(raw.asset_sha256, SHA256, "asset_sha256");
  const assetSourceId = pattern(raw.asset_source_id, SOURCE_ID, "asset_source_id");
  const analysisRunId = pattern(raw.analysis_run_id, ANALYSIS_RUN_ID, "analysis_run_id");
  const analysisRevision = integer(raw.analysis_revision, "analysis_revision", 1);
  const parentStartMs = integer(raw.parent_start_ms, "parent_start_ms");
  const parentEndMs = integer(raw.parent_end_ms, "parent_end_ms", 1);
  const sourceInMs = integer(raw.source_in_ms, "source_in_ms");
  const sourceOutMs = integer(raw.source_out_ms, "source_out_ms", 1);
  const inputAssetSha256 = pattern(raw.input_asset_sha256, SHA256, "input_asset_sha256");

  if (
    assetId !== `asset_${assetSha256.slice(0, 24)}`
    || parentMatch?.[1] !== assetSha256.slice(0, 24)
    || Number(parentMatch?.[2]) !== analysisRevision
    || inputAssetSha256 !== assetSha256
  ) {
    fail("residual parent, asset, or analysis identity is inconsistent.");
  }
  if (
    parentEndMs <= parentStartMs
    || sourceOutMs <= sourceInMs
    || sourceInMs < parentStartMs
    || sourceOutMs > parentEndMs
    || (sourceInMs === parentStartMs && sourceOutMs === parentEndMs)
  ) {
    fail("residual interval is not a strict half-open parent remainder.");
  }

  return {
    object: exactString(
      raw.object,
      "memolens.residual_binding",
      "residual_binding.object",
    ) as ResidualBinding["object"],
    contract_version: exactString(
      raw.contract_version,
      "1",
      "residual_binding.contract_version",
    ) as ResidualBinding["contract_version"],
    residual_id: residualId,
    parent_segment_id: parentSegmentId,
    asset_id: assetId,
    asset_sha256: assetSha256,
    asset_source_id: assetSourceId,
    source_binding_sha256: pattern(
      raw.source_binding_sha256,
      SHA256,
      "source_binding_sha256",
    ),
    analysis_run_id: analysisRunId,
    analysis_revision: analysisRevision,
    input_asset_sha256: inputAssetSha256,
    parent_start_ms: parentStartMs,
    parent_end_ms: parentEndMs,
    source_in_ms: sourceInMs,
    source_out_ms: sourceOutMs,
    usage_revision: pattern(raw.usage_revision, SHA256, "usage_revision"),
    parent_usage_projection_sha256: pattern(
      raw.parent_usage_projection_sha256,
      SHA256,
      "parent_usage_projection_sha256",
    ),
  };
}

export function normalizeResidualBinding(
  value: unknown,
  candidate: Pick<
    CreativeAssetMatch,
    | "id"
    | "asset_id"
    | "asset_source_id"
    | "analysis_run_id"
    | "analysis_revision"
    | "start_ms"
    | "end_ms"
  >,
): ResidualBinding {
  const binding = normalizeClosedResidualBinding(value);
  if (
    binding.residual_id !== candidate.id
    || binding.asset_id !== candidate.asset_id
    || binding.asset_source_id !== candidate.asset_source_id
    || binding.analysis_run_id !== candidate.analysis_run_id
    || binding.analysis_revision !== candidate.analysis_revision
    || binding.source_in_ms !== candidate.start_ms
    || binding.source_out_ms !== candidate.end_ms
  ) {
    fail("residual_binding does not match its presented candidate.");
  }
  return binding;
}

export function residualParentSegmentId(
  match: Pick<CreativeAssetMatch, "id" | "parent_segment_id" | "residual_binding">,
): string {
  if (!isResidualCandidateId(match.id)) return match.id;
  const parent = match.residual_binding?.parent_segment_id ?? match.parent_segment_id;
  if (typeof parent !== "string" || !PARENT_SEGMENT_ID.test(parent)) {
    fail("residual candidate has no verified parent segment resolver.");
  }
  return parent;
}
