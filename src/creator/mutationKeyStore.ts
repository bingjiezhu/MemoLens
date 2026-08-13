export type MutationPayload = Record<string, unknown>;

interface PendingMutation {
  fingerprint: string;
  key: string;
  createdAt: number;
  attempt: number;
}

interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, nested]) => nested !== undefined)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, nested]) => [key, canonicalize(nested)]),
    );
  }
  return value;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export function mutationFingerprint(payload: MutationPayload): Promise<string> {
  return sha256(JSON.stringify(canonicalize(payload)));
}

export function shouldRetainMutationKey(error: unknown): boolean {
  if (error instanceof DOMException) return error.name === "TimeoutError" || error.name === "AbortError";
  if (error instanceof TypeError) return true;
  const status = error && typeof error === "object" && "status" in error
    ? Number((error as { status?: unknown }).status)
    : null;
  return status !== null && Number.isFinite(status)
    ? status >= 500 || status === 408 || status === 425 || status === 429
    : true;
}

export class PendingMutationKeyStore {
  private readonly namespace = "memolens.pending-mutation.v1";
  private readonly fallback = new Map<string, PendingMutation>();
  private readonly storage: KeyValueStorage | null;

  constructor(storage: KeyValueStorage | null) {
    this.storage = storage;
  }

  private async storageKey(scope: string): Promise<string> {
    return `${this.namespace}:${await sha256(scope)}`;
  }

  private async read(scope: string): Promise<PendingMutation | null> {
    const fallback = this.fallback.get(scope) ?? null;
    if (!this.storage) return fallback;
    try {
      const raw = this.storage.getItem(await this.storageKey(scope));
      if (!raw) return fallback;
      const parsed = JSON.parse(raw) as Partial<PendingMutation>;
      return typeof parsed.fingerprint === "string"
        && typeof parsed.key === "string"
        && typeof parsed.createdAt === "number"
        && typeof parsed.attempt === "number"
        ? {
            fingerprint: parsed.fingerprint,
            key: parsed.key,
            createdAt: parsed.createdAt,
            attempt: parsed.attempt,
          }
        : fallback;
    } catch {
      return fallback;
    }
  }

  async acquire(scope: string, payload: MutationPayload): Promise<string> {
    const fingerprint = await mutationFingerprint(payload);
    const existing = await this.read(scope);
    if (existing?.fingerprint === fingerprint) {
      const replay = { ...existing, attempt: existing.attempt + 1 };
      this.fallback.set(scope, replay);
      try {
        this.storage?.setItem(await this.storageKey(scope), JSON.stringify(replay));
      } catch {
        // In-memory replay remains deterministic for this app session.
      }
      return replay.key;
    }
    const pending = {
      fingerprint,
      key: `mutation-${crypto.randomUUID()}`,
      createdAt: Date.now(),
      attempt: 1,
    };
    this.fallback.set(scope, pending);
    try {
      this.storage?.setItem(await this.storageKey(scope), JSON.stringify(pending));
    } catch {
      // The in-memory fallback still preserves the key for this app session.
    }
    return pending.key;
  }

  async settle(scope: string, key: string, error?: unknown): Promise<void> {
    const existing = await this.read(scope);
    if (!existing || existing.key !== key || (error && shouldRetainMutationKey(error))) return;
    this.fallback.delete(scope);
    try {
      this.storage?.removeItem(await this.storageKey(scope));
    } catch {
      // A stale persisted entry can only cause safe idempotent replay.
    }
  }
}
