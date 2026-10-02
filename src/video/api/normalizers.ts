import type {
  CreativeAnalysisStatus,
  CreativeAssetMatch,
  CreativeProject,
  CreativeTimeline,
  MediaJob,
  MediaJobError,
  RenderJob,
  RenderKind,
} from "../types";
import {
  CanonicalUsageIntegrityError,
  normalizeCanonicalUsageProjection,
} from "../usage";
import { normalizeAtlasCanonicalImageObservation } from "../../query/api";
import {
  isResidualCandidateId,
  normalizeResidualBinding,
} from "../residual";

const ANALYSIS_STATUSES = new Set<CreativeAnalysisStatus>([
  "current",
  "pending",
  "unknown",
]);

interface NormalizedAnalysisEvidence {
  analysis_status: CreativeAnalysisStatus;
  analysis_run_id: string | null;
  analysis_revision: number | null;
}

function exactAnalysisRunId(value: unknown): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 256
    && !value.includes("\0")
    && value === value.trim();
}

function exactAnalysisRevision(value: unknown): value is number {
  return Number.isSafeInteger(value)
    && (value as number) >= 1
    && (value as number) <= 1_000_000;
}

function normalizeAnalysisEvidence(
  raw: Record<string, unknown>,
  resultType: CreativeAssetMatch["result_type"],
  options: { requireCanonical: boolean },
): NormalizedAnalysisEvidence {
  const rawStatus = raw.analysis_status;
  const statusIsMissing = rawStatus === undefined;
  const statusIsSupported = ANALYSIS_STATUSES.has(rawStatus as CreativeAnalysisStatus);
  const hasExactBinding = exactAnalysisRunId(raw.analysis_run_id)
    && exactAnalysisRevision(raw.analysis_revision);
  const hasExactNullBinding = raw.analysis_run_id === null
    && raw.analysis_revision === null;

  let status: CreativeAnalysisStatus;
  if (statusIsSupported) {
    status = rawStatus as CreativeAnalysisStatus;
  } else if (statusIsMissing && resultType === "video_segment" && hasExactBinding) {
    // Pre-analysis_status video responses already carried a closed run/revision binding.
    status = "current";
  } else {
    // Missing image status must never promote an old or forged binding to current.
    status = "unknown";
  }

  const evidenceIsCanonical = status === "current"
    ? hasExactBinding
    : hasExactNullBinding;
  const legacyStatusIsAdmissible = !statusIsMissing
    || (resultType === "video_segment" ? hasExactBinding : hasExactNullBinding);
  if (options.requireCanonical && (!statusIsSupported && !statusIsMissing)) {
    throw new CanonicalUsageIntegrityError("candidate analysis_status is invalid.");
  }
  if (options.requireCanonical && (!legacyStatusIsAdmissible || !evidenceIsCanonical)) {
    throw new CanonicalUsageIntegrityError(
      "candidate analysis status and canonical binding are inconsistent.",
    );
  }
  if (!evidenceIsCanonical) {
    return {
      analysis_status: "unknown",
      analysis_run_id: null,
      analysis_revision: null,
    };
  }
  return {
    analysis_status: status,
    analysis_run_id: status === "current" ? raw.analysis_run_id as string : null,
    analysis_revision: status === "current" ? raw.analysis_revision as number : null,
  };
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

export function asNullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function asNumber(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

export function asNullableNumber(value: unknown): number | null {
  const numeric = Number(value);
  return value !== null && value !== undefined && Number.isFinite(numeric) ? numeric : null;
}

export function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => asString(item)).filter(Boolean)
    : [];
}

export function normalizeProgress(value: unknown): number {
  const numeric = asNumber(value, 0);
  const percent = numeric > 0 && numeric <= 1 ? numeric * 100 : numeric;
  return Math.max(0, Math.min(100, Math.round(percent)));
}

export function unwrapRecord(
  payload: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const nested = asRecord(payload[key]);
  return Object.keys(nested).length > 0 ? nested : payload;
}

export function normalizeJob(rawValue: unknown): MediaJob {
  const raw = asRecord(rawValue);
  const singleError = asRecord(raw.error);
  const rawErrors = Array.isArray(raw.errors)
    ? raw.errors
    : Object.keys(singleError).length > 0
      ? [singleError]
      : [];
  const errors: MediaJobError[] = rawErrors.map((value) => {
    const error = asRecord(value);
    return {
      code: asNullableString(error.code),
      message: asString(error.message, "Media processing failed."),
      asset_id: asNullableString(error.asset_id),
      relative_path: asNullableString(error.relative_path),
      retryable: Boolean(error.retryable),
    };
  });
  const status = asString(raw.status, "queued") as MediaJob["status"];
  return {
    object: asString(raw.object, "media.job"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id ?? raw.job_id),
    kind: asString(raw.kind, "video_index"),
    status,
    stage: asString(raw.stage, status),
    progress: normalizeProgress(raw.progress),
    attempt: asNumber(raw.attempt, 1),
    completed_assets: asNumber(raw.completed_assets ?? raw.completed, 0),
    total_assets: asNumber(raw.total_assets ?? raw.total, 0),
    asset_ids: asStringList(raw.asset_ids),
    errors,
    message: asNullableString(raw.message),
    created_at: asNullableString(raw.created_at),
    finished_at: asNullableString(raw.finished_at),
  };
}

export function normalizeMatch(
  rawValue: unknown,
  options: {
    requireCanonicalUsage?: boolean;
    requireCanonicalImageObservation?: boolean;
  } = {},
): CreativeAssetMatch {
  const raw = asRecord(rawValue);
  const resultType = asString(
    raw.result_type ?? raw.type,
    "image_asset",
  ) as CreativeAssetMatch["result_type"];
  const id = asString(raw.id ?? raw.segment_id ?? raw.asset_id);
  const analysisEvidence = normalizeAnalysisEvidence(raw, resultType, {
    requireCanonical: options.requireCanonicalUsage === true,
  });
  if (options.requireCanonicalUsage && (
    typeof raw.id !== "string"
    || raw.id.length === 0
    || raw.id !== id
    || typeof raw.asset_id !== "string"
    || raw.asset_id.length === 0
    || raw.asset_id !== asString(raw.asset_id)
    || typeof raw.asset_source_id !== "string"
    || raw.asset_source_id.length === 0
    || raw.asset_source_id !== asString(raw.asset_source_id)
    || (raw.result_type !== "image_asset" && raw.result_type !== "video_segment")
    || (
      raw.result_type === "image_asset"
      && (
        raw.start_ms !== null
        || raw.end_ms !== null
      )
    )
    || (
      raw.result_type === "video_segment"
      && (
        !Number.isSafeInteger(raw.start_ms)
        || !Number.isSafeInteger(raw.end_ms)
      )
    )
  )) {
    throw new CanonicalUsageIntegrityError("candidate identity or source interval is invalid.");
  }
  const normalized: CreativeAssetMatch = {
    object: asString(raw.object, "creative_asset_match"),
    schema_version: asString(raw.schema_version, "1"),
    result_type: resultType,
    id,
    asset_id: asString(raw.asset_id, id),
    asset_sha256: asNullableString(raw.asset_sha256),
    asset_source_id: asString(raw.asset_source_id ?? raw.source_id),
    parent_segment_id: null,
    residual_binding: null,
    filename: asNullableString(raw.filename),
    title: asNullableString(raw.title),
    start_ms: asNullableNumber(raw.start_ms),
    end_ms: asNullableNumber(raw.end_ms),
    duration_ms: asNullableNumber(raw.duration_ms),
    thumbnail_url: asNullableString(raw.thumbnail_url ?? raw.poster_url),
    media_url: asNullableString(raw.media_url ?? raw.stream_url),
    summary: asString(raw.summary ?? raw.description, "Indexed local media"),
    matched_terms: asStringList(raw.matched_terms),
    score: asNumber(raw.score, 0),
    confidence: asNullableNumber(raw.confidence),
    ...analysisEvidence,
    canonical_image_observation: null,
    provenance: asStringList(raw.provenance),
    reasons: asStringList(raw.reasons),
    warnings: asStringList(raw.warnings),
    usage: null,
  };
  if (isResidualCandidateId(normalized.id)) {
    if (
      normalized.result_type !== "video_segment"
      || typeof raw.parent_segment_id !== "string"
    ) {
      throw new CanonicalUsageIntegrityError(
        "a residual candidate omitted its exact persisted parent segment.",
      );
    }
    const binding = normalizeResidualBinding(raw.residual_binding, normalized);
    if (
      binding.parent_segment_id !== raw.parent_segment_id
      || normalized.asset_sha256 !== binding.asset_sha256
    ) {
      throw new CanonicalUsageIntegrityError(
        "a residual candidate contradicts its parent or asset binding.",
      );
    }
    const thumbnail = asNullableString(raw.thumbnail_url ?? raw.poster_url);
    if (thumbnail?.includes(encodeURIComponent(normalized.id))) {
      throw new CanonicalUsageIntegrityError(
        "a residual candidate attempted to resolve preview media through its synthetic identity.",
      );
    }
    normalized.parent_segment_id = binding.parent_segment_id;
    normalized.residual_binding = binding;
  } else if (raw.parent_segment_id !== undefined || raw.residual_binding !== undefined) {
    throw new CanonicalUsageIntegrityError(
      "an ordinary candidate carries residual-only authority fields.",
    );
  }
  if (resultType === "image_asset" && analysisEvidence.analysis_status === "current") {
    try {
      const observation = normalizeAtlasCanonicalImageObservation(
        raw.canonical_image_observation,
        normalized.asset_id,
      );
      if (
        observation.analysis_binding.analysis_run_id !== normalized.analysis_run_id
        || observation.analysis_binding.revision !== normalized.analysis_revision
      ) {
        throw new Error("analysis binding mismatch");
      }
      normalized.canonical_image_observation = observation;
    } catch {
      if (options.requireCanonicalImageObservation || options.requireCanonicalUsage) {
        throw new CanonicalUsageIntegrityError(
          "a current image result omitted its exact canonical observation.",
        );
      }
    }
  } else if (
    resultType === "image_asset"
    && options.requireCanonicalImageObservation
  ) {
    throw new CanonicalUsageIntegrityError(
      "a non-current image cannot be selected as grounded evidence.",
    );
  }
  const hasUsage = raw.usage !== undefined && raw.usage !== null;
  if (options.requireCanonicalUsage && !hasUsage) {
    throw new CanonicalUsageIntegrityError("a usage-aware result omitted usage.");
  }
  if (hasUsage) {
    normalized.usage = normalizeCanonicalUsageProjection(raw.usage, normalized);
    if (
      normalized.residual_binding
      && (
        normalized.usage.media_kind !== "video"
        || normalized.usage.used_intervals.length !== 0
        || normalized.usage.residual_intervals.length !== 1
        || normalized.usage.residual_intervals[0][0] !== normalized.start_ms
        || normalized.usage.residual_intervals[0][1] !== normalized.end_ms
      )
    ) {
      throw new CanonicalUsageIntegrityError(
        "a residual candidate carries a non-residual Usage projection.",
      );
    }
  }
  return normalized;
}

export function normalizeProject(rawValue: unknown): CreativeProject {
  const raw = asRecord(rawValue);
  const brief = asRecord(raw.brief);
  const search = asRecord(raw.search);
  const latestTimeline = asRecord(raw.latest_timeline);
  const candidates = Array.isArray(raw.candidates)
    ? raw.candidates
    : Array.isArray(brief.candidates)
      ? brief.candidates
      : Array.isArray(search.data)
        ? search.data
        : [];
  return {
    object: asString(raw.object, "creative.project"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id ?? raw.project_id),
    title: asString(raw.title, asString(brief.goal, "Untitled video")),
    status: asString(raw.status, "draft"),
    brief: {
      id: asString(brief.id ?? brief.brief_id),
      revision: Math.max(1, asNumber(brief.revision ?? raw.brief_revision, 1)),
      goal: asString(brief.goal),
      audience: asString(brief.audience, "General audience"),
      platform: asString(brief.platform, "General"),
      duration_ms: Math.max(1000, asNumber(brief.duration_ms, 15_000)),
      aspect_ratio: asString(brief.aspect_ratio, "9:16"),
      tone: asString(brief.tone, "natural"),
      pace: asString(brief.pace, "balanced"),
      must_include: asStringList(brief.must_include),
      must_exclude: asStringList(brief.must_exclude),
      narrative_arc: asString(
        brief.narrative_arc,
        "A clear beginning, development, and ending.",
      ),
      candidate_refs: asStringList(brief.candidate_refs),
      missing_assets: asStringList(brief.missing_assets),
      assumptions: asStringList(brief.assumptions),
      creator_profile_ref: Object.keys(asRecord(brief.creator_profile_ref)).length > 0
        ? asRecord(brief.creator_profile_ref) as unknown as CreativeProject["brief"]["creator_profile_ref"]
        : null,
      applied_profile_fields: asStringList(brief.applied_profile_fields),
    },
    candidates: candidates.map((value) => normalizeMatch(value)),
    latest_timeline_id: asNullableString(raw.latest_timeline_id ?? latestTimeline.id),
    latest_timeline_revision: asNullableNumber(
      raw.latest_timeline_revision ?? latestTimeline.revision,
    ),
    created_at: asNullableString(raw.created_at),
    updated_at: asNullableString(raw.updated_at),
  };
}

export function normalizeTimeline(rawValue: unknown): CreativeTimeline {
  const raw = asRecord(rawValue);
  const transitions = Array.isArray(raw.transitions) ? raw.transitions : [];
  const transitionByFromClip = new Map(
    transitions.map((value) => {
      const transition = asRecord(value);
      return [asString(transition.from_clip_id), transition] as const;
    }),
  );
  const tracks = Array.isArray(raw.tracks)
    ? raw.tracks.map((trackValue) => {
        const track = asRecord(trackValue);
        const clips = Array.isArray(track.clips)
          ? track.clips.map((clipValue) => {
              const clip = asRecord(clipValue);
              const topLevelTransition = transitionByFromClip.get(asString(clip.id));
              return {
                ...clip,
                kind: asString(clip.kind, asString(track.type, "video")),
                asset_source_id: asString(clip.asset_source_id ?? clip.source_id),
                transition_out: clip.transition_out ?? (topLevelTransition
                  ? {
                      type: asString(topLevelTransition.type),
                      duration_ms: asNumber(topLevelTransition.duration_ms, 0),
                    }
                  : undefined),
              };
            })
          : [];
        return { ...track, clips };
      })
    : [];
  return { ...raw, tracks, transitions } as unknown as CreativeTimeline;
}

export function normalizeRender(rawValue: unknown): RenderJob {
  const raw = asRecord(rawValue);
  const output = asRecord(raw.output);
  const error = asRecord(raw.error);
  return {
    object: asString(raw.object, "render.job"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id ?? raw.job_id),
    kind: asString(
      raw.kind,
      asString(raw.profile).startsWith("export") ? "export" : "preview",
    ) as RenderKind,
    profile: asString(raw.profile, "preview-low"),
    status: asString(raw.status, "queued") as RenderJob["status"],
    stage: asString(raw.stage, asString(raw.status, "queued")),
    progress: normalizeProgress(raw.progress),
    timeline_id: asString(raw.timeline_id),
    timeline_revision: asNumber(raw.timeline_revision, 1),
    output_sha256: asNullableString(raw.output_sha256 ?? output.output_sha256),
    size_bytes: asNullableNumber(raw.size_bytes ?? output.size_bytes),
    download_url: asNullableString(
      raw.download_url ?? raw.artifact_url ?? output.download_url,
    ),
    media_url: asNullableString(
      raw.media_url
        ?? raw.artifact_url
        ?? output.media_url
        ?? raw.download_url
        ?? output.download_url,
    ),
    filename: asNullableString(raw.filename ?? output.filename),
    output: Object.keys(output).length > 0
      ? {
          download_url: asNullableString(output.download_url),
          media_url: asNullableString(output.media_url ?? output.download_url),
          filename: asNullableString(output.filename),
          output_sha256: asNullableString(output.output_sha256),
          size_bytes: asNullableNumber(output.size_bytes),
          duration_ms: asNullableNumber(output.duration_ms),
          width: asNullableNumber(output.width),
          height: asNullableNumber(output.height),
        }
      : null,
    error: Object.keys(error).length > 0
      ? {
          code: asNullableString(error.code),
          message: asString(error.message, "Render failed."),
          retryable: Boolean(error.retryable),
        }
      : null,
    message: asNullableString(raw.message),
  };
}
