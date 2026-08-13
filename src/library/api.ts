import { normalizeAssetReview, normalizeInboxPage } from "./model";
import { requestDeadline } from "../creator/requestDeadline";
import type {
  AssetReview,
  InboxMediaKind,
  InboxPage,
  InboxState,
  ReviewUpdate,
} from "./types";

export class InboxApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "InboxApiError";
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
      : "Media Inbox request failed.",
    code: typeof nested.code === "string" && nested.code.trim() ? nested.code.trim() : null,
  };
}

async function responseJson(response: Response): Promise<unknown> {
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = errorPayload(payload);
    throw new InboxApiError(error.message, response.status, error.code);
  }
  return payload;
}

export async function fetchInbox(input: {
  apiBase: string;
  dbPath: string;
  state: InboxState;
  kinds: InboxMediaKind[];
  limit?: number;
  cursor?: string | null;
  signal?: AbortSignal;
}): Promise<InboxPage> {
  const params = new URLSearchParams({
    db_path: input.dbPath,
    state: input.state,
    kinds: input.kinds.join(","),
    limit: String(input.limit ?? 48),
  });
  if (input.cursor) params.set("cursor", input.cursor);
  const response = await fetch(`${baseUrl(input.apiBase)}/v1/inbox?${params.toString()}`, {
    signal: input.signal,
  });
  return normalizeInboxPage(await responseJson(response));
}

export async function putAssetReview(input: {
  apiBase: string;
  dbPath: string;
  assetId: string;
  baseRevision: number;
  update: ReviewUpdate;
  idempotencyKey: string;
  signal?: AbortSignal;
}): Promise<AssetReview> {
  const deadline = requestDeadline(input.signal);
  try {
    const response = await fetch(
      `${baseUrl(input.apiBase)}/v1/inbox/assets/${encodeURIComponent(input.assetId)}`,
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": input.idempotencyKey,
        },
        body: JSON.stringify({
          db_path: input.dbPath,
          base_revision: input.baseRevision,
          ...input.update,
        }),
        signal: deadline.signal,
      },
    );
    const payload = await responseJson(response);
    const raw = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
    return normalizeAssetReview(raw.review);
  } finally {
    deadline.cleanup();
  }
}
