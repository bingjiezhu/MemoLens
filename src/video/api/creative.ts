import type {
  CreativeBriefInput,
  CreativeProject,
  CreatorProfileReference,
} from "../types";
import { asRecord, normalizeProject } from "./normalizers";
import { requestJson } from "./transport";

export async function createCreativeBrief(input: {
  apiBase: string;
  dbPath?: string | null;
  brief: CreativeBriefInput;
  selectedRefs?: string[];
  creatorProfileRef?: CreatorProfileReference | null;
  appliedProfileFields?: string[];
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
      creator_profile_ref: input.creatorProfileRef ?? undefined,
      applied_profile_fields: input.creatorProfileRef
        ? input.appliedProfileFields ?? []
        : undefined,
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
