import type {
  AssetImportResult,
  MediaJob,
  MixedSearchResponse,
  VideoCapabilityStatus,
  VideoSegmentDetail,
} from "../types";
import {
  asNullableNumber,
  asNullableString,
  asNumber,
  asRecord,
  asString,
  asStringList,
  normalizeJob,
  normalizeMatch,
  unwrapRecord,
} from "./normalizers";
import { requestJson, VideoApiError } from "./transport";

export async function fetchVideoCapabilities(
  apiBase: string,
  signal?: AbortSignal,
): Promise<VideoCapabilityStatus> {
  let payload: Record<string, unknown>;
  try {
    payload = await requestJson(apiBase, "/v1/media/capabilities", {
      signal,
      timeoutMs: 10_000,
    });
  } catch (error) {
    if (!(error instanceof VideoApiError) || error.status !== 404) {
      throw error;
    }
    payload = await requestJson(apiBase, "/v1/capabilities", {
      signal,
      timeoutMs: 10_000,
    });
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
  const payload = await requestJson(
    apiBase,
    `/v1/index/jobs/${encodeURIComponent(jobId)}?${params.toString()}`,
    { signal, timeoutMs: 10_000 },
  );
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
    refinement_job: Object.keys(rawRefinement).length > 0
      ? normalizeJob(rawRefinement)
      : null,
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
