import { createHash } from "node:crypto";
import { constants } from "node:fs";
import { link, lstat, mkdir, open, unlink } from "node:fs/promises";
import { isAbsolute, join, normalize, resolve } from "node:path";

import { getCanonicalAppStateDir } from "./appPaths.js";
import {
  resolveLibraryDbPathForState,
} from "./desktopSettings.js";
import type { ApprovedDesktopLibraryBinding } from "./librarySelectionAuthority.js";

export const LIBRARY_BOOTSTRAP_BINDING_SCHEMA_VERSION = "1";
export const LIBRARY_BOOTSTRAP_BINDING_DIRECTORY = "library-bootstrap-bindings";
export const LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES = 4_096;
export const LIBRARY_BOOTSTRAP_BINDING_DIGEST_DOMAIN =
  "memolens.library_bootstrap_candidate_binding.v1";

const REQUEST_ID = /^lb_[0-9a-f]{64}$/;
const LOWER_SHA256 = /^[0-9a-f]{64}$/;
const DECIMAL_IDENTITY = /^(?:0|[1-9][0-9]*)$/;
const ENVELOPE_KEYS = [
  "candidate_binding_sha256",
  "canonical_root",
  "database_path",
  "expected_library_root_device",
  "expected_library_root_inode",
  "intent_created_at_ms",
  "intent_expires_at_ms",
  "kind",
  "native_confirmed_at_ms",
  "request_id",
  "schema_version",
] as const;

export interface LibraryBootstrapIntentReference {
  schema_version: "1";
  kind: "library_bootstrap_intent";
  request_id: string;
  created_at_ms: number;
  expires_at_ms: number;
}

export interface CandidateLibraryBootstrapBindingMaterial {
  canonical_root: string;
  database_path: string;
  expected_library_root_device: string;
  expected_library_root_inode: string;
}

export interface CandidateLibraryBootstrapBindingEnvelope {
  schema_version: "1";
  kind: "library_bootstrap_candidate_binding";
  request_id: string;
  intent_created_at_ms: number;
  intent_expires_at_ms: number;
  native_confirmed_at_ms: number;
  canonical_root: string;
  database_path: string;
  expected_library_root_device: string;
  expected_library_root_inode: string;
  candidate_binding_sha256: string;
}

export interface SealCandidateLibraryBootstrapBindingInput {
  request: LibraryBootstrapIntentReference;
  nativeConfirmedAtMs: number;
  binding: ApprovedDesktopLibraryBinding;
}

export interface SealedCandidateLibraryBootstrapBinding {
  envelope: CandidateLibraryBootstrapBindingEnvelope;
  replayed: boolean;
}

export interface LibraryBootstrapBindingStoreOptions {
  stateDir?: string;
  now?: () => number;
  afterRead?: (filePath: string) => Promise<void>;
}

export class LibraryBootstrapBindingBusyError extends Error {
  constructor() {
    super("MemoLens bootstrap binding publication is already in progress.");
    this.name = "LibraryBootstrapBindingBusyError";
  }
}

function canonicalPath(value: string): string {
  const canonical = normalize(resolve(value)).normalize("NFC");
  return process.platform === "win32" ? canonical.toLowerCase() : canonical;
}

function bindingFilename(requestId: string): string {
  return `${requestId}.binding.json`;
}

function bindingTemporaryFilename(requestId: string): string {
  return `.${bindingFilename(requestId)}.tmp`;
}

function assertRequestId(value: string): void {
  if (!REQUEST_ID.test(value)) {
    throw new Error("MemoLens rejected an invalid Library bootstrap request ID.");
  }
}

function assertNonNegativeSafeInteger(value: number, field: string): void {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`MemoLens rejected an invalid ${field}.`);
  }
}

function assertMaterialShape(material: CandidateLibraryBootstrapBindingMaterial): void {
  if (
    typeof material.canonical_root !== "string"
    || !isAbsolute(material.canonical_root)
    || material.canonical_root !== material.canonical_root.normalize("NFC")
    || canonicalPath(material.canonical_root) !== material.canonical_root
    || typeof material.database_path !== "string"
    || !isAbsolute(material.database_path)
    || material.database_path !== material.database_path.normalize("NFC")
    || canonicalPath(material.database_path) !== material.database_path
    || typeof material.expected_library_root_device !== "string"
    || !DECIMAL_IDENTITY.test(material.expected_library_root_device)
    || typeof material.expected_library_root_inode !== "string"
    || !DECIMAL_IDENTITY.test(material.expected_library_root_inode)
  ) {
    throw new Error("MemoLens rejected an invalid candidate Library binding material.");
  }
}

export function canonicalCandidateBindingJson(
  material: CandidateLibraryBootstrapBindingMaterial,
): string {
  assertMaterialShape(material);
  return JSON.stringify({
    canonical_root: material.canonical_root,
    database_path: material.database_path,
    expected_library_root_device: material.expected_library_root_device,
    expected_library_root_inode: material.expected_library_root_inode,
  });
}

export function computeCandidateBindingSha256(
  material: CandidateLibraryBootstrapBindingMaterial,
): string {
  const digest = createHash("sha256");
  digest.update(LIBRARY_BOOTSTRAP_BINDING_DIGEST_DOMAIN, "ascii");
  digest.update(Buffer.from([0]));
  digest.update(canonicalCandidateBindingJson(material), "utf8");
  return digest.digest("hex");
}

function materialFromEnvelope(
  envelope: CandidateLibraryBootstrapBindingEnvelope,
): CandidateLibraryBootstrapBindingMaterial {
  return {
    canonical_root: envelope.canonical_root,
    database_path: envelope.database_path,
    expected_library_root_device: envelope.expected_library_root_device,
    expected_library_root_inode: envelope.expected_library_root_inode,
  };
}

function buildEnvelope(
  input: SealCandidateLibraryBootstrapBindingInput,
  stateDir: string,
): CandidateLibraryBootstrapBindingEnvelope {
  assertRequestId(input.request.request_id);
  if (
    input.request.schema_version !== "1"
    || input.request.kind !== "library_bootstrap_intent"
  ) {
    throw new Error("MemoLens rejected an invalid Library bootstrap intent contract.");
  }
  assertNonNegativeSafeInteger(input.request.created_at_ms, "bootstrap creation time");
  assertNonNegativeSafeInteger(input.request.expires_at_ms, "bootstrap expiry time");
  assertNonNegativeSafeInteger(input.nativeConfirmedAtMs, "native confirmation time");
  if (
    input.request.expires_at_ms !== input.request.created_at_ms + 300_000
    || input.nativeConfirmedAtMs < input.request.created_at_ms
    || input.nativeConfirmedAtMs >= input.request.expires_at_ms
  ) {
    throw new Error("MemoLens rejected inconsistent Library bootstrap timestamps.");
  }
  const material: CandidateLibraryBootstrapBindingMaterial = {
    canonical_root: input.binding.canonicalRoot,
    database_path: input.binding.dbPath,
    expected_library_root_device: input.binding.device,
    expected_library_root_inode: input.binding.inode,
  };
  assertMaterialShape(material);
  if (material.database_path !== resolveLibraryDbPathForState(material.canonical_root, stateDir)) {
    throw new Error("MemoLens rejected a candidate binding for the wrong database path.");
  }
  return {
    schema_version: LIBRARY_BOOTSTRAP_BINDING_SCHEMA_VERSION,
    kind: "library_bootstrap_candidate_binding",
    request_id: input.request.request_id,
    intent_created_at_ms: input.request.created_at_ms,
    intent_expires_at_ms: input.request.expires_at_ms,
    native_confirmed_at_ms: input.nativeConfirmedAtMs,
    ...material,
    candidate_binding_sha256: computeCandidateBindingSha256(material),
  };
}

function skipWhitespace(raw: string, offset: number): number {
  let cursor = offset;
  while (cursor < raw.length && /[\t\n\r ]/.test(raw[cursor])) cursor += 1;
  return cursor;
}

function parseString(raw: string, offset: number): { value: string; next: number } {
  if (raw[offset] !== '"') throw new Error("MemoLens rejected malformed binding JSON.");
  let cursor = offset + 1;
  while (cursor < raw.length) {
    if (raw[cursor] === "\\") {
      cursor += 2;
      continue;
    }
    if (raw[cursor] === '"') {
      try {
        const value = JSON.parse(raw.slice(offset, cursor + 1)) as unknown;
        if (typeof value !== "string") throw new Error("not string");
        return { value, next: cursor + 1 };
      } catch {
        throw new Error("MemoLens rejected malformed binding JSON.");
      }
    }
    if (raw.charCodeAt(cursor) < 0x20) {
      throw new Error("MemoLens rejected malformed binding JSON.");
    }
    cursor += 1;
  }
  throw new Error("MemoLens rejected malformed binding JSON.");
}

function parsePrimitive(
  raw: string,
  offset: number,
): { value: string | number | boolean | null; next: number } {
  if (raw[offset] === '"') return parseString(raw, offset);
  const token = /^(?:-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null)/
    .exec(raw.slice(offset))?.[0];
  if (token === undefined) throw new Error("MemoLens rejected nested binding JSON.");
  return { value: JSON.parse(token) as number | boolean | null, next: offset + token.length };
}

function parseClosedEnvelope(raw: string): CandidateLibraryBootstrapBindingEnvelope {
  if (Buffer.byteLength(raw, "utf8") > LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES) {
    throw new Error("MemoLens rejected an oversized candidate binding.");
  }
  let cursor = skipWhitespace(raw, 0);
  if (raw[cursor] !== "{") throw new Error("MemoLens rejected non-object binding JSON.");
  cursor = skipWhitespace(raw, cursor + 1);
  const value: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
  const keys = new Set<string>();
  if (raw[cursor] !== "}") {
    while (true) {
      const key = parseString(raw, cursor);
      if (keys.has(key.value)) {
        throw new Error("MemoLens rejected a duplicate decoded binding member.");
      }
      keys.add(key.value);
      cursor = skipWhitespace(raw, key.next);
      if (raw[cursor] !== ":") throw new Error("MemoLens rejected malformed binding JSON.");
      const primitive = parsePrimitive(raw, skipWhitespace(raw, cursor + 1));
      value[key.value] = primitive.value;
      cursor = skipWhitespace(raw, primitive.next);
      if (raw[cursor] === "}") break;
      if (raw[cursor] !== ",") throw new Error("MemoLens rejected malformed binding JSON.");
      cursor = skipWhitespace(raw, cursor + 1);
    }
  }
  cursor = skipWhitespace(raw, cursor + 1);
  if (cursor !== raw.length) throw new Error("MemoLens rejected malformed binding JSON.");
  const actualKeys = Object.keys(value).sort();
  if (
    actualKeys.length !== ENVELOPE_KEYS.length
    || !actualKeys.every((key, index) => key === ENVELOPE_KEYS[index])
  ) {
    throw new Error("MemoLens rejected a non-closed candidate binding envelope.");
  }
  const envelope = value as unknown as CandidateLibraryBootstrapBindingEnvelope;
  validateEnvelope(envelope);
  return envelope;
}

function validateEnvelope(envelope: CandidateLibraryBootstrapBindingEnvelope): void {
  if (
    envelope.schema_version !== LIBRARY_BOOTSTRAP_BINDING_SCHEMA_VERSION
    || envelope.kind !== "library_bootstrap_candidate_binding"
    || typeof envelope.request_id !== "string"
    || !REQUEST_ID.test(envelope.request_id)
    || typeof envelope.candidate_binding_sha256 !== "string"
    || !LOWER_SHA256.test(envelope.candidate_binding_sha256)
  ) {
    throw new Error("MemoLens rejected an invalid candidate binding envelope.");
  }
  assertNonNegativeSafeInteger(envelope.intent_created_at_ms, "binding intent creation time");
  assertNonNegativeSafeInteger(envelope.intent_expires_at_ms, "binding intent expiry time");
  assertNonNegativeSafeInteger(envelope.native_confirmed_at_ms, "binding confirmation time");
  if (
    envelope.intent_expires_at_ms !== envelope.intent_created_at_ms + 300_000
    || envelope.native_confirmed_at_ms < envelope.intent_created_at_ms
    || envelope.native_confirmed_at_ms >= envelope.intent_expires_at_ms
  ) {
    throw new Error("MemoLens rejected inconsistent candidate binding timestamps.");
  }
  const material = materialFromEnvelope(envelope);
  assertMaterialShape(material);
  if (computeCandidateBindingSha256(material) !== envelope.candidate_binding_sha256) {
    throw new Error("MemoLens rejected a candidate binding digest mismatch.");
  }
}

function serializeEnvelope(envelope: CandidateLibraryBootstrapBindingEnvelope): string {
  return `${JSON.stringify({
    candidate_binding_sha256: envelope.candidate_binding_sha256,
    canonical_root: envelope.canonical_root,
    database_path: envelope.database_path,
    expected_library_root_device: envelope.expected_library_root_device,
    expected_library_root_inode: envelope.expected_library_root_inode,
    intent_created_at_ms: envelope.intent_created_at_ms,
    intent_expires_at_ms: envelope.intent_expires_at_ms,
    kind: envelope.kind,
    native_confirmed_at_ms: envelope.native_confirmed_at_ms,
    request_id: envelope.request_id,
    schema_version: envelope.schema_version,
  })}\n`;
}

function sameEnvelope(
  left: CandidateLibraryBootstrapBindingEnvelope,
  right: CandidateLibraryBootstrapBindingEnvelope,
): boolean {
  return serializeEnvelope(left) === serializeEnvelope(right);
}

export class LibraryBootstrapBindingStore {
  private readonly stateDir: string;
  private readonly bindingDir: string;
  private readonly now: () => number;
  private readonly afterRead: ((filePath: string) => Promise<void>) | null;
  private queue: Promise<void> = Promise.resolve();

  constructor(options: LibraryBootstrapBindingStoreOptions = {}) {
    this.stateDir = resolve(options.stateDir ?? getCanonicalAppStateDir());
    this.bindingDir = join(this.stateDir, LIBRARY_BOOTSTRAP_BINDING_DIRECTORY);
    this.now = options.now ?? Date.now;
    this.afterRead = options.afterRead ?? null;
  }

  seal(
    input: SealCandidateLibraryBootstrapBindingInput,
  ): Promise<SealedCandidateLibraryBootstrapBinding> {
    return this.serialized(() => this.sealOnce(input));
  }

  load(requestId: string): Promise<CandidateLibraryBootstrapBindingEnvelope | null> {
    return this.serialized(async () => {
      assertRequestId(requestId);
      await this.ensureDirectory();
      await this.recoverTemporary(requestId);
      return this.readEnvelope(requestId);
    });
  }

  private serialized<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.queue.then(operation, operation);
    this.queue = result.then(() => undefined, () => undefined);
    return result;
  }

  private async sealOnce(
    input: SealCandidateLibraryBootstrapBindingInput,
  ): Promise<SealedCandidateLibraryBootstrapBinding> {
    const desired = buildEnvelope(input, this.stateDir);
    await this.ensureDirectory();
    await this.recoverTemporary(desired.request_id);
    const existing = await this.readEnvelope(desired.request_id);
    if (existing !== null) {
      if (!sameEnvelope(existing, desired)) {
        throw new Error("MemoLens rejected a conflicting permanent bootstrap binding.");
      }
      return { envelope: existing, replayed: true };
    }

    const temporaryPath = join(this.bindingDir, bindingTemporaryFilename(desired.request_id));
    const targetPath = join(this.bindingDir, bindingFilename(desired.request_id));
    const body = serializeEnvelope(desired);
    if (Buffer.byteLength(body, "utf8") > LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES) {
      throw new Error("MemoLens refused to write an oversized candidate binding.");
    }
    let handle;
    try {
      handle = await open(
        temporaryPath,
        constants.O_WRONLY
          | constants.O_CREAT
          | constants.O_EXCL
          | (constants.O_NOFOLLOW ?? 0),
        0o600,
      );
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      await this.recoverTemporary(desired.request_id);
      const winner = await this.readEnvelope(desired.request_id);
      if (winner !== null && sameEnvelope(winner, desired)) {
        return { envelope: winner, replayed: true };
      }
      throw new LibraryBootstrapBindingBusyError();
    }
    try {
      await handle.chmod(0o600);
      await handle.writeFile(body, { encoding: "utf8" });
      await handle.sync();
    } finally {
      await handle.close();
    }
    try {
      await link(temporaryPath, targetPath);
    } catch (error) {
      await unlink(temporaryPath).catch(() => undefined);
      await this.fsyncDirectory();
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const winner = await this.readEnvelope(desired.request_id);
      if (winner === null || !sameEnvelope(winner, desired)) {
        throw new Error("MemoLens rejected a conflicting permanent bootstrap binding.");
      }
      return { envelope: winner, replayed: true };
    }
    await unlink(temporaryPath).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "ENOENT") throw error;
    });
    await this.fsyncDirectory();
    return { envelope: desired, replayed: false };
  }

  private async ensureDirectory(): Promise<void> {
    await mkdir(this.bindingDir, { recursive: true, mode: 0o700 });
    const before = await lstat(this.bindingDir, { bigint: true });
    if (
      !before.isDirectory()
      || before.isSymbolicLink()
      || (Number(before.mode) & 0o777) !== 0o700
    ) {
      throw new Error("MemoLens rejected an unsafe bootstrap binding directory.");
    }
    const handle = await open(
      this.bindingDir,
      constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) | (constants.O_NOFOLLOW ?? 0),
    );
    try {
      const opened = await handle.stat({ bigint: true });
      if (
        !opened.isDirectory()
        || opened.dev !== before.dev
        || opened.ino !== before.ino
        || (Number(opened.mode) & 0o777) !== 0o700
      ) {
        throw new Error("MemoLens rejected a replaced bootstrap binding directory.");
      }
    } finally {
      await handle.close();
    }
  }

  private async recoverTemporary(requestId: string): Promise<void> {
    const temporaryPath = join(this.bindingDir, bindingTemporaryFilename(requestId));
    const targetPath = join(this.bindingDir, bindingFilename(requestId));
    let temporary;
    try {
      temporary = await lstat(temporaryPath, { bigint: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
    if (
      !temporary.isFile()
      || temporary.isSymbolicLink()
      || temporary.size > BigInt(LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES)
      || (Number(temporary.mode) & 0o777) !== 0o600
      || (temporary.nlink !== 1n && temporary.nlink !== 2n)
    ) {
      throw new Error("MemoLens rejected an unsafe bootstrap binding temporary.");
    }
    if (
      temporary.nlink === 1n
      && temporary.mtimeMs > BigInt(Math.max(0, this.now() - 300_000))
    ) {
      throw new LibraryBootstrapBindingBusyError();
    }
    if (temporary.nlink === 2n) {
      const target = await lstat(targetPath, { bigint: true });
      if (
        !target.isFile()
        || target.isSymbolicLink()
        || target.dev !== temporary.dev
        || target.ino !== temporary.ino
        || target.size !== temporary.size
        || target.nlink !== 2n
        || (Number(target.mode) & 0o777) !== 0o600
      ) {
        throw new Error("MemoLens rejected a mismatched bootstrap binding publication.");
      }
    }
    await unlink(temporaryPath);
    await this.fsyncDirectory();
  }

  private async readEnvelope(
    requestId: string,
  ): Promise<CandidateLibraryBootstrapBindingEnvelope | null> {
    const filePath = join(this.bindingDir, bindingFilename(requestId));
    let before;
    try {
      before = await lstat(filePath, { bigint: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
      throw error;
    }
    if (
      !before.isFile()
      || before.isSymbolicLink()
      || before.nlink !== 1n
      || before.size > BigInt(LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES)
      || (Number(before.mode) & 0o777) !== 0o600
    ) {
      throw new Error("MemoLens rejected an unsafe bootstrap binding envelope.");
    }
    const handle = await open(filePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    try {
      const opened = await handle.stat({ bigint: true });
      if (
        !opened.isFile()
        || opened.dev !== before.dev
        || opened.ino !== before.ino
        || opened.size !== before.size
        || opened.mtimeNs !== before.mtimeNs
        || opened.ctimeNs !== before.ctimeNs
        || opened.nlink !== 1n
        || (Number(opened.mode) & 0o777) !== 0o600
      ) {
        throw new Error("MemoLens rejected a replaced bootstrap binding envelope.");
      }
      const buffer = Buffer.alloc(LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES + 1);
      let bytesRead = 0;
      while (bytesRead <= LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES) {
        const next = await handle.read(
          buffer,
          bytesRead,
          LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES + 1 - bytesRead,
          bytesRead,
        );
        if (next.bytesRead === 0) break;
        bytesRead += next.bytesRead;
      }
      if (
        bytesRead > LIBRARY_BOOTSTRAP_BINDING_MAX_BYTES
        || BigInt(bytesRead) !== opened.size
      ) {
        throw new Error("MemoLens rejected an oversized candidate binding.");
      }
      if (this.afterRead !== null) {
        await this.afterRead(filePath);
      }
      const after = await handle.stat({ bigint: true });
      if (
        !after.isFile()
        || after.dev !== opened.dev
        || after.ino !== opened.ino
        || after.size !== opened.size
        || after.mtimeNs !== opened.mtimeNs
        || after.ctimeNs !== opened.ctimeNs
        || after.nlink !== opened.nlink
        || (Number(after.mode) & 0o777) !== (Number(opened.mode) & 0o777)
      ) {
        throw new Error("MemoLens rejected a binding changed while it was read.");
      }
      const envelope = parseClosedEnvelope(buffer.subarray(0, bytesRead).toString("utf8"));
      if (envelope.request_id !== requestId) {
        throw new Error("MemoLens rejected a misplaced candidate binding envelope.");
      }
      if (
        envelope.database_path
        !== resolveLibraryDbPathForState(envelope.canonical_root, this.stateDir)
      ) {
        throw new Error("MemoLens rejected a candidate binding for the wrong database path.");
      }
      return envelope;
    } finally {
      await handle.close();
    }
  }

  private async fsyncDirectory(): Promise<void> {
    const handle = await open(
      this.bindingDir,
      constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) | (constants.O_NOFOLLOW ?? 0),
    );
    try {
      await handle.sync();
    } finally {
      await handle.close();
    }
  }
}
