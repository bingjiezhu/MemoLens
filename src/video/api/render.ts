import type { RenderJob, RenderKind } from "../types";
import { normalizeRender } from "./normalizers";
import {
  cleanBase,
  requestJson,
  resolveVideoResourceUrl,
  VideoApiError,
} from "./transport";

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
    throw new VideoApiError(
      "Preview render requires an immutable timeline hash and an app-managed output root.",
      {
        status: 400,
        code: "render_contract_incomplete",
        retryable: false,
      },
    );
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
  const payload = await requestJson(
    apiBase,
    `/v1/renders/${encodeURIComponent(jobId)}?${params.toString()}`,
    { signal, timeoutMs: 10_000 },
  );
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
    job.download_url
      ?? job.output?.download_url
      ?? `/v1/renders/${encodeURIComponent(job.id)}/download`,
  ) ?? `${cleanBase(apiBase)}/v1/renders/${encodeURIComponent(job.id)}/download`;
}
