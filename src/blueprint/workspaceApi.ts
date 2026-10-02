import { requestJson } from "../video/api/transport";
import {
  normalizeBlueprintRevisionResource,
  normalizeBlueprintWorkspace,
} from "./workspaceModel";
import type {
  BlueprintCurrentRevision,
  BlueprintProjectWorkspace,
  BlueprintRevisionRef,
} from "./workspaceTypes";

function queryString(values: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && String(value).length > 0) {
      params.set(key, String(value));
    }
  });
  return params.size > 0 ? `?${params.toString()}` : "";
}

export function blueprintRestoreIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedHead: BlueprintRevisionRef,
  restoreFrom: BlueprintRevisionRef,
): string {
  return [
    "b2a-restore",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `e${expectedHead.revision}`,
    expectedHead.content_sha256.slice(0, 20),
    `r${restoreFrom.revision}`,
    restoreFrom.content_sha256.slice(0, 20),
  ].join("-");
}

export async function fetchBlueprintWorkspace(input: {
  apiBase: string;
  projectId: string;
  dbPath?: string | null;
  historyLimit?: number;
  signal?: AbortSignal;
}): Promise<BlueprintProjectWorkspace> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}${queryString({
      db_path: input.dbPath,
      history_limit: input.historyLimit ?? 50,
    })}`,
    { signal: input.signal, timeoutMs: 15_000 },
  );
  return normalizeBlueprintWorkspace(payload.project ?? payload);
}

export async function fetchBlueprintRevision(input: {
  apiBase: string;
  projectId: string;
  revision: number;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<BlueprintCurrentRevision> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/blueprint${queryString({
      db_path: input.dbPath,
      revision: input.revision,
    })}`,
    { signal: input.signal, timeoutMs: 15_000 },
  );
  return normalizeBlueprintRevisionResource(payload);
}

export async function restoreBlueprintRevision(input: {
  apiBase: string;
  projectId: string;
  expectedHead: BlueprintRevisionRef;
  restoreFrom: BlueprintRevisionRef;
  expectedDatabaseUuid: string;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<Record<string, unknown>> {
  return requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/blueprint/restore${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_head: input.expectedHead,
        restore_from: input.restoreFrom,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: blueprintRestoreIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedHead,
        input.restoreFrom,
      ),
    },
  );
}
