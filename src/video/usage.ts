import type {
  CanonicalUsagePolicy,
  CanonicalUsageProjection,
  CanonicalUsageSelection,
  CreativeAssetMatch,
  SearchUsageMode,
} from "./types";

const USAGE_FIELDS = new Set([
  "asset_id",
  "media_kind",
  "occurrence_count",
  "used",
  "used_in",
  "source_domain",
  "candidate_domain",
  "used_intervals",
  "residual_intervals",
  "fully_used",
  "has_residual",
]);
const USED_IN_FIELDS = new Set(["project_id", "export_revision"]);
const SHA256 = /^[0-9a-f]{64}$/;

export class CanonicalUsageIntegrityError extends Error {
  constructor(detail: string) {
    super(`Canonical Usage response failed integrity validation: ${detail}`);
    this.name = "CanonicalUsageIntegrityError";
  }
}

function fail(detail: string): never {
  throw new CanonicalUsageIntegrityError(detail);
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function closedRecord(
  value: unknown,
  fields: ReadonlySet<string>,
  label: string,
): Record<string, unknown> {
  const result = record(value, label);
  const keys = Object.keys(result);
  if (keys.length !== fields.size || keys.some((key) => !fields.has(key))) {
    fail(`${label} has an unsupported shape.`);
  }
  return result;
}

function positiveIdentifier(value: unknown, label: string): string {
  if (
    typeof value !== "string"
    || value.length === 0
    || value.length > 256
    || value.includes("\0")
  ) {
    fail(`${label} is invalid.`);
  }
  return value;
}

function requireCanonicalAnalysisEvidence(match: CreativeAssetMatch): void {
  if (match.analysis_status === "current") {
    const analysisRunId = positiveIdentifier(match.analysis_run_id, "analysis_run_id");
    if (
      analysisRunId !== analysisRunId.trim()
      || !Number.isSafeInteger(match.analysis_revision)
      || (match.analysis_revision as number) < 1
      || (match.analysis_revision as number) > 1_000_000
    ) {
      fail("a current candidate has no exact canonical analysis binding.");
    }
    if (match.result_type === "image_asset") {
      const observation = match.canonical_image_observation;
      if (
        !observation
        || observation.asset_id !== match.asset_id
        || observation.provenance_status !== "verified_current"
        || observation.analysis_binding.analysis_run_id !== analysisRunId
        || observation.analysis_binding.revision !== match.analysis_revision
      ) {
        fail("a current image candidate has no exact canonical image observation.");
      }
    }
    return;
  }
  if (
    (match.analysis_status !== "pending" && match.analysis_status !== "unknown")
    || match.analysis_run_id !== null
    || match.analysis_revision !== null
  ) {
    fail("a non-current candidate carries a false canonical analysis binding.");
  }
}

function safeInteger(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    fail(`${label} is invalid.`);
  }
  return value as number;
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") fail(`${label} must be a boolean.`);
  return value;
}

function halfOpenInterval(value: unknown, label: string): [number, number] {
  if (!Array.isArray(value) || value.length !== 2) {
    fail(`${label} must be one half-open interval.`);
  }
  const start = safeInteger(value[0], `${label} start`);
  const end = safeInteger(value[1], `${label} end`);
  if (end <= start) fail(`${label} must have positive length.`);
  return [start, end];
}

function canonicalIntervals(value: unknown, label: string): Array<[number, number]> {
  if (!Array.isArray(value) || value.length > 4_096) {
    fail(`${label} must be a bounded interval array.`);
  }
  const intervals = value.map((item, index) => halfOpenInterval(item, `${label}[${index}]`));
  for (let index = 1; index < intervals.length; index += 1) {
    // The backend canonical union merges both overlap and exact adjacency.
    if (intervals[index][0] <= intervals[index - 1][1]) {
      fail(`${label} must be sorted, disjoint, and adjacency-merged.`);
    }
  }
  return intervals;
}

function intervalsInside(
  intervals: Array<[number, number]>,
  domain: [number, number],
  label: string,
): void {
  if (intervals.some(([start, end]) => start < domain[0] || end > domain[1])) {
    fail(`${label} leaves its candidate domain.`);
  }
}

function exactPartition(
  domain: [number, number],
  used: Array<[number, number]>,
  residual: Array<[number, number]>,
): void {
  const partition = [...used, ...residual].sort(
    (left, right) => left[0] - right[0] || left[1] - right[1],
  );
  let cursor = domain[0];
  for (const [start, end] of partition) {
    if (start !== cursor) fail("video used and residual intervals do not exactly partition the candidate.");
    cursor = end;
  }
  if (cursor !== domain[1]) {
    fail("video used and residual intervals do not exactly partition the candidate.");
  }
}

function normalizedUsedIn(value: unknown, occurrenceCount: number) {
  if (!Array.isArray(value) || value.length > Math.min(4_096, occurrenceCount)) {
    fail("usage.used_in must be a bounded provenance array.");
  }
  const rows = value.map((item, index) => {
    const row = closedRecord(item, USED_IN_FIELDS, `usage.used_in[${index}]`);
    const exportRevision = safeInteger(
      row.export_revision,
      `usage.used_in[${index}].export_revision`,
      1,
    );
    if (exportRevision > 1_000_000) {
      fail(`usage.used_in[${index}].export_revision is too large.`);
    }
    return {
      project_id: positiveIdentifier(
        row.project_id,
        `usage.used_in[${index}].project_id`,
      ),
      export_revision: exportRevision,
    };
  });
  for (let index = 1; index < rows.length; index += 1) {
    const prior = rows[index - 1];
    const current = rows[index];
    if (
      current.project_id < prior.project_id
      || (
        current.project_id === prior.project_id
        && current.export_revision <= prior.export_revision
      )
    ) {
      fail("usage.used_in must be sorted and unique.");
    }
  }
  if (rows.length > occurrenceCount) {
    fail("usage.used_in exceeds usage.occurrence_count.");
  }
  return rows;
}

export function requireCanonicalUsageRevision(value: unknown): string {
  if (typeof value !== "string" || !SHA256.test(value)) {
    fail("usage_revision must be a lowercase SHA-256 digest.");
  }
  return value;
}

export function normalizeCanonicalUsageProjection(
  value: unknown,
  candidate: Pick<
    CreativeAssetMatch,
    "asset_id" | "result_type" | "start_ms" | "end_ms"
  >,
): CanonicalUsageProjection {
  const usage = closedRecord(value, USAGE_FIELDS, "usage");
  const assetId = positiveIdentifier(usage.asset_id, "usage.asset_id");
  if (assetId !== candidate.asset_id) fail("usage asset identity does not match its candidate.");

  const occurrenceCount = safeInteger(
    usage.occurrence_count,
    "usage.occurrence_count",
  );
  if (occurrenceCount > 1_000_000) fail("usage.occurrence_count is too large.");
  const used = booleanValue(usage.used, "usage.used");
  const usedIn = normalizedUsedIn(usage.used_in, occurrenceCount);
  if (
    used !== (occurrenceCount > 0)
    || used !== (usedIn.length > 0)
  ) {
    fail("usage used/count/provenance fields are inconsistent.");
  }

  const fullyUsed = booleanValue(usage.fully_used, "usage.fully_used");
  const hasResidual = booleanValue(usage.has_residual, "usage.has_residual");
  const usedIntervals = canonicalIntervals(usage.used_intervals, "usage.used_intervals");
  const residualIntervals = canonicalIntervals(
    usage.residual_intervals,
    "usage.residual_intervals",
  );

  if (candidate.result_type === "image_asset") {
    if (
      usage.media_kind !== "image"
      || candidate.start_ms !== null
      || candidate.end_ms !== null
      || usage.source_domain !== null
      || usage.candidate_domain !== null
      || usedIntervals.length !== 0
      || residualIntervals.length !== 0
      || fullyUsed !== used
      || hasResidual !== !used
    ) {
      fail("image usage semantics are inconsistent.");
    }
    return {
      asset_id: assetId,
      media_kind: "image",
      occurrence_count: occurrenceCount,
      used,
      used_in: usedIn,
      source_domain: null,
      candidate_domain: null,
      used_intervals: [],
      residual_intervals: [],
      fully_used: fullyUsed,
      has_residual: hasResidual,
    };
  }

  if (candidate.result_type !== "video_segment" || usage.media_kind !== "video") {
    fail("video usage media kind does not match its candidate.");
  }
  if (
    !Number.isSafeInteger(candidate.start_ms)
    || !Number.isSafeInteger(candidate.end_ms)
    || (candidate.start_ms as number) < 0
    || (candidate.end_ms as number) <= (candidate.start_ms as number)
  ) {
    fail("video candidate interval is invalid.");
  }
  const sourceDomain = halfOpenInterval(usage.source_domain, "usage.source_domain");
  if (sourceDomain[0] !== 0) fail("video source domain must begin at zero.");
  const candidateDomain = halfOpenInterval(
    usage.candidate_domain,
    "usage.candidate_domain",
  );
  if (
    candidateDomain[0] !== candidate.start_ms
    || candidateDomain[1] !== candidate.end_ms
    || candidateDomain[0] < sourceDomain[0]
    || candidateDomain[1] > sourceDomain[1]
  ) {
    fail("video candidate domain is outside or differs from its source candidate.");
  }
  intervalsInside(usedIntervals, candidateDomain, "usage.used_intervals");
  intervalsInside(residualIntervals, candidateDomain, "usage.residual_intervals");
  exactPartition(candidateDomain, usedIntervals, residualIntervals);
  if (
    fullyUsed !== (usedIntervals.length > 0 && residualIntervals.length === 0)
    || hasResidual !== (residualIntervals.length > 0)
  ) {
    fail("video fully-used/residual flags are inconsistent.");
  }
  return {
    asset_id: assetId,
    media_kind: "video",
    occurrence_count: occurrenceCount,
    used,
    used_in: usedIn,
    source_domain: sourceDomain,
    candidate_domain: candidateDomain,
    used_intervals: usedIntervals,
    residual_intervals: residualIntervals,
    fully_used: fullyUsed,
    has_residual: hasResidual,
  };
}

export function usageMatchSelectableInBrief(
  match: CreativeAssetMatch,
  mode: SearchUsageMode,
): boolean {
  if (match.result_type === "image_asset") {
    try {
      requireCanonicalAnalysisEvidence(match);
    } catch {
      return false;
    }
  }
  if (mode !== "unused_only") return true;
  if (!match.usage) return false;
  if (match.result_type === "image_asset") return !match.usage.used;
  return match.result_type === "video_segment"
    && match.usage.used_intervals.length === 0;
}

export function selectableUsageReferenceIds(
  referenceIds: readonly string[],
  matches: readonly CreativeAssetMatch[],
  mode: SearchUsageMode,
): string[] {
  const matchesById = new Map(matches.map((match) => [match.id, match]));
  return referenceIds.filter((referenceId) => {
    const match = matchesById.get(referenceId);
    return Boolean(match && usageMatchSelectableInBrief(match, mode));
  });
}

export function buildCanonicalUsageSelection(
  referenceIds: readonly string[],
  matches: readonly CreativeAssetMatch[],
  policy: CanonicalUsagePolicy,
  usageRevision: string,
  derivativeRevision: string,
): CanonicalUsageSelection {
  const revision = requireCanonicalUsageRevision(usageRevision);
  const frozenDerivativeRevision = requireCanonicalUsageRevision(derivativeRevision);
  if (
    referenceIds.length === 0
    || referenceIds.length > 24
    || new Set(referenceIds).size !== referenceIds.length
  ) {
    fail("usage selection references must be non-empty and unique.");
  }
  const matchesById = new Map(matches.map((match) => [match.id, match]));
  const candidates = referenceIds.map((referenceId) => {
    const match = matchesById.get(referenceId);
    if (!match || !match.usage) {
      fail("usage selection is missing a current projected candidate.");
    }
    if (policy === "unused_only" && !usageMatchSelectableInBrief(match, policy)) {
      fail("a selected video is neither wholly unused nor an exact residual clip.");
    }
    requireCanonicalAnalysisEvidence(match);
    return {
      id: match.id,
      asset_id: match.asset_id,
      asset_source_id: match.asset_source_id,
      result_type: match.result_type,
      analysis_run_id: match.analysis_run_id,
      analysis_revision: match.analysis_revision,
      start_ms: match.start_ms,
      end_ms: match.end_ms,
      usage: match.usage,
      ...(match.residual_binding
        ? { residual_binding: match.residual_binding }
        : {}),
    };
  });
  return {
    policy,
    usage_revision: revision,
    derivative_revision: frozenDerivativeRevision,
    candidates,
  };
}
