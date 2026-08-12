export type MutationJson =
  | null
  | boolean
  | number
  | string
  | MutationJson[]
  | { [key: string]: MutationJson };

export interface MutationStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface MutationIdentity {
  scope: string;
  action: string;
  payload: MutationJson;
}

export interface MutationLease {
  identityHash: string;
  idempotencyKey: string;
  createdAtMs: number;
  lastAttemptAtMs: number;
  attemptCount: number;
}

export type MutationOutcome =
  | { kind: "success" }
  | { kind: "timeout" | "abort" | "network_error" | "request_in_progress" }
  | {
      kind: "http_error";
      status: number;
      code?: string | null;
      retryable?: boolean;
    };

export type MutationDisposition = "cleared" | "retained" | "stale";

interface MutationHttpErrorLike {
  status: number;
  code?: string | null;
  retryable?: boolean | null;
}

interface StoredMutation {
  idempotency_key: string;
  created_at_ms: number;
  last_attempt_at_ms: number;
  attempt_count: number;
}

interface StoredLedger {
  version: 1;
  entries: Record<string, StoredMutation>;
}

export interface VideoMutationLedgerOptions {
  storageKey?: string;
  now?: () => number;
  keyFactory?: () => string;
  hash?: (canonicalIdentity: string) => string | Promise<string>;
}

export const VIDEO_MUTATION_LEDGER_STORAGE_KEY = "memolens.video.mutation-ledger.v1";

const SHA256_RE = /^[0-9a-f]{64}$/;
const IDEMPOTENCY_KEY_RE = /^[\x21-\x7e]{1,200}$/;

function canonicalJson(value: MutationJson, seen: Set<object>): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("Mutation payload numbers must be finite.");
    }
    return JSON.stringify(Object.is(value, -0) ? 0 : value);
  }
  if (typeof value !== "object") {
    throw new TypeError("Mutation payload must contain only JSON values.");
  }
  if (seen.has(value)) {
    throw new TypeError("Mutation payload must not contain cycles.");
  }
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return `[${value.map((item) => canonicalJson(item, seen)).join(",")}]`;
    }
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) {
      throw new TypeError("Mutation payload objects must be plain JSON objects.");
    }
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key], seen)}`)
      .join(",")}}`;
  } finally {
    seen.delete(value);
  }
}

export function canonicalizeMutationIdentity(identity: MutationIdentity): string {
  const scope = identity.scope.trim();
  const action = identity.action.trim();
  if (!scope || scope.length > 256) {
    throw new TypeError("Mutation scope must be 1 to 256 characters.");
  }
  if (!action || action.length > 256) {
    throw new TypeError("Mutation action must be 1 to 256 characters.");
  }
  return canonicalJson({ action, payload: identity.payload, scope }, new Set());
}

async function sha256Hex(value: string): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Web Crypto is required to create a mutation identity hash.");
  }
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function defaultKeyFactory(): string {
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("A cryptographically random UUID generator is required for mutations.");
  }
  return `mlv1-${globalThis.crypto.randomUUID()}`;
}

function isStoredMutation(value: unknown): value is StoredMutation {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    typeof record.idempotency_key === "string"
    && IDEMPOTENCY_KEY_RE.test(record.idempotency_key)
    && typeof record.created_at_ms === "number"
    && Number.isFinite(record.created_at_ms)
    && typeof record.last_attempt_at_ms === "number"
    && Number.isFinite(record.last_attempt_at_ms)
    && typeof record.attempt_count === "number"
    && Number.isInteger(record.attempt_count)
    && record.attempt_count >= 1
  );
}

function parseLedger(raw: string | null): StoredLedger {
  if (raw === null) {
    return { version: 1, entries: {} };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error("The persisted video mutation ledger is not valid JSON.", { cause: error });
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("The persisted video mutation ledger is invalid.");
  }
  const record = parsed as Record<string, unknown>;
  if (record.version !== 1 || !record.entries || typeof record.entries !== "object" || Array.isArray(record.entries)) {
    throw new Error("The persisted video mutation ledger version is unsupported.");
  }
  const entries = record.entries as Record<string, unknown>;
  for (const [identityHash, entry] of Object.entries(entries)) {
    if (!SHA256_RE.test(identityHash) || !isStoredMutation(entry)) {
      throw new Error("The persisted video mutation ledger contains an invalid entry.");
    }
  }
  return { version: 1, entries: entries as Record<string, StoredMutation> };
}

function leaseFrom(identityHash: string, entry: StoredMutation): MutationLease {
  return {
    identityHash,
    idempotencyKey: entry.idempotency_key,
    createdAtMs: entry.created_at_ms,
    lastAttemptAtMs: entry.last_attempt_at_ms,
    attemptCount: entry.attempt_count,
  };
}

export function shouldRetainVideoMutation(outcome: MutationOutcome): boolean {
  if (outcome.kind === "success") {
    return false;
  }
  if (outcome.kind !== "http_error") {
    return true;
  }
  const code = (outcome.code ?? "").trim().toLowerCase();
  if (code === "request_in_progress") {
    return true;
  }
  if (outcome.status === 409 && code === "revision_conflict") {
    return true;
  }
  if (outcome.status >= 500) {
    return true;
  }
  return !(outcome.status >= 400 && outcome.status < 500 && outcome.retryable === false);
}

function isHttpErrorLike(error: unknown): error is MutationHttpErrorLike {
  if (!error || typeof error !== "object") {
    return false;
  }
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" && Number.isFinite(status) && status > 0;
}

/**
 * Convert transport/API failures into the conservative settlement vocabulary.
 * Unknown failures are treated as network ambiguity so a write key is never
 * discarded merely because the client could not classify the response.
 */
export function videoMutationOutcomeFromError(error: unknown): MutationOutcome {
  if (isHttpErrorLike(error)) {
    const code = typeof error.code === "string" ? error.code : null;
    if (code?.trim().toLowerCase() === "request_in_progress") {
      return { kind: "request_in_progress" };
    }
    return {
      kind: "http_error",
      status: error.status,
      code,
      ...(typeof error.retryable === "boolean" ? { retryable: error.retryable } : {}),
    };
  }
  const name = error && typeof error === "object"
    ? (error as { name?: unknown }).name
    : null;
  if (name === "TimeoutError") {
    return { kind: "timeout" };
  }
  if (name === "AbortError") {
    return { kind: "abort" };
  }
  return { kind: "network_error" };
}

export function isAmbiguousVideoMutationOutcome(outcome: MutationOutcome): boolean {
  if (outcome.kind === "timeout" || outcome.kind === "abort" || outcome.kind === "network_error" || outcome.kind === "request_in_progress") {
    return true;
  }
  return outcome.kind === "http_error" && outcome.status >= 500;
}

export function shouldReconcileTimelineMutation(outcome: MutationOutcome): boolean {
  return outcome.kind === "timeout"
    || (outcome.kind === "http_error"
      && outcome.status === 409
      && (outcome.code ?? "").trim().toLowerCase() === "revision_conflict");
}

export class VideoMutationLedger {
  readonly storageKey: string;

  private readonly storage: MutationStorage;
  private readonly now: () => number;
  private readonly keyFactory: () => string;
  private readonly hash: (canonicalIdentity: string) => string | Promise<string>;

  constructor(storage: MutationStorage, options: VideoMutationLedgerOptions = {}) {
    this.storage = storage;
    this.storageKey = options.storageKey ?? VIDEO_MUTATION_LEDGER_STORAGE_KEY;
    this.now = options.now ?? Date.now;
    this.keyFactory = options.keyFactory ?? defaultKeyFactory;
    this.hash = options.hash ?? sha256Hex;
  }

  async acquire(identity: MutationIdentity): Promise<MutationLease> {
    const identityHash = await this.identityHash(identity);
    const ledger = this.load();
    const timestamp = this.now();
    const current = ledger.entries[identityHash];
    if (current) {
      current.last_attempt_at_ms = timestamp;
      current.attempt_count += 1;
      this.save(ledger);
      return leaseFrom(identityHash, current);
    }

    const idempotencyKey = this.keyFactory();
    if (!IDEMPOTENCY_KEY_RE.test(idempotencyKey)) {
      throw new Error("The mutation idempotency key must be 1 to 200 printable ASCII characters.");
    }
    const created: StoredMutation = {
      idempotency_key: idempotencyKey,
      created_at_ms: timestamp,
      last_attempt_at_ms: timestamp,
      attempt_count: 1,
    };
    ledger.entries[identityHash] = created;
    this.save(ledger);
    return leaseFrom(identityHash, created);
  }

  async peek(identity: MutationIdentity): Promise<MutationLease | null> {
    const identityHash = await this.identityHash(identity);
    const entry = this.load().entries[identityHash];
    return entry ? leaseFrom(identityHash, entry) : null;
  }

  settle(lease: MutationLease, outcome: MutationOutcome): MutationDisposition {
    const ledger = this.load();
    const current = ledger.entries[lease.identityHash];
    if (!current || current.idempotency_key !== lease.idempotencyKey) {
      return "stale";
    }
    if (shouldRetainVideoMutation(outcome)) {
      return "retained";
    }
    delete ledger.entries[lease.identityHash];
    this.save(ledger);
    return "cleared";
  }

  private async identityHash(identity: MutationIdentity): Promise<string> {
    const hash = (await this.hash(canonicalizeMutationIdentity(identity))).toLowerCase();
    if (!SHA256_RE.test(hash)) {
      throw new Error("The mutation identity hash must be a SHA-256 hex digest.");
    }
    return hash;
  }

  private load(): StoredLedger {
    return parseLedger(this.storage.getItem(this.storageKey));
  }

  private save(ledger: StoredLedger): void {
    if (Object.keys(ledger.entries).length === 0) {
      this.storage.removeItem(this.storageKey);
      return;
    }
    this.storage.setItem(this.storageKey, JSON.stringify(ledger));
  }
}
