import type {
  CreativeTimeline,
  TimelineInstructionPreview,
  TimelineOperation,
  TimelineRevisionResponse,
  TimelineValidation,
} from "../types";
import {
  asNullableString,
  asRecord,
  asString,
  normalizeTimeline,
  unwrapRecord,
} from "./normalizers";
import { requestJson } from "./transport";

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
