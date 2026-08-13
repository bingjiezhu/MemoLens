import {
  normalizeCreatorProfileRevision,
  normalizeCreatorSuggestions,
} from "./model";
import type {
  CreatorEvidence,
  CreatorProfileContent,
  CreatorProfileRevision,
  CreatorProfileSource,
  CreatorProfileSuggestion,
} from "./types";
import { requestDeadline } from "./requestDeadline";

export class CreatorApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "CreatorApiError";
    this.status = status;
    this.code = code;
  }
}

function baseUrl(apiBase: string): string {
  return apiBase.replace(/\/+$/, "");
}

function errorPayload(value: unknown): { message: string; code: string | null } {
  const raw = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const nested = raw.error && typeof raw.error === "object"
    ? raw.error as Record<string, unknown>
    : raw;
  return {
    message: typeof nested.message === "string" && nested.message.trim()
      ? nested.message.trim()
      : "Creator Memory request failed.",
    code: typeof nested.code === "string" && nested.code.trim() ? nested.code.trim() : null,
  };
}

async function responseJson(response: Response): Promise<unknown> {
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = errorPayload(payload);
    throw new CreatorApiError(error.message, response.status, error.code);
  }
  return payload;
}

function dbQuery(dbPath: string): string {
  return new URLSearchParams({ db_path: dbPath }).toString();
}

export async function fetchCreatorProfile(
  apiBase: string,
  dbPath: string,
  signal?: AbortSignal,
): Promise<CreatorProfileRevision> {
  const response = await fetch(`${baseUrl(apiBase)}/v1/creator/profile?${dbQuery(dbPath)}`, { signal });
  return normalizeCreatorProfileRevision(await responseJson(response));
}

export async function fetchCreatorSuggestions(
  apiBase: string,
  dbPath: string,
  signal?: AbortSignal,
): Promise<CreatorProfileSuggestion[]> {
  const response = await fetch(
    `${baseUrl(apiBase)}/v1/creator/profile/suggestions?${dbQuery(dbPath)}`,
    { signal },
  );
  return normalizeCreatorSuggestions(await responseJson(response));
}

export async function putCreatorProfile(input: {
  apiBase: string;
  dbPath: string;
  baseRevision: number;
  profile: Partial<CreatorProfileContent>;
  evidence?: CreatorEvidence[];
  source: CreatorProfileSource;
  idempotencyKey: string;
  signal?: AbortSignal;
}): Promise<CreatorProfileRevision> {
  const deadline = requestDeadline(input.signal);
  try {
    const response = await fetch(`${baseUrl(input.apiBase)}/v1/creator/profile`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": input.idempotencyKey,
      },
      body: JSON.stringify({
        db_path: input.dbPath,
        base_revision: input.baseRevision,
        profile: input.profile,
        evidence: input.evidence ?? [],
        source: input.source,
      }),
      signal: deadline.signal,
    });
    return normalizeCreatorProfileRevision(await responseJson(response));
  } finally {
    deadline.cleanup();
  }
}
