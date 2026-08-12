import type {
  AssetImportResult,
  CreativeAssetMatch,
  CreativeBriefInput,
  CreativeProject,
  CreativeTimeline,
  MediaJob,
  MediaJobError,
  MixedSearchResponse,
  RenderJob,
  RenderKind,
  TimelineOperation,
  TimelineInstructionPreview,
  TimelineRevisionResponse,
  TimelineValidation,
  VideoCapabilityStatus,
  VideoSegmentDetail,
} from "./types";

interface RequestOptions {
  method?: "GET" | "POST";
  body?: Record<string, unknown>;
  signal?: AbortSignal;
  timeoutMs?: number;
  idempotencyKey?: string;
}

export class VideoApiError extends Error {
  status: number;
  code: string | null;
  retryable: boolean | null;
  field: string | null;

  constructor(message: string, options: {
    status: number;
    code?: string | null;
    retryable?: boolean | null;
    field?: string | null;
  }) {
    super(message);
    this.name = "VideoApiError";
    this.status = options.status;
    this.code = options.code ?? null;
    this.retryable = typeof options.retryable === "boolean" ? options.retryable : null;
    this.field = options.field ?? null;
  }
}

function cleanBase(apiBase: string): string {
  return apiBase.replace(/\/+$/, "");
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function asNumber(value: unknown, fallback = 0): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function asNullableNumber(value: unknown): number | null {
  const numeric = Number(value);
  return value !== null && value !== undefined && Number.isFinite(numeric) ? numeric : null;
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => asString(item)).filter(Boolean)
    : [];
}

function normalizeProgress(value: unknown): number {
  const numeric = asNumber(value, 0);
  const percent = numeric > 0 && numeric <= 1 ? numeric * 100 : numeric;
  return Math.max(0, Math.min(100, Math.round(percent)));
}

function createRequestSignal(parent: AbortSignal | undefined, timeoutMs: number): {
  signal: AbortSignal;
  cleanup: () => void;
  timedOut: () => boolean;
} {
  const controller = new AbortController();
  let reachedDeadline = false;
  const abortFromParent = () => controller.abort(parent?.reason);
  if (parent?.aborted) {
    controller.abort(parent.reason);
  } else {
    parent?.addEventListener("abort", abortFromParent, { once: true });
  }
  const timeoutId = window.setTimeout(() => {
    reachedDeadline = true;
    controller.abort(new DOMException("Request timed out", "TimeoutError"));
  }, timeoutMs);
  return {
    signal: controller.signal,
    timedOut: () => reachedDeadline,
    cleanup: () => {
      window.clearTimeout(timeoutId);
      parent?.removeEventListener("abort", abortFromParent);
    },
  };
}

async function requestJson(
  apiBase: string,
  path: string,
  options: RequestOptions = {},
): Promise<Record<string, unknown>> {
  const { signal, cleanup, timedOut } = createRequestSignal(options.signal, options.timeoutMs ?? 20_000);
  try {
    const headers: Record<string, string> = {};
    if (options.body) {
      headers["Content-Type"] = "application/json";
    }
    if (options.idempotencyKey) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await fetch(`${cleanBase(apiBase)}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal,
    });
    const payload = asRecord(await response.json().catch(() => ({})));
    const error = asRecord(payload.error);
    const isErrorEnvelope = asString(payload.object) === "error"
      || (Object.keys(error).length > 0 && asString(error.message).length > 0);
    if (!response.ok || isErrorEnvelope) {
      throw new VideoApiError(
        asString(error.message) || asString(payload.message) || `Request failed with status ${response.status}`,
        {
          status: response.ok ? 502 : response.status,
          code: asNullableString(error.code),
          retryable: typeof error.retryable === "boolean" ? error.retryable : null,
          field: asNullableString(error.field),
        },
      );
    }
    return payload;
  } catch (error) {
    if (timedOut()) {
      throw new DOMException("Request timed out", "TimeoutError");
    }
    throw error;
  } finally {
    cleanup();
  }
}

function normalizeJob(rawValue: unknown): MediaJob {
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

function normalizeMatch(rawValue: unknown): CreativeAssetMatch {
  const raw = asRecord(rawValue);
  const resultType = asString(raw.result_type ?? raw.type, "image_asset") as CreativeAssetMatch["result_type"];
  const id = asString(raw.id ?? raw.segment_id ?? raw.asset_id);
  return {
    object: asString(raw.object, "creative_asset_match"),
    schema_version: asString(raw.schema_version, "1"),
    result_type: resultType,
    id,
    asset_id: asString(raw.asset_id, id),
    asset_source_id: asString(raw.asset_source_id ?? raw.source_id),
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
    analysis_revision: asNullableNumber(raw.analysis_revision),
    provenance: asStringList(raw.provenance),
    reasons: asStringList(raw.reasons),
    warnings: asStringList(raw.warnings),
  };
}

function unwrapRecord(payload: Record<string, unknown>, key: string): Record<string, unknown> {
  const nested = asRecord(payload[key]);
  return Object.keys(nested).length > 0 ? nested : payload;
}

export function resolveVideoResourceUrl(apiBase: string, resourceUrl: string | null | undefined): string | null {
  if (!resourceUrl) {
    return null;
  }
  try {
    const backend = new URL(`${cleanBase(apiBase)}/`);
    const resource = new URL(resourceUrl, backend);
    return resource.origin === backend.origin ? resource.toString() : null;
  } catch {
    return null;
  }
}

export async function fetchVideoCapabilities(
  apiBase: string,
  signal?: AbortSignal,
): Promise<VideoCapabilityStatus> {
  let payload: Record<string, unknown>;
  try {
    payload = await requestJson(apiBase, "/v1/media/capabilities", { signal, timeoutMs: 10_000 });
  } catch (error) {
    if (!(error instanceof VideoApiError) || error.status !== 404) {
      throw error;
    }
    payload = await requestJson(apiBase, "/v1/capabilities", { signal, timeoutMs: 10_000 });
  }
  const raw = unwrapRecord(payload, "capabilities");
  const ffmpeg = asRecord(raw.ffmpeg);
  const ffprobe = asRecord(raw.ffprobe);
  const transcription = asRecord(raw.transcription);
  const vision = asRecord(raw.vision);
  const encoderProbe = asRecord(raw.encoder_probe);
  const verifiedPreviewSaveAs = asRecord(raw.verified_preview_save_as);
  const supported = asRecord(raw.supported);
  const externalVideoAnalysis = raw.external_video_analysis === true;
  const localVisionAvailable = Boolean(vision.available) && asString(vision.mode) === "local";
  return {
    object: asString(raw.object, "media.capabilities"),
    schema_version: asString(raw.schema_version, "1"),
    status: asString(raw.status, "unknown"),
    ffmpeg: {
      available: Boolean(ffmpeg.available),
      version: asNullableString(ffmpeg.version),
    },
    ffprobe: {
      available: Boolean(ffprobe.available),
      version: asNullableString(ffprobe.version),
    },
    encoder_probe: {
      available: encoderProbe.available === true,
      code: asNullableString(encoderProbe.code),
      message: asNullableString(encoderProbe.message),
      profiles: asStringList(encoderProbe.profiles),
      duration_ms: asNullableNumber(encoderProbe.duration_ms),
    },
    local_mode: raw.local_mode !== false && raw.local_only !== false,
    transcription: {
      available: Boolean(transcription.available),
      mode: asString(transcription.mode, "unavailable"),
    },
    vision: {
      available: localVisionAvailable || externalVideoAnalysis,
      mode: localVisionAvailable
        ? "local"
        : externalVideoAnalysis
          ? asString(vision.mode, "external")
          : "metadata_only",
    },
    supported_inputs: [
      ...asStringList(raw.supported_inputs),
      ...asStringList(supported.image_extensions),
      ...asStringList(supported.video_extensions),
    ].filter((value, index, list) => list.indexOf(value) === index),
    supported_output: Array.isArray(supported.render_profiles)
      ? asStringList(supported.render_profiles)
      : asStringList(raw.supported_output),
    preview_root_id: asNullableString(raw.preview_root_id ?? supported.preview_root_id),
    verified_preview_save_as: raw.verified_preview_save_as === true
      || verifiedPreviewSaveAs.available === true
      || supported.verified_preview_save_as === true
      || supported.high_resolution_artifact_via_electron_save_as === true,
    message: asNullableString(raw.message),
  };
}

export async function importVideoAssets(input: {
  apiBase: string;
  imageLibraryDir: string;
  dbPath?: string | null;
  dryRun?: boolean;
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<AssetImportResult> {
  const payload = await requestJson(input.apiBase, "/v1/assets/import", {
    method: "POST",
    body: {
      root_path: input.imageLibraryDir,
      db_path: input.dbPath || undefined,
      recursive: true,
      kinds: ["video"],
      dry_run: Boolean(input.dryRun),
    },
    signal: input.signal,
    timeoutMs: 45_000,
    idempotencyKey: input.idempotencyKey,
  });
  const raw = unwrapRecord(payload, "result");
  const jobsValue = raw.jobs ?? payload.jobs;
  const jobs = Array.isArray(jobsValue) ? jobsValue : [];
  const assetsValue = raw.assets ?? payload.assets;
  const assets = Array.isArray(assetsValue) ? assetsValue : [];
  const rawJob = asRecord(raw.job ?? payload.job ?? jobs[0]);
  const jobId = asNullableString(raw.job_id ?? payload.job_id ?? rawJob.id);
  const rejectedValues = Array.isArray(raw.rejected) ? raw.rejected : [];
  return {
    object: asString(raw.object, "asset.import"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id, jobId ?? `import-${Date.now()}`),
    status: asString(raw.status, jobId ? "queued" : "completed"),
    job: Object.keys(rawJob).length > 0 ? normalizeJob(rawJob) : null,
    jobs: jobs.map(normalizeJob).filter((job) => job.id),
    job_id: jobId,
    asset_ids: asStringList(raw.asset_ids ?? payload.asset_ids).length > 0
      ? asStringList(raw.asset_ids ?? payload.asset_ids)
      : assets.map((value) => asString(asRecord(value).id)).filter(Boolean),
    imported: asNumber(raw.imported ?? raw.imported_count, assets.length),
    skipped: asNumber(raw.skipped ?? raw.skipped_count, 0),
    rejected: rejectedValues.map((value) => {
      const rejected = asRecord(value);
      return {
        code: asNullableString(rejected.code),
        message: asString(rejected.message, "Asset rejected."),
        relative_path: asNullableString(rejected.relative_path),
        retryable: Boolean(rejected.retryable),
      };
    }),
    message: asNullableString(raw.message),
  };
}

export async function fetchMediaJob(
  apiBase: string,
  jobId: string,
  dbPath: string,
  signal?: AbortSignal,
): Promise<MediaJob> {
  const params = new URLSearchParams({ db_path: dbPath });
  const payload = await requestJson(apiBase, `/v1/index/jobs/${encodeURIComponent(jobId)}?${params.toString()}`, {
    signal,
    timeoutMs: 10_000,
  });
  return normalizeJob(payload.job ?? payload);
}

export async function fetchRecentMediaJobs(
  apiBase: string,
  dbPath: string,
  signal?: AbortSignal,
): Promise<MediaJob[]> {
  const params = new URLSearchParams({ active: "false", limit: "50", db_path: dbPath });
  const payload = await requestJson(apiBase, `/v1/index/jobs?${params.toString()}`, {
    signal,
    timeoutMs: 10_000,
  });
  const values = Array.isArray(payload.jobs)
    ? payload.jobs
    : Array.isArray(payload.data)
      ? payload.data
      : [];
  return values.map(normalizeJob).filter((job) => job.id);
}

export async function changeMediaJob(
  apiBase: string,
  jobId: string,
  action: "cancel" | "resume",
  dbPath: string,
  signal?: AbortSignal,
  idempotencyKey?: string,
): Promise<MediaJob> {
  const payload = await requestJson(
    apiBase,
    `/v1/index/jobs/${encodeURIComponent(jobId)}/${action}`,
    {
      method: "POST",
      body: { db_path: dbPath },
      signal,
      timeoutMs: 10_000,
      idempotencyKey,
    },
  );
  return normalizeJob(payload.job ?? payload);
}

export async function searchMixedAssets(input: {
  apiBase: string;
  query: string;
  dbPath?: string | null;
  topK?: number;
  orientation?: string | null;
  excludedTerms?: string[];
  signal?: AbortSignal;
}): Promise<MixedSearchResponse> {
  const payload = await requestJson(input.apiBase, "/v1/search/mixed", {
    method: "POST",
    body: {
      query: input.query,
      db_path: input.dbPath || undefined,
      types: ["image", "video_segment"],
      top_k: input.topK ?? 24,
      filters: {
        orientation: input.orientation || undefined,
        excluded_terms: input.excludedTerms?.length ? input.excludedTerms : undefined,
      },
      refinement: {
        mode: "auto",
        max_segments: 3,
        budget_frames: 300,
      },
    },
    signal: input.signal,
    timeoutMs: 45_000,
  });
  const raw = unwrapRecord(payload, "result");
  const values = Array.isArray(raw.results)
    ? raw.results
    : Array.isArray(raw.data)
      ? raw.data
      : [];
  const rawRefinement = asRecord(raw.refinement_job);
  return {
    object: asString(raw.object, "mixed.search"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id, `search-${Date.now()}`),
    status: asString(raw.status, "completed"),
    results: values.map(normalizeMatch).filter((item) => item.id && item.asset_id),
    candidate_count: asNumber(raw.candidate_count, values.length),
    message: asNullableString(raw.message),
    refinement_job: Object.keys(rawRefinement).length > 0 ? normalizeJob(rawRefinement) : null,
  };
}

export async function fetchVideoSegment(
  apiBase: string,
  segmentId: string,
  dbPath?: string | null,
  signal?: AbortSignal,
): Promise<VideoSegmentDetail> {
  const params = new URLSearchParams();
  if (dbPath) params.set("db_path", dbPath);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const payload = await requestJson(
    apiBase,
    `/v1/video-segments/${encodeURIComponent(segmentId)}${suffix}`,
    { signal, timeoutMs: 15_000 },
  );
  const raw = unwrapRecord(payload, "segment");
  const keyframes = Array.isArray(raw.keyframes) ? raw.keyframes : [];
  const transcript = Array.isArray(raw.transcript)
    ? raw.transcript
    : Array.isArray(raw.transcript_segments)
      ? raw.transcript_segments
      : [];
  return {
    object: asString(raw.object, "video.segment"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id, segmentId),
    asset_id: asString(raw.asset_id),
    start_ms: asNumber(raw.start_ms, 0),
    end_ms: asNumber(raw.end_ms, 0),
    summary: asString(raw.summary, "Indexed video segment"),
    media_url: asNullableString(raw.media_url ?? raw.stream_url),
    thumbnail_url: asNullableString(raw.thumbnail_url ?? raw.poster_url),
    keyframes: keyframes.map((value) => {
      const frame = asRecord(value);
      return {
        id: asString(frame.id),
        timestamp_ms: asNumber(frame.timestamp_ms, 0),
        thumbnail_url: asNullableString(frame.thumbnail_url ?? frame.url),
        selection_reason: asString(frame.selection_reason, "Representative frame"),
      };
    }),
    transcript: transcript.map((value) => {
      const line = asRecord(value);
      return {
        id: asString(line.id),
        start_ms: asNumber(line.start_ms, 0),
        end_ms: asNumber(line.end_ms, 0),
        text: asString(line.text),
      };
    }).filter((line) => line.text),
    visual_status: asString(raw.visual_status, "unknown"),
    transcript_status: asString(raw.transcript_status, "unknown"),
  };
}

function normalizeProject(rawValue: unknown): CreativeProject {
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
      narrative_arc: asString(brief.narrative_arc, "A clear beginning, development, and ending."),
      candidate_refs: asStringList(brief.candidate_refs),
      missing_assets: asStringList(brief.missing_assets),
      assumptions: asStringList(brief.assumptions),
    },
    candidates: candidates.map(normalizeMatch),
    latest_timeline_id: asNullableString(raw.latest_timeline_id ?? latestTimeline.id),
    latest_timeline_revision: asNullableNumber(raw.latest_timeline_revision ?? latestTimeline.revision),
    created_at: asNullableString(raw.created_at),
    updated_at: asNullableString(raw.updated_at),
  };
}

export async function createCreativeBrief(input: {
  apiBase: string;
  dbPath?: string | null;
  brief: CreativeBriefInput;
  selectedRefs?: string[];
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<CreativeProject> {
  const payload = await requestJson(input.apiBase, "/v1/creative/briefs", {
    method: "POST",
    body: {
      ...input.brief,
      db_path: input.dbPath || undefined,
      candidate_refs: input.selectedRefs?.length
        ? input.selectedRefs
        : input.brief.candidate_refs,
    },
    signal: input.signal,
    timeoutMs: 60_000,
    idempotencyKey: input.idempotencyKey,
  });
  const project = asRecord(payload.project);
  return normalizeProject({
    ...project,
    search: payload.search,
  });
}

export async function fetchCreativeProject(
  apiBase: string,
  projectId: string,
  dbPath?: string | null,
  signal?: AbortSignal,
): Promise<CreativeProject> {
  const params = new URLSearchParams();
  if (dbPath) params.set("db_path", dbPath);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const payload = await requestJson(
    apiBase,
    `/v1/creative/projects/${encodeURIComponent(projectId)}${suffix}`,
    { signal, timeoutMs: 15_000 },
  );
  return normalizeProject(payload.project ?? payload);
}

function normalizeTimeline(rawValue: unknown): CreativeTimeline {
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

export async function createTimeline(input: {
  apiBase: string;
  projectId: string;
  briefRevision: number;
  dbPath?: string | null;
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<TimelineRevisionResponse> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timelines`,
    {
      method: "POST",
      body: {
        db_path: input.dbPath || undefined,
        brief_revision: input.briefRevision,
      },
      signal: input.signal,
      timeoutMs: 60_000,
      idempotencyKey: input.idempotencyKey,
    },
  );
  const timeline = normalizeTimeline({
    ...asRecord(payload.timeline ?? payload),
    content_sha256: payload.content_sha256 ?? asRecord(payload.timeline).content_sha256,
  });
  return {
    object: asString(payload.object, "timeline.revision"),
    schema_version: asString(payload.schema_version, "1"),
    id: asString(payload.id, timeline.id),
    timeline,
    diff: Array.isArray(payload.diff) ? payload.diff as TimelineRevisionResponse["diff"] : [],
    message: asNullableString(payload.message),
  };
}

export async function fetchTimeline(
  apiBase: string,
  timelineId: string,
  revision?: number | null,
  dbPath?: string | null,
  signal?: AbortSignal,
): Promise<CreativeTimeline> {
  const params = new URLSearchParams();
  if (revision) params.set("revision", String(revision));
  if (dbPath) params.set("db_path", dbPath);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const payload = await requestJson(
    apiBase,
    `/v1/timelines/${encodeURIComponent(timelineId)}${suffix}`,
    { signal, timeoutMs: 15_000 },
  );
  return normalizeTimeline({
    ...asRecord(payload.timeline ?? payload),
    content_sha256: payload.content_sha256 ?? asRecord(payload.timeline).content_sha256,
  });
}

export async function reviseTimeline(input: {
  apiBase: string;
  timelineId: string;
  baseRevision: number;
  dbPath?: string | null;
  operations?: TimelineOperation[];
  instruction?: string;
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<TimelineRevisionResponse> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/timelines/${encodeURIComponent(input.timelineId)}/revise`,
    {
      method: "POST",
      body: {
        db_path: input.dbPath || undefined,
        base_revision: input.baseRevision,
        operations: input.operations?.length ? input.operations : undefined,
        instruction: input.instruction?.trim() || undefined,
      },
      signal: input.signal,
      timeoutMs: 60_000,
      idempotencyKey: input.idempotencyKey,
    },
  );
  const timeline = normalizeTimeline({
    ...asRecord(payload.timeline ?? payload),
    content_sha256: payload.content_sha256 ?? asRecord(payload.timeline).content_sha256,
  });
  return {
    object: asString(payload.object, "timeline.revision"),
    schema_version: asString(payload.schema_version, "1"),
    id: asString(payload.id, timeline.id),
    timeline,
    diff: Array.isArray(payload.diff) ? payload.diff as TimelineRevisionResponse["diff"] : [],
    message: asNullableString(payload.message),
  };
}

export async function previewTimelineInstruction(input: {
  apiBase: string;
  timelineId: string;
  baseRevision: number;
  dbPath?: string | null;
  instruction: string;
  signal?: AbortSignal;
}): Promise<TimelineInstructionPreview> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/timelines/${encodeURIComponent(input.timelineId)}/revise`,
    {
      method: "POST",
      body: {
        db_path: input.dbPath || undefined,
        base_revision: input.baseRevision,
        instruction: input.instruction.trim(),
        apply: false,
      },
      signal: input.signal,
      timeoutMs: 45_000,
    },
  );
  const preview = asRecord(payload.preview);
  const operations = Array.isArray(preview.operations)
    ? preview.operations
    : Array.isArray(payload.operations)
      ? payload.operations
      : [];
  const diff = Array.isArray(preview.diff)
    ? preview.diff
    : Array.isArray(payload.diff)
      ? payload.diff
      : [];
  return {
    object: asString(payload.object, "timeline.revision_preview"),
    schema_version: asString(payload.schema_version, "1"),
    id: asString(payload.id, `${input.timelineId}:${input.baseRevision}:preview`),
    operations: operations as TimelineOperation[],
    diff: diff as TimelineInstructionPreview["diff"],
    message: asNullableString(payload.message ?? preview.message),
  };
}

export async function applyTimelineInstruction(input: {
  apiBase: string;
  timelineId: string;
  baseRevision: number;
  dbPath?: string | null;
  instruction: string;
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<TimelineRevisionResponse> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/timelines/${encodeURIComponent(input.timelineId)}/revise`,
    {
      method: "POST",
      body: {
        db_path: input.dbPath || undefined,
        base_revision: input.baseRevision,
        instruction: input.instruction.trim(),
        apply: true,
      },
      signal: input.signal,
      timeoutMs: 60_000,
      idempotencyKey: input.idempotencyKey,
    },
  );
  const timeline = normalizeTimeline({
    ...asRecord(payload.timeline ?? payload),
    content_sha256: payload.content_sha256 ?? asRecord(payload.timeline).content_sha256,
  });
  return {
    object: asString(payload.object, "timeline.revision"),
    schema_version: asString(payload.schema_version, "1"),
    id: asString(payload.id, timeline.id),
    timeline,
    diff: Array.isArray(payload.diff) ? payload.diff as TimelineRevisionResponse["diff"] : [],
    message: asNullableString(payload.message),
  };
}

export async function validateTimeline(input: {
  apiBase: string;
  timelineId: string;
  revision: number;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<TimelineValidation> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/timelines/${encodeURIComponent(input.timelineId)}/validate`,
    {
      method: "POST",
      body: { revision: input.revision, db_path: input.dbPath || undefined },
      signal: input.signal,
      timeoutMs: 20_000,
    },
  );
  const raw = unwrapRecord(payload, "validation");
  const normalizeIssues = (value: unknown): TimelineValidation["errors"] => (
    Array.isArray(value)
      ? value.map((item) => {
          const issue = asRecord(item);
          return {
            code: asString(issue.code, "invalid_timeline"),
            field: asNullableString(issue.field),
            message: asString(issue.message, "Timeline validation failed."),
            severity: asString(issue.severity, "error"),
          };
        })
      : []
  );
  const errors = normalizeIssues(raw.errors);
  const warnings = normalizeIssues(raw.warnings);
  return {
    object: asString(raw.object, "timeline.validation"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id, `${input.timelineId}:${input.revision}`),
    valid: raw.valid === true || (asString(raw.status) === "valid" && errors.length === 0),
    status: asString(raw.status, errors.length === 0 ? "valid" : "invalid"),
    errors,
    warnings,
  };
}

function normalizeRender(rawValue: unknown): RenderJob {
  const raw = asRecord(rawValue);
  const output = asRecord(raw.output);
  const error = asRecord(raw.error);
  return {
    object: asString(raw.object, "render.job"),
    schema_version: asString(raw.schema_version, "1"),
    id: asString(raw.id ?? raw.job_id),
    kind: asString(raw.kind, asString(raw.profile).startsWith("export") ? "export" : "preview") as RenderKind,
    profile: asString(raw.profile, "preview-low"),
    status: asString(raw.status, "queued") as RenderJob["status"],
    stage: asString(raw.stage, asString(raw.status, "queued")),
    progress: normalizeProgress(raw.progress),
    timeline_id: asString(raw.timeline_id),
    timeline_revision: asNumber(raw.timeline_revision, 1),
    output_sha256: asNullableString(raw.output_sha256 ?? output.output_sha256),
    size_bytes: asNullableNumber(raw.size_bytes ?? output.size_bytes),
    download_url: asNullableString(raw.download_url ?? raw.artifact_url ?? output.download_url),
    media_url: asNullableString(raw.media_url ?? raw.artifact_url ?? output.media_url ?? raw.download_url ?? output.download_url),
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

export async function startRender(input: {
  apiBase: string;
  timelineId: string;
  revision: number;
  timelineSha256?: string | null;
  previewRootId: string;
  kind: RenderKind;
  dbPath?: string | null;
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<RenderJob> {
  const profile = input.kind === "preview" ? "preview-low" : "export-1080p";
  if (input.kind !== "preview" || !input.timelineSha256 || !input.previewRootId) {
    throw new VideoApiError("Preview render requires an immutable timeline hash and an app-managed output root.", {
      status: 400,
      code: "render_contract_incomplete",
      retryable: false,
    });
  }
  const payload = await requestJson(input.apiBase, "/v1/renders", {
    method: "POST",
    body: {
      timeline_id: input.timelineId,
      timeline_revision: input.revision,
      expected_timeline_sha256: input.timelineSha256,
      output: { root_id: input.previewRootId },
      profile,
      db_path: input.dbPath || undefined,
    },
    signal: input.signal,
    timeoutMs: 20_000,
    idempotencyKey: input.idempotencyKey,
  });
  return normalizeRender(payload.job ?? payload);
}

export async function fetchRenderJob(
  apiBase: string,
  jobId: string,
  dbPath: string,
  signal?: AbortSignal,
): Promise<RenderJob> {
  const params = new URLSearchParams({ db_path: dbPath });
  const payload = await requestJson(apiBase, `/v1/renders/${encodeURIComponent(jobId)}?${params.toString()}`, {
    signal,
    timeoutMs: 10_000,
  });
  return normalizeRender(payload.job ?? payload);
}

export async function fetchRecentRenderJobs(
  apiBase: string,
  dbPath: string,
  signal?: AbortSignal,
): Promise<RenderJob[]> {
  const params = new URLSearchParams({ active: "false", limit: "20", db_path: dbPath });
  const payload = await requestJson(apiBase, `/v1/renders?${params.toString()}`, {
    signal,
    timeoutMs: 10_000,
  });
  const values = Array.isArray(payload.jobs)
    ? payload.jobs
    : Array.isArray(payload.data)
      ? payload.data
      : [];
  return values.map(normalizeRender).filter((job) => job.id);
}

export async function cancelRenderJob(
  apiBase: string,
  jobId: string,
  dbPath: string,
  signal?: AbortSignal,
  idempotencyKey?: string,
): Promise<RenderJob> {
  const payload = await requestJson(
    apiBase,
    `/v1/renders/${encodeURIComponent(jobId)}/cancel`,
    {
      method: "POST",
      body: { db_path: dbPath },
      signal,
      timeoutMs: 10_000,
      idempotencyKey,
    },
  );
  return normalizeRender(payload.job ?? payload);
}

export function renderDownloadUrl(apiBase: string, job: RenderJob): string {
  return resolveVideoResourceUrl(
    apiBase,
    job.download_url ?? job.output?.download_url ?? `/v1/renders/${encodeURIComponent(job.id)}/download`,
  ) ?? `${cleanBase(apiBase)}/v1/renders/${encodeURIComponent(job.id)}/download`;
}
