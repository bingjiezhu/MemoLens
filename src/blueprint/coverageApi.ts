import { requestJson } from "../video/api/transport";
import {
  normalizeCoverageMaterializeCommandResult,
  normalizeCoverageWorkspace,
} from "./coverageModel";
import type {
  CoverageMaterializeCommandResult,
  CoveragePlanWorkspace,
} from "./coverageTypes";
import type {
  BlueprintCoverageBinding,
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

export function coverageMaterializeIdempotencyKey(
  expectedDatabaseUuid: string,
  expectedBlueprint: BlueprintCoverageBinding,
  expectedPlanHead: BlueprintRevisionRef | null,
): string {
  const headToken = expectedPlanHead === null
    ? "p-none"
    : `p${expectedPlanHead.revision}-${expectedPlanHead.content_sha256.slice(0, 20)}`;
  return [
    "coverage-materialize",
    expectedDatabaseUuid.replaceAll("-", "").slice(0, 12),
    `b${expectedBlueprint.revision}`,
    expectedBlueprint.content_sha256.slice(0, 20),
    expectedBlueprint.semantic_sha256.slice(0, 20),
    headToken,
  ].join("-");
}

export async function fetchCoverageWorkspace(input: {
  apiBase: string;
  projectId: string;
  dbPath?: string | null;
  revision?: number | null;
  signal?: AbortSignal;
}): Promise<CoveragePlanWorkspace> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/coverage${queryString({
      db_path: input.dbPath,
      revision: input.revision,
    })}`,
    { signal: input.signal, timeoutMs: 15_000 },
  );
  return normalizeCoverageWorkspace(payload);
}

export async function materializeCoverageBaseline(input: {
  apiBase: string;
  projectId: string;
  expectedDatabaseUuid: string;
  expectedBlueprint: BlueprintCoverageBinding;
  expectedPlanHead: BlueprintRevisionRef | null;
  dbPath?: string | null;
  signal?: AbortSignal;
}): Promise<CoverageMaterializeCommandResult> {
  const payload = await requestJson(
    input.apiBase,
    `/v1/creative/projects/${encodeURIComponent(input.projectId)}/coverage/materialize${queryString({
      db_path: input.dbPath,
      expected_database_uuid: input.expectedDatabaseUuid,
    })}`,
    {
      method: "POST",
      body: {
        expected_blueprint: input.expectedBlueprint,
        expected_plan_head: input.expectedPlanHead,
      },
      signal: input.signal,
      timeoutMs: 20_000,
      idempotencyKey: coverageMaterializeIdempotencyKey(
        input.expectedDatabaseUuid,
        input.expectedBlueprint,
        input.expectedPlanHead,
      ),
    },
  );
  return normalizeCoverageMaterializeCommandResult(payload);
}
