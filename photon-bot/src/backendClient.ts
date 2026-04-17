import type { BotConfig } from "./config.js";
import type { QueryPhotosInput, RetrievalImage, RetrievalResponse } from "./types.js";

export class BackendClient {
  constructor(
    private readonly config: Pick<
      BotConfig,
      | "backendBaseUrl"
      | "requestTimeoutMs"
      | "imageLibraryDir"
      | "dbPath"
      | "backendSendPathOverrides"
    >,
  ) {}

  async queryPhotos(input: QueryPhotosInput): Promise<RetrievalResponse> {
    const url = `${this.config.backendBaseUrl}/v1/retrieval/query`;
    const payload: Record<string, unknown> = {
      text: input.text,
      top_k: input.topK,
    };

    if (this.config.backendSendPathOverrides) {
      payload.db_path = this.config.dbPath;
      payload.image_library_dir = this.config.imageLibraryDir;
    }

    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(this.config.requestTimeoutMs),
      });
    } catch (error) {
      if (isTimeoutError(error)) {
        throw new Error(
          `Backend query timed out after ${Math.round(this.config.requestTimeoutMs / 1000)} seconds.`,
        );
      }
      throw error;
    }

    const body = await readJson(response);
    if (!response.ok) {
      const detail = typeof body?.message === "string" ? body.message : `HTTP ${response.status}`;
      throw new Error(`Backend query failed: ${detail}`);
    }

    return normalizeRetrievalResponse(body);
  }
}

function isTimeoutError(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" ||
      error.name === "AbortError" ||
      error.message.includes("aborted due to timeout"))
  );
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  const text = await response.text();
  if (!text.trim()) {
    return {};
  }

  try {
    const parsed = JSON.parse(text);
    return isRecord(parsed) ? parsed : {};
  } catch {
    throw new Error("Backend returned invalid JSON.");
  }
}

function normalizeRetrievalResponse(body: Record<string, unknown>): RetrievalResponse {
  const data = Array.isArray(body.data)
    ? body.data.map(normalizeRetrievalImage).filter((item): item is RetrievalImage => item !== null)
    : [];

  return {
    status: typeof body.status === "string" ? body.status : "failed",
    message: typeof body.message === "string" ? body.message : null,
    title: typeof body.title === "string" ? body.title : null,
    caption: typeof body.caption === "string" ? body.caption : null,
    notes: Array.isArray(body.notes)
      ? body.notes.filter((item): item is string => typeof item === "string")
      : [],
    data,
  };
}

function normalizeRetrievalImage(value: unknown): RetrievalImage | null {
  if (!isRecord(value) || typeof value.relative_path !== "string") {
    return null;
  }

  return {
    relative_path: value.relative_path,
    place_name: typeof value.place_name === "string" ? value.place_name : null,
    taken_at: typeof value.taken_at === "string" ? value.taken_at : null,
    description: typeof value.description === "string" ? value.description : null,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
