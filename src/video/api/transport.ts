import { asNullableString, asRecord, asString } from "./normalizers";

interface RequestOptions {
  method?: "GET" | "POST";
  body?: Record<string, unknown>;
  signal?: AbortSignal;
  timeoutMs?: number;
  idempotencyKey?: string;
}

export class VideoApiError extends Error {
  status: number;
  code: string | null;
  retryable: boolean | null;
  field: string | null;

  constructor(message: string, options: {
    status: number;
    code?: string | null;
    retryable?: boolean | null;
    field?: string | null;
  }) {
    super(message);
    this.name = "VideoApiError";
    this.status = options.status;
    this.code = options.code ?? null;
    this.retryable = typeof options.retryable === "boolean" ? options.retryable : null;
    this.field = options.field ?? null;
  }
}

export function cleanBase(apiBase: string): string {
  return apiBase.replace(/\/+$/, "");
}

function createRequestSignal(parent: AbortSignal | undefined, timeoutMs: number): {
  signal: AbortSignal;
  cleanup: () => void;
  timedOut: () => boolean;
} {
  const controller = new AbortController();
  let reachedDeadline = false;
  const abortFromParent = () => controller.abort(parent?.reason);
  if (parent?.aborted) {
    controller.abort(parent.reason);
  } else {
    parent?.addEventListener("abort", abortFromParent, { once: true });
  }
  const timeoutId = window.setTimeout(() => {
    reachedDeadline = true;
    controller.abort(new DOMException("Request timed out", "TimeoutError"));
  }, timeoutMs);
  return {
    signal: controller.signal,
    timedOut: () => reachedDeadline,
    cleanup: () => {
      window.clearTimeout(timeoutId);
      parent?.removeEventListener("abort", abortFromParent);
    },
  };
}

export async function requestJson(
  apiBase: string,
  path: string,
  options: RequestOptions = {},
): Promise<Record<string, unknown>> {
  const { signal, cleanup, timedOut } = createRequestSignal(
    options.signal,
    options.timeoutMs ?? 20_000,
  );
  try {
    const headers: Record<string, string> = {};
    if (options.body) {
      headers["Content-Type"] = "application/json";
    }
    if (options.idempotencyKey) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await fetch(`${cleanBase(apiBase)}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal,
    });
    const payload = asRecord(await response.json().catch(() => ({})));
    const error = asRecord(payload.error);
    const isErrorEnvelope = asString(payload.object) === "error"
      || (Object.keys(error).length > 0 && asString(error.message).length > 0);
    if (!response.ok || isErrorEnvelope) {
      throw new VideoApiError(
        asString(error.message)
          || asString(payload.message)
          || `Request failed with status ${response.status}`,
        {
          status: response.ok ? 502 : response.status,
          code: asNullableString(error.code),
          retryable: typeof error.retryable === "boolean" ? error.retryable : null,
          field: asNullableString(error.field),
        },
      );
    }
    return payload;
  } catch (error) {
    if (timedOut()) {
      throw new DOMException("Request timed out", "TimeoutError");
    }
    throw error;
  } finally {
    cleanup();
  }
}

export function resolveVideoResourceUrl(
  apiBase: string,
  resourceUrl: string | null | undefined,
): string | null {
  if (!resourceUrl) {
    return null;
  }
  try {
    const backend = new URL(`${cleanBase(apiBase)}/`);
    const resource = new URL(resourceUrl, backend);
    return resource.origin === backend.origin ? resource.toString() : null;
  } catch {
    return null;
  }
}
