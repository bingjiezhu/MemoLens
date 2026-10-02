import type {
  CanonicalUsageSelection,
  CreativeBriefInput,
  CreativeProject,
  CreatorProfileReference,
} from "../types";
import type { AtlasCanonicalImageObservation } from "../../query/types.js";
import {
  hasCanonicalWorkspaceMarker,
  isBlueprintProjectWorkspace,
  normalizeBlueprintWorkspace,
} from "../../blueprint/workspaceModel";
import type { BlueprintProjectWorkspace } from "../../blueprint/workspaceTypes";
import { asRecord, normalizeProject } from "./normalizers";
import { requestJson, VideoApiError } from "./transport";

function blueprintIntegrityError(): VideoApiError {
  return new VideoApiError(
    "Canonical Blueprint workspace failed integrity validation.",
    {
      status: 502,
      code: "blueprint_integrity_error",
      retryable: false,
    },
  );
}

function canonicalSourceKind(value: unknown): "canonical" | "legacy" | "absent" {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "absent";
  const raw = value as Record<string, unknown>;
  if (!Object.prototype.hasOwnProperty.call(raw, "canonical_source")) return "absent";
  if (raw.canonical_source === "creative_blueprint") return "canonical";
  if (raw.canonical_source === "legacy_brief" && raw.object === "creative.project") {
    return "legacy";
  }
  throw blueprintIntegrityError();
}

export async function createCreativeBrief(input: {
  apiBase: string;
  dbPath?: string | null;
  brief: CreativeBriefInput;
  selectedRefs?: string[];
  usageSelection?: CanonicalUsageSelection | null;
  candidateObservations?: AtlasCanonicalImageObservation[];
  creatorProfileRef?: CreatorProfileReference | null;
  appliedProfileFields?: string[];
  signal?: AbortSignal;
  idempotencyKey: string;
}): Promise<CreativeProject> {
  const candidateRefs = input.selectedRefs?.length
    ? input.selectedRefs
    : input.brief.candidate_refs ?? [];
  if (input.usageSelection) {
    const selectionIds = input.usageSelection.candidates.map((candidate) => candidate.id);
    if (
      candidateRefs.length === 0
      || new Set(candidateRefs).size !== candidateRefs.length
      || selectionIds.length !== candidateRefs.length
      || selectionIds.some((id, index) => id !== candidateRefs[index])
    ) {
      throw new VideoApiError(
        "Canonical Usage selection must exactly match the selected candidate references.",
        {
          status: 400,
          code: "invalid_usage_selection",
          retryable: false,
        },
      );
    }
  }
  const candidateObservations = input.candidateObservations ?? [];
  const selectedImageIds = input.usageSelection?.candidates
    .filter((candidate) => candidate.result_type === "image_asset")
    .map((candidate) => {
      if (candidate.id !== candidate.asset_id) {
        throw new VideoApiError(
          "Canonical image candidate IDs must equal their canonical asset IDs.",
          {
            status: 400,
            code: "invalid_candidate_observations",
            retryable: false,
          },
        );
      }
      return candidate.asset_id;
    }) ?? [];
  if (
    candidateObservations.length > 24
    || new Set(candidateObservations.map((value) => value.asset_id)).size
      !== candidateObservations.length
    || candidateObservations.some((value) => !candidateRefs.includes(value.asset_id))
    || selectedImageIds.length !== candidateObservations.length
    || selectedImageIds.some(
      (assetId, index) => candidateObservations[index]?.asset_id !== assetId,
    )
  ) {
    throw new VideoApiError(
      "Canonical image observations must be unique and correspond to selected references.",
      {
        status: 400,
        code: "invalid_candidate_observations",
        retryable: false,
      },
    );
  }
  const payload = await requestJson(input.apiBase, "/v1/creative/briefs", {
    method: "POST",
    body: {
      ...input.brief,
      db_path: input.dbPath || undefined,
      candidate_refs: candidateRefs,
      candidate_observations: candidateObservations.length > 0
        ? candidateObservations
        : undefined,
      usage_selection: input.usageSelection ?? undefined,
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
): Promise<CreativeProject | BlueprintProjectWorkspace> {
  const params = new URLSearchParams();
  if (dbPath) params.set("db_path", dbPath);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  const payload = await requestJson(
    apiBase,
    `/v1/creative/projects/${encodeURIComponent(projectId)}${suffix}`,
    { signal, timeoutMs: 15_000 },
  );
  const resource = payload.project ?? payload;
  const payloadSource = canonicalSourceKind(payload);
  const resourceSource = canonicalSourceKind(resource);
  if (
    payloadSource === "canonical"
    || resourceSource === "canonical"
    || hasCanonicalWorkspaceMarker(payload)
    || hasCanonicalWorkspaceMarker(resource)
    || isBlueprintProjectWorkspace(resource)
  ) {
    try {
      return normalizeBlueprintWorkspace(resource);
    } catch {
      throw blueprintIntegrityError();
    }
  }
  if (resourceSource !== "legacy") {
    throw blueprintIntegrityError();
  }
  return normalizeProject(resource);
}
