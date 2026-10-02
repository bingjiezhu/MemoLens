import type {
  CanonicalExportCommandResult,
  CanonicalExportJob,
  CanonicalExportJobResultProof,
  CanonicalExportJobStatus,
  CanonicalExportWorkspaceJobSummary,
  CanonicalExportWorkspaceSummary,
  CanonicalExportPresentation,
  CanonicalExportPresentationEnvelope,
  DesktopCanonicalExportApprovalRequest,
  DesktopCanonicalExportApprovalResult,
  DesktopCanonicalExportExpectedTimelineBinding,
} from "./exportTypes.js";

// Keep these runtime validators local so this pure model remains consumable by
// both the renderer's source-level Node tests and Electron's emitted NodeNext
// graph. The exported contract constants live in exportTypes.ts.
const CANONICAL_EXPORT_PACKAGE_ROLES = [
  "final_video",
  "script",
  "package_manifest",
  "human_usage_list",
  "completion_marker",
] as const;
const CANONICAL_EXPORT_JOB_STATUSES = [
  "requested",
  "rendering",
  "packaging",
  "commit_pending",
  "succeeded",
  "failed",
  "cancelling",
  "cancelled",
  "interrupted",
] as const;

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const PACKAGE_BASENAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
const FORBIDDEN_KEY = /(?:^|_)(?:absolute_)?(?:path|root|directory|token|credential|native_nonce)(?:$|_)/i;

function fail(label: string): never {
  throw new Error(`${label} is malformed.`);
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) fail(label);
  return value as Record<string, unknown>;
}

function closedRecord(
  value: unknown,
  keys: readonly string[],
  label: string,
): Record<string, unknown> {
  const normalized = record(value, label);
  const actual = Object.keys(normalized).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])
  ) fail(label);
  return normalized;
}

function exactString(value: unknown, expected: string, label: string): string {
  if (value !== expected) fail(label);
  return expected;
}

function identifier(value: unknown, label: string): string {
  if (typeof value !== "string" || !IDENTIFIER.test(value)) fail(label);
  return value;
}

function digest(value: unknown, label: string): string {
  if (typeof value !== "string" || !SHA256.test(value)) fail(label);
  return value;
}

function canonicalJsonForSha256(value: unknown, label: string, depth = 0): string {
  if (depth > 32) fail(label);
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || Object.is(value, -0)) fail(label);
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    if (value.length > 100_000) fail(label);
    return `[${value.map((item) => canonicalJsonForSha256(item, label, depth + 1)).join(",")}]`;
  }
  const raw = record(value, label);
  const keys = Object.keys(raw);
  if (keys.length > 64) fail(label);
  return `{${keys.sort().map((key) => (
    `${JSON.stringify(key)}:${canonicalJsonForSha256(raw[key], label, depth + 1)}`
  )).join(",")}}`;
}

export async function canonicalExportSourceBindingsSha256(
  sourceBindings: readonly unknown[],
): Promise<string> {
  if (!Array.isArray(sourceBindings) || sourceBindings.length === 0) {
    return fail("desktop export source bindings");
  }
  const material = new TextEncoder().encode(
    canonicalJsonForSha256(sourceBindings, "desktop export source bindings"),
  );
  const hashed = await globalThis.crypto.subtle.digest("SHA-256", material);
  return [...new Uint8Array(hashed)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function integer(value: unknown, minimum: number, maximum: number, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    fail(label);
  }
  return Number(value);
}

function timestamp(value: unknown, label: string): string {
  if (
    typeof value !== "string"
    || value.length > 64
    || !Number.isFinite(Date.parse(value))
  ) fail(label);
  return value;
}

function nullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : timestamp(value, label);
}

export function isCanonicalExportJobStatus(value: unknown): value is CanonicalExportJobStatus {
  return typeof value === "string"
    && (CANONICAL_EXPORT_JOB_STATUSES as readonly string[]).includes(value);
}

export function isActiveCanonicalExportJobStatus(status: CanonicalExportJobStatus): boolean {
  return [
    "requested",
    "rendering",
    "packaging",
    "commit_pending",
    "cancelling",
  ].includes(status);
}

export type CanonicalExportJobPresentation = {
  phase: "active" | "succeeded" | "failed" | "cancelled" | "interrupted";
  label: string;
  detail: string;
  assertive: boolean;
};

/**
 * Renderer-only wording for the closed canonical export status vocabulary.
 * This deliberately does not infer success from progress or package presence:
 * only the Core-persisted `succeeded` terminal is presented as a committed
 * package. `cancelling` remains active until Core records a terminal state.
 */
export function canonicalExportJobPresentation(
  status: CanonicalExportJobStatus,
): CanonicalExportJobPresentation {
  switch (status) {
    case "requested":
      return {
        phase: "active",
        label: "Queued",
        detail: "The canonical request is recorded and waiting for the local export worker.",
        assertive: false,
      };
    case "rendering":
      return {
        phase: "active",
        label: "Rendering",
        detail: "The fixed Timeline is rendering; no successful package is claimed yet.",
        assertive: false,
      };
    case "packaging":
      return {
        phase: "active",
        label: "Packaging",
        detail: "Artifacts are being assembled; usage is not final until the package commits.",
        assertive: false,
      };
    case "commit_pending":
      return {
        phase: "active",
        label: "Commit pending",
        detail: "Package verification is finishing; success has not been recorded yet.",
        assertive: false,
      };
    case "cancelling":
      return {
        phase: "active",
        label: "Cancelling",
        detail: "Cancellation is in progress and remains non-terminal until Core records its result.",
        assertive: false,
      };
    case "succeeded":
      return {
        phase: "succeeded",
        label: "Export succeeded",
        detail: "The canonical package committed successfully and its usage ledger is final.",
        assertive: false,
      };
    case "failed":
      return {
        phase: "failed",
        label: "Export failed",
        detail: "No successful package is claimed. Review the local service state before retrying.",
        assertive: true,
      };
    case "cancelled":
      return {
        phase: "cancelled",
        label: "Export cancelled",
        detail: "The job reached a cancelled terminal; no successful package or finalized usage is claimed.",
        assertive: false,
      };
    case "interrupted":
      return {
        phase: "interrupted",
        label: "Export interrupted",
        detail: "Recovery is required before another canonical export can be approved.",
        assertive: true,
      };
  }
}

export type CanonicalExportPollObservation =
  | { state: "active"; job: CanonicalExportWorkspaceJobSummary }
  | { state: "terminal"; job: CanonicalExportWorkspaceJobSummary }
  | { state: "pending"; job: null }
  | { state: "unknown"; job: null; reason: "no_new_job" | "expected_job_missing" };

/**
 * Pure observation rule used by the renderer poll loop. An IPC-unknown command
 * may bind only to a newly observed job (or one Core already reports active),
 * while a known submission may bind only to its exact job id. A different
 * terminal is never borrowed as the command's outcome.
 */
export function observeCanonicalExportPoll(input: {
  workspace: CanonicalExportWorkspaceSummary;
  expectedJobId: string | null;
  initialLatestJobId: string | null;
  unknownGraceReached: boolean;
}): CanonicalExportPollObservation {
  const latestJob = input.workspace.latest_job;
  const observedExpectedJob = input.expectedJobId === null
    ? latestJob !== null && (
      latestJob.job_id !== input.initialLatestJobId
      || isActiveCanonicalExportJobStatus(latestJob.status)
    )
    : latestJob?.job_id === input.expectedJobId;
  if (observedExpectedJob && latestJob !== null) {
    return isActiveCanonicalExportJobStatus(latestJob.status)
      ? { state: "active", job: latestJob }
      : { state: "terminal", job: latestJob };
  }
  if (input.unknownGraceReached && input.workspace.state !== "in_progress") {
    return {
      state: "unknown",
      job: null,
      reason: input.expectedJobId === null ? "no_new_job" : "expected_job_missing",
    };
  }
  return { state: "pending", job: null };
}

export function normalizeCanonicalExportPackageBasename(
  value: unknown,
  fallback = "memolens-export",
): string {
  const raw = typeof value === "string" ? value : "";
  const cleaned = raw
    .normalize("NFKC")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .slice(0, 120);
  const safeFallback = fallback
    .normalize("NFKC")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .slice(0, 120) || "memolens-export";
  const result = cleaned || safeFallback;
  if (!PACKAGE_BASENAME.test(result) || result === "." || result === "..") {
    return "memolens-export";
  }
  return result;
}

function strictPackageBasename(value: unknown, label: string): string {
  if (
    typeof value !== "string"
    || !PACKAGE_BASENAME.test(value)
    || value === "."
    || value === ".."
    || normalizeCanonicalExportPackageBasename(value) !== value
  ) fail(label);
  return value;
}

function normalizeBlueprintBinding(value: unknown): CanonicalExportPresentation["timeline_binding"]["blueprint_binding"] {
  const raw = closedRecord(
    value,
    ["revision", "content_sha256", "semantic_sha256", "operation_id"],
    "export.presentation.timeline_binding.blueprint_binding",
  );
  return {
    revision: integer(raw.revision, 1, 1_000_000, "export.presentation.timeline_binding.blueprint_binding.revision"),
    content_sha256: digest(raw.content_sha256, "export.presentation.timeline_binding.blueprint_binding.content_sha256"),
    semantic_sha256: digest(raw.semantic_sha256, "export.presentation.timeline_binding.blueprint_binding.semantic_sha256"),
    operation_id: identifier(raw.operation_id, "export.presentation.timeline_binding.blueprint_binding.operation_id"),
  };
}

function normalizeCoverageBinding(value: unknown): CanonicalExportPresentation["timeline_binding"]["coverage_binding"] {
  const raw = closedRecord(
    value,
    ["revision", "content_sha256", "evidence_manifest_sha256", "operation_id"],
    "export.presentation.timeline_binding.coverage_binding",
  );
  return {
    revision: integer(raw.revision, 1, 1_000_000, "export.presentation.timeline_binding.coverage_binding.revision"),
    content_sha256: digest(raw.content_sha256, "export.presentation.timeline_binding.coverage_binding.content_sha256"),
    evidence_manifest_sha256: digest(raw.evidence_manifest_sha256, "export.presentation.timeline_binding.coverage_binding.evidence_manifest_sha256"),
    operation_id: identifier(raw.operation_id, "export.presentation.timeline_binding.coverage_binding.operation_id"),
  };
}

export function normalizeCanonicalExportPresentation(
  value: unknown,
): CanonicalExportPresentation {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "database_uuid",
      "project_id",
      "timeline_binding",
      "profile",
      "package_roles",
      "duration_ms",
      "aspect_ratio",
      "clip_count",
    ],
    "export.presentation",
  );
  const timeline = closedRecord(
    raw.timeline_binding,
    [
      "revision",
      "revision_sha256",
      "timeline_id",
      "timeline_content_sha256",
      "source_bindings_sha256",
      "blueprint_binding",
      "coverage_binding",
    ],
    "export.presentation.timeline_binding",
  );
  if (
    !Array.isArray(raw.package_roles)
    || raw.package_roles.length !== CANONICAL_EXPORT_PACKAGE_ROLES.length
    || raw.package_roles.some((role, index) => role !== CANONICAL_EXPORT_PACKAGE_ROLES[index])
  ) fail("export.presentation.package_roles");
  if (!(["16:9", "9:16", "1:1", "4:5"] as const).includes(
    raw.aspect_ratio as "16:9",
  )) fail("export.presentation.aspect_ratio");
  return {
    object: exactString(raw.object, "canonical_export.presentation", "export.presentation.object") as "canonical_export.presentation",
    schema_version: exactString(raw.schema_version, "1", "export.presentation.schema_version") as "1",
    database_uuid: identifier(raw.database_uuid, "export.presentation.database_uuid"),
    project_id: identifier(raw.project_id, "export.presentation.project_id"),
    timeline_binding: {
      revision: integer(timeline.revision, 1, 1_000_000, "export.presentation.timeline_binding.revision"),
      revision_sha256: digest(timeline.revision_sha256, "export.presentation.timeline_binding.revision_sha256"),
      timeline_id: identifier(timeline.timeline_id, "export.presentation.timeline_binding.timeline_id"),
      timeline_content_sha256: digest(timeline.timeline_content_sha256, "export.presentation.timeline_binding.timeline_content_sha256"),
      source_bindings_sha256: digest(timeline.source_bindings_sha256, "export.presentation.timeline_binding.source_bindings_sha256"),
      blueprint_binding: normalizeBlueprintBinding(timeline.blueprint_binding),
      coverage_binding: normalizeCoverageBinding(timeline.coverage_binding),
    },
    profile: exactString(raw.profile, "export-1080p", "export.presentation.profile") as "export-1080p",
    package_roles: [...CANONICAL_EXPORT_PACKAGE_ROLES],
    duration_ms: integer(raw.duration_ms, 1, 24 * 60 * 60 * 1_000, "export.presentation.duration_ms"),
    aspect_ratio: raw.aspect_ratio as CanonicalExportPresentation["aspect_ratio"],
    clip_count: integer(raw.clip_count, 1, 100_000, "export.presentation.clip_count"),
  };
}

export function normalizeCanonicalExportPresentationEnvelope(
  value: unknown,
): CanonicalExportPresentationEnvelope {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "database_uuid", "presentation", "presentation_sha256"],
    "export.presentation_envelope",
  );
  const presentation = normalizeCanonicalExportPresentation(raw.presentation);
  const databaseUuid = identifier(
    raw.database_uuid,
    "export.presentation_envelope.database_uuid",
  );
  if (presentation.database_uuid !== databaseUuid) {
    fail("export.presentation_envelope.database_uuid");
  }
  return {
    object: exactString(raw.object, "canonical_export.presentation_envelope", "export.presentation_envelope.object") as "canonical_export.presentation_envelope",
    schema_version: exactString(raw.schema_version, "1", "export.presentation_envelope.schema_version") as "1",
    database_uuid: databaseUuid,
    presentation,
    presentation_sha256: digest(raw.presentation_sha256, "export.presentation_envelope.presentation_sha256"),
  };
}

function normalizeDesktopExpectedTimelineBinding(
  value: unknown,
): DesktopCanonicalExportExpectedTimelineBinding {
  const raw = closedRecord(
    value,
    [
      "revision",
      "revisionSha256",
      "timelineId",
      "timelineContentSha256",
      "sourceBindingsSha256",
    ],
    "desktop export request.expectedTimelineBinding",
  );
  return {
    revision: integer(
      raw.revision,
      1,
      1_000_000,
      "desktop export request.expectedTimelineBinding.revision",
    ),
    revisionSha256: digest(
      raw.revisionSha256,
      "desktop export request.expectedTimelineBinding.revisionSha256",
    ),
    timelineId: identifier(
      raw.timelineId,
      "desktop export request.expectedTimelineBinding.timelineId",
    ),
    timelineContentSha256: digest(
      raw.timelineContentSha256,
      "desktop export request.expectedTimelineBinding.timelineContentSha256",
    ),
    sourceBindingsSha256: digest(
      raw.sourceBindingsSha256,
      "desktop export request.expectedTimelineBinding.sourceBindingsSha256",
    ),
  };
}

function normalizeJobTimelineBinding(value: unknown): CanonicalExportJob["timeline_binding"] {
  const raw = closedRecord(
    value,
    ["revision", "revision_sha256", "timeline_content_sha256", "source_bindings_sha256"],
    "export.command.job.timeline_binding",
  );
  return {
    revision: integer(raw.revision, 1, 1_000_000, "export.command.job.timeline_binding.revision"),
    revision_sha256: digest(raw.revision_sha256, "export.command.job.timeline_binding.revision_sha256"),
    timeline_content_sha256: digest(raw.timeline_content_sha256, "export.command.job.timeline_binding.timeline_content_sha256"),
    source_bindings_sha256: digest(raw.source_bindings_sha256, "export.command.job.timeline_binding.source_bindings_sha256"),
  };
}

function assertRecursivelyPathFree(value: unknown, label: string, depth = 0): void {
  if (depth > 8) fail(label);
  if (value === null || typeof value === "string" || typeof value === "boolean") return;
  if (typeof value === "number" && Number.isFinite(value)) return;
  if (Array.isArray(value)) {
    if (value.length > 256) fail(label);
    value.forEach((item, index) => assertRecursivelyPathFree(item, `${label}[${index}]`, depth + 1));
    return;
  }
  const raw = record(value, label);
  const keys = Object.keys(raw);
  if (keys.length > 64 || keys.some((key) => FORBIDDEN_KEY.test(key))) fail(label);
  for (const key of keys) assertRecursivelyPathFree(raw[key], `${label}.${key}`, depth + 1);
}

function normalizeResult(value: unknown): CanonicalExportJobResultProof | null {
  if (value === null) return null;
  const raw = closedRecord(
    value,
    [
      "revision",
      "revision_sha256",
      "video_sha256",
      "video_size_bytes",
      "video_duration_ms",
      "package_manifest_sha256",
      "usage_sha256",
      "runtime_manifest_sha256",
      "human_usage_sha256",
      "completion_marker_sha256",
      "completed_at",
    ],
    "export.command.job.result",
  );
  return {
    revision: integer(raw.revision, 1, 1_000_000, "export.command.job.result.revision"),
    revision_sha256: digest(raw.revision_sha256, "export.command.job.result.revision_sha256"),
    video_sha256: digest(raw.video_sha256, "export.command.job.result.video_sha256"),
    video_size_bytes: integer(raw.video_size_bytes, 0, Number.MAX_SAFE_INTEGER, "export.command.job.result.video_size_bytes"),
    video_duration_ms: integer(raw.video_duration_ms, 1, 24 * 60 * 60 * 1_000, "export.command.job.result.video_duration_ms"),
    package_manifest_sha256: digest(raw.package_manifest_sha256, "export.command.job.result.package_manifest_sha256"),
    usage_sha256: digest(raw.usage_sha256, "export.command.job.result.usage_sha256"),
    runtime_manifest_sha256: digest(raw.runtime_manifest_sha256, "export.command.job.result.runtime_manifest_sha256"),
    human_usage_sha256: digest(raw.human_usage_sha256, "export.command.job.result.human_usage_sha256"),
    completion_marker_sha256: digest(raw.completion_marker_sha256, "export.command.job.result.completion_marker_sha256"),
    completed_at: timestamp(raw.completed_at, "export.command.job.result.completed_at"),
  };
}

function normalizeJob(value: unknown): CanonicalExportJob {
  const raw = closedRecord(
    value,
    [
      "id",
      "project_id",
      "operation_id",
      "presentation_sha256",
      "request_sha256",
      "timeline_binding",
      "output_binding",
      "status",
      "stage",
      "progress",
      "attempt",
      "cancel_requested",
      "error",
      "created_at",
      "started_at",
      "heartbeat_at",
      "finished_at",
      "result",
    ],
    "export.command.job",
  );
  if (!isCanonicalExportJobStatus(raw.status)) fail("export.command.job.status");
  const output = closedRecord(
    raw.output_binding,
    ["output_root_id", "permission_fingerprint", "package_basename", "profile"],
    "export.command.job.output_binding",
  );
  let normalizedError: CanonicalExportJob["error"] = null;
  if (raw.error !== null) {
    assertRecursivelyPathFree(raw.error, "export.command.job.error");
    const error = closedRecord(
      raw.error,
      ["code", "message", "retryable"],
      "export.command.job.error",
    );
    if (
      typeof error.code !== "string"
      || !IDENTIFIER.test(error.code)
      || typeof error.message !== "string"
      || error.message.length === 0
      || error.message.length > 500
      || typeof error.retryable !== "boolean"
    ) fail("export.command.job.error");
    normalizedError = {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
    };
  }
  const progress = typeof raw.progress === "number" && Number.isFinite(raw.progress)
    && raw.progress >= 0 && raw.progress <= 1
    ? raw.progress
    : fail("export.command.job.progress");
  const stages = [
    "requested",
    "rendering",
    "packaging",
    "commit_pending",
    "completed",
    "failed",
    "cancelling",
    "cancelled",
    "interrupted",
  ] as const;
  if (!stages.includes(raw.stage as typeof stages[number])) fail("export.command.job.stage");
  if (typeof raw.cancel_requested !== "boolean") fail("export.command.job.cancel_requested");
  return {
    id: identifier(raw.id, "export.command.job.id"),
    project_id: identifier(raw.project_id, "export.command.job.project_id"),
    operation_id: identifier(raw.operation_id, "export.command.job.operation_id"),
    presentation_sha256: digest(raw.presentation_sha256, "export.command.job.presentation_sha256"),
    request_sha256: digest(raw.request_sha256, "export.command.job.request_sha256"),
    timeline_binding: normalizeJobTimelineBinding(raw.timeline_binding),
    output_binding: {
      output_root_id: identifier(output.output_root_id, "export.command.job.output_binding.output_root_id"),
      permission_fingerprint: digest(output.permission_fingerprint, "export.command.job.output_binding.permission_fingerprint"),
      package_basename: strictPackageBasename(output.package_basename, "export.command.job.output_binding.package_basename"),
      profile: exactString(output.profile, "export-1080p", "export.command.job.output_binding.profile") as "export-1080p",
    },
    status: raw.status,
    stage: raw.stage as typeof stages[number],
    progress,
    attempt: integer(raw.attempt, 0, 1_000, "export.command.job.attempt"),
    cancel_requested: raw.cancel_requested,
    error: normalizedError,
    created_at: timestamp(raw.created_at, "export.command.job.created_at"),
    started_at: nullableTimestamp(raw.started_at, "export.command.job.started_at"),
    heartbeat_at: nullableTimestamp(raw.heartbeat_at, "export.command.job.heartbeat_at"),
    finished_at: nullableTimestamp(raw.finished_at, "export.command.job.finished_at"),
    result: normalizeResult(raw.result),
  };
}

export function normalizeCanonicalExportCommandResult(
  value: unknown,
): CanonicalExportCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "operation_id", "job"],
    "export.command",
  );
  const job = normalizeJob(raw.job);
  const projectId = identifier(raw.project_id, "export.command.project_id");
  const operationId = identifier(raw.operation_id, "export.command.operation_id");
  if (job.project_id !== projectId || job.operation_id !== operationId) fail("export.command.identity");
  return {
    object: exactString(raw.object, "canonical_export.command_result", "export.command.object") as "canonical_export.command_result",
    schema_version: exactString(raw.schema_version, "1", "export.command.schema_version") as "1",
    project_id: projectId,
    operation_id: operationId,
    job,
  };
}

export function normalizeDesktopCanonicalExportApprovalRequest(
  value: unknown,
): DesktopCanonicalExportApprovalRequest {
  const raw = closedRecord(
    value,
    ["projectId", "databaseUuid", "expectedTimelineBinding", "suggestedPackageName"],
    "desktop export request",
  );
  return {
    projectId: identifier(raw.projectId, "desktop export request.projectId"),
    databaseUuid: identifier(raw.databaseUuid, "desktop export request.databaseUuid"),
    expectedTimelineBinding: normalizeDesktopExpectedTimelineBinding(
      raw.expectedTimelineBinding,
    ),
    suggestedPackageName: normalizeCanonicalExportPackageBasename(raw.suggestedPackageName),
  };
}

export function normalizeDesktopCanonicalExportApprovalResult(
  value: unknown,
): DesktopCanonicalExportApprovalResult {
  const raw = record(value, "desktop export result");
  if (raw.status === "submitted") {
    const closed = closedRecord(raw, ["status", "message", "job"], "desktop export result");
    const job = closedRecord(
      closed.job,
      ["jobId", "status", "packageBasename"],
      "desktop export result.job",
    );
    if (!isCanonicalExportJobStatus(job.status)) fail("desktop export result.job.status");
    if (typeof closed.message !== "string" || closed.message.length > 500) {
      fail("desktop export result.message");
    }
    return {
      status: "submitted",
      message: closed.message,
      job: {
        jobId: identifier(job.jobId, "desktop export result.job.jobId"),
        status: job.status,
        packageBasename: strictPackageBasename(
          job.packageBasename,
          "desktop export result.job.packageBasename",
        ),
      },
    };
  }
  if (
    raw.status !== "cancelled"
    && raw.status !== "failed"
    && raw.status !== "unknown"
  ) fail("desktop export result.status");
  const closed = closedRecord(raw, ["status", "message", "job"], "desktop export result");
  if (closed.job !== null || typeof closed.message !== "string" || closed.message.length > 500) {
    fail("desktop export result");
  }
  return { status: raw.status, message: closed.message, job: null };
}
