import { requestJson } from "../video/api/transport";
import {
  normalizeCanonicalTimelineEdit,
  normalizeCanonicalTimelineEditCommandResult,
  normalizeCanonicalTimelineMaterializeCommandResult,
  normalizeCanonicalTimelineReconcileCommandResult,
  normalizeCanonicalTimelineRestoreCommandResult,
  normalizeCanonicalTimelineStructuralEdit,
  normalizeCanonicalTimelineStructuralEditCommandResult,
  normalizeCanonicalTimelineWorkspace,
} from "./timelineModel";
import type {
  CanonicalTimelineBlueprintBinding,
  CanonicalTimelineCoverageBinding,
  CanonicalTimelineEdit,
  CanonicalTimelineEditCommandResult,
  CanonicalTimelineMaterializeCommandResult,
  CanonicalTimelineHead,
  CanonicalTimelineReconcileCommandResult,
  CanonicalTimelineRestoreCommandResult,
  CanonicalTimelineRestoreFrom,
  CanonicalTimelineStructuralEdit,
  CanonicalTimelineStructuralEditCommandResult,
  CanonicalTimelineWorkspace,
} from "./timelineTypes";

function queryString(values: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && String(value).length > 0) {
      params.set(key, String(value));
    }
  });
  return params.size > 0 ? `?${params.toString()}` : "";
}

export function canonicalTimelineMaterializeIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedBlueprint: CanonicalTimelineBlueprintBinding,
  expectedCoverage: CanonicalTimelineCoverageBinding,
): string {
  return [
    "timeline-materialize",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `b${expectedBlueprint.revision}`,
    expectedBlueprint.content_sha256.slice(0, 16),
    expectedBlueprint.semantic_sha256.slice(0, 16),
    expectedBlueprint.operation_id.slice(0, 20),
    `c${expectedCoverage.revision}`,
    expectedCoverage.content_sha256.slice(0, 16),
    expectedCoverage.evidence_manifest_sha256.slice(0, 16),
    expectedCoverage.operation_id.slice(0, 20),
    "t-none",
  ].join("-");
}

export async function fetchCanonicalTimelineWorkspace(input: {
  apiBase: string;
  projectId: string;
  dbPath?: string | null;
  revision?: number | null;
  signal?: AbortSignal;
}): Promise<CanonicalTimelineWorkspace> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timeline${queryString({
      db_path: input.dbPath,
      revision: input.revision,
    })}`,
    { signal: input.signal, timeoutMs: 15_000 },
  );
  return normalizeCanonicalTimelineWorkspace(payload);
}

export async function materializeCanonicalTimelineFirstCut(input: {
  apiBase: string;
  projectId: string;
  expectedDatabaseUuid: string;
  expectedBlueprint: CanonicalTimelineBlueprintBinding;
  expectedCoverage: CanonicalTimelineCoverageBinding;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<CanonicalTimelineMaterializeCommandResult> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timeline/materialize${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_blueprint: input.expectedBlueprint,
        expected_coverage: input.expectedCoverage,
        expected_timeline_head: null,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: canonicalTimelineMaterializeIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedBlueprint,
        input.expectedCoverage,
      ),
    },
  );
  return normalizeCanonicalTimelineMaterializeCommandResult(payload);
}

export function canonicalTimelineReconcileIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedCoverage: CanonicalTimelineCoverageBinding,
  expectedTimelineHead: CanonicalTimelineHead,
): string {
  return [
    "timeline-reconcile",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `t${expectedTimelineHead.revision}`,
    expectedTimelineHead.revision_sha256,
    `c${expectedCoverage.revision}`,
    expectedCoverage.content_sha256,
  ].join("-");
}

export async function reconcileCanonicalTimelineFromCoverage(input: {
  apiBase: string;
  projectId: string;
  expectedDatabaseUuid: string;
  expectedBlueprint: CanonicalTimelineBlueprintBinding;
  expectedCoverage: CanonicalTimelineCoverageBinding;
  expectedTimelineHead: CanonicalTimelineHead;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<CanonicalTimelineReconcileCommandResult> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timeline/reconcile${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_blueprint: input.expectedBlueprint,
        expected_coverage: input.expectedCoverage,
        expected_timeline_head: input.expectedTimelineHead,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: canonicalTimelineReconcileIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedCoverage,
        input.expectedTimelineHead,
      ),
    },
  );
  return normalizeCanonicalTimelineReconcileCommandResult(payload);
}

function canonicalTimelineEditIdentity(
  edit: CanonicalTimelineEdit,
): readonly (string | number)[] {
  if (edit.op === "move_clip") return [edit.op, edit.clip_id, edit.to_index];
  if (edit.op === "trim_clip") {
    return [edit.op, edit.clip_id, edit.source_in_ms, edit.source_out_ms];
  }
  if (edit.op === "set_clip_duration") {
    return [edit.op, edit.clip_id, edit.duration_ms];
  }
  return [edit.op, edit.clip_id, edit.assignment_id];
}

async function sha256Hex(value: string): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Web Crypto is required to identify a canonical Timeline edit.");
  }
  const hashed = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(
    new Uint8Array(hashed),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function canonicalTimelineEditIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedTimelineHead: CanonicalTimelineHead,
  edit: CanonicalTimelineEdit,
): Promise<string> {
  const digest = await sha256Hex(JSON.stringify([
    "timeline.apply_edit",
    "1",
    expectedDatabaseUuid,
    expectedTimelineHead.revision,
    expectedTimelineHead.revision_sha256,
    canonicalTimelineEditIdentity(edit),
  ]));
  return [
    "timeline-edit",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `t${expectedTimelineHead.revision}`,
    digest,
  ].join("-");
}

export async function applyCanonicalTimelineEdit(input: {
  apiBase: string;
  projectId: string;
  expectedDatabaseUuid: string;
  expectedBlueprint: CanonicalTimelineBlueprintBinding;
  expectedCoverage: CanonicalTimelineCoverageBinding;
  expectedTimelineHead: CanonicalTimelineHead;
  edit: CanonicalTimelineEdit;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<CanonicalTimelineEditCommandResult> {
  const edit = normalizeCanonicalTimelineEdit(input.edit);
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timeline/edit${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_blueprint: input.expectedBlueprint,
        expected_coverage: input.expectedCoverage,
        expected_timeline_head: input.expectedTimelineHead,
        edit,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: await canonicalTimelineEditIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedTimelineHead,
        edit,
      ),
    },
  );
  return normalizeCanonicalTimelineEditCommandResult(payload);
}

function canonicalTimelineStructuralEditIdentity(
  edit: CanonicalTimelineStructuralEdit,
): readonly (string | number)[] {
  return edit.op === "split_clip"
    ? [edit.op, edit.clip_id, edit.source_split_ms]
    : [edit.op, edit.clip_id];
}

export async function canonicalTimelineStructuralEditIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedTimelineHead: CanonicalTimelineHead,
  edit: CanonicalTimelineStructuralEdit,
): Promise<string> {
  const digest = await sha256Hex(JSON.stringify([
    "timeline.apply_structural_edit",
    "1",
    expectedDatabaseUuid,
    expectedTimelineHead.revision,
    expectedTimelineHead.revision_sha256,
    canonicalTimelineStructuralEditIdentity(edit),
  ]));
  return [
    "timeline-structural-edit",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `t${expectedTimelineHead.revision}`,
    digest,
  ].join("-");
}

export async function applyCanonicalTimelineStructuralEdit(input: {
  apiBase: string;
  projectId: string;
  expectedDatabaseUuid: string;
  expectedBlueprint: CanonicalTimelineBlueprintBinding;
  expectedCoverage: CanonicalTimelineCoverageBinding;
  expectedTimelineHead: CanonicalTimelineHead;
  edit: CanonicalTimelineStructuralEdit;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<CanonicalTimelineStructuralEditCommandResult> {
  const structuralEdit = normalizeCanonicalTimelineStructuralEdit(input.edit);
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timeline/structural-edit${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_blueprint: input.expectedBlueprint,
        expected_coverage: input.expectedCoverage,
        expected_timeline_head: input.expectedTimelineHead,
        structural_edit: structuralEdit,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: await canonicalTimelineStructuralEditIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedTimelineHead,
        structuralEdit,
      ),
    },
  );
  return normalizeCanonicalTimelineStructuralEditCommandResult(payload);
}

export async function canonicalTimelineRestoreIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedTimelineHead: CanonicalTimelineHead,
  restoreFrom: CanonicalTimelineRestoreFrom,
): Promise<string> {
  const headIdentity = (head: CanonicalTimelineHead | CanonicalTimelineRestoreFrom) => [
    head.revision,
    head.revision_sha256,
    head.timeline_id,
    head.timeline_content_sha256,
    [
      head.blueprint_binding.revision,
      head.blueprint_binding.content_sha256,
      head.blueprint_binding.semantic_sha256,
      head.blueprint_binding.operation_id,
    ],
    [
      head.coverage_binding.revision,
      head.coverage_binding.content_sha256,
      head.coverage_binding.evidence_manifest_sha256,
      head.coverage_binding.operation_id,
    ],
    head.operation_id,
  ];
  const digest = await sha256Hex(JSON.stringify([
    "timeline.restore_revision",
    "1",
    expectedDatabaseUuid,
    headIdentity(expectedTimelineHead),
    headIdentity(restoreFrom),
  ]));
  return [
    "timeline-restore",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `n${expectedTimelineHead.revision}`,
    `k${restoreFrom.revision}`,
    digest,
  ].join("-");
}

export async function restoreCanonicalTimelineRevision(input: {
  apiBase: string;
  projectId: string;
  expectedDatabaseUuid: string;
  expectedBlueprint: CanonicalTimelineBlueprintBinding;
  expectedCoverage: CanonicalTimelineCoverageBinding;
  expectedTimelineHead: CanonicalTimelineHead;
  restoreFrom: CanonicalTimelineRestoreFrom;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<CanonicalTimelineRestoreCommandResult> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/timeline/restore${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_blueprint: input.expectedBlueprint,
        expected_coverage: input.expectedCoverage,
        expected_timeline_head: input.expectedTimelineHead,
        restore_from: input.restoreFrom,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: await canonicalTimelineRestoreIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedTimelineHead,
        input.restoreFrom,
      ),
    },
  );
  return normalizeCanonicalTimelineRestoreCommandResult(payload);
}
