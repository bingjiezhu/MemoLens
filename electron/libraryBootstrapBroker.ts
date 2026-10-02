import { randomUUID } from "node:crypto";
import { constants } from "node:fs";
import {
  link,
  lstat,
  mkdir,
  open,
  readdir,
  unlink,
} from "node:fs/promises";
import { join } from "node:path";

import { getCanonicalAppStateDir } from "./appPaths.js";
import type {
  LibrarySelectionAuthority,
  NativeLibrarySelectionCoordinator,
  VerifiedDesktopLibraryBinding,
} from "./librarySelectionAuthority.js";

export const LIBRARY_BOOTSTRAP_SCHEMA_VERSION = "1";
export const LIBRARY_BOOTSTRAP_TTL_MS = 5 * 60 * 1000;
export const LIBRARY_BOOTSTRAP_MAX_BYTES = 2_048;
export const LIBRARY_BOOTSTRAP_MAX_ENTRIES = 64;
export const LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES = 128;
export const LIBRARY_BOOTSTRAP_SPOOL_NAME = "library-bootstrap-spool";

const REQUEST_ID = /^lb_[0-9a-f]{64}$/;
const OWNED_RECEIPT_TEMP = /^\.(lb_[0-9a-f]{64})\.receipt\.json\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.tmp$/;
const OWNED_REQUEST_TEMP = /^\.(lb_[0-9a-f]{64})\.[0-9a-f]{32}\.tmp$/;
const FINAL_ENTRY = /^(lb_[0-9a-f]{64})\.(request|claim|receipt)\.json$/;
const REQUEST_KEYS = [
  "created_at_ms",
  "expires_at_ms",
  "kind",
  "request_id",
  "schema_version",
] as const;
const RECEIPT_KEYS = [
  "completed_at_ms",
  "expires_at_ms",
  "kind",
  "request_id",
  "schema_version",
  "state",
] as const;

export type BootstrapReceiptState =
  | "library_authority_committed"
  | "library_authority_staged"
  | "native_cancelled"
  | "expired";

export interface LibraryBootstrapRequest {
  schema_version: "1";
  kind: "library_bootstrap_intent";
  request_id: string;
  created_at_ms: number;
  expires_at_ms: number;
}

interface LibraryBootstrapReceipt {
  schema_version: "1";
  kind: "library_bootstrap_receipt";
  request_id: string;
  state: BootstrapReceiptState;
  completed_at_ms: number;
  expires_at_ms: number;
}

export interface LibraryBootstrapBrokerResult {
  object: "memolens.library_bootstrap_broker_result";
  schema_version: "1";
  request_id: string;
  status: BootstrapReceiptState;
  core_library_created: boolean;
  database_created: boolean;
  project_created: boolean;
  scan_started: false;
  editor_ready: false;
  timeline_edit_granted: false;
}

export interface ClaimedBootstrapRequest {
  request: LibraryBootstrapRequest;
  claimName: string;
  expired: boolean;
}

export interface LibraryBootstrapRequestSpoolOptions {
  now?: () => number;
  stateDir?: string;
}

export interface LibraryBootstrapBrokerOptions {
  spool: Pick<
    LibraryBootstrapRequestSpool,
    "claimNext" | "finishClaim"
  >;
  picker: Pick<NativeLibrarySelectionCoordinator, "pick">;
  authority: Pick<LibrarySelectionAuthority, "redeemSelection">;
  resume?(
    claimed: ClaimedBootstrapRequest,
  ): Promise<"library_authority_committed" | null>;
  commit(
    binding: VerifiedDesktopLibraryBinding,
    claimed: ClaimedBootstrapRequest,
  ): Promise<"library_authority_committed" | "library_authority_staged" | void>;
}

function claimName(requestId: string): string {
  return `${requestId}.claim.json`;
}

function receiptName(requestId: string): string {
  return `${requestId}.receipt.json`;
}

function classifyOwnedTemporary(
  name: string,
): { requestId: string; publishedName: string } | null {
  const receipt = OWNED_RECEIPT_TEMP.exec(name);
  if (receipt !== null) {
    return { requestId: receipt[1], publishedName: receiptName(receipt[1]) };
  }
  const request = OWNED_REQUEST_TEMP.exec(name);
  return request === null
    ? null
    : { requestId: request[1], publishedName: `${request[1]}.request.json` };
}

function sortedKeys(value: Record<string, unknown>): string[] {
  return Object.keys(value).sort();
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = sortedKeys(value);
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function skipJsonWhitespace(raw: string, offset: number): number {
  let cursor = offset;
  while (
    cursor < raw.length
    && (raw[cursor] === " "
      || raw[cursor] === "\t"
      || raw[cursor] === "\r"
      || raw[cursor] === "\n")
  ) {
    cursor += 1;
  }
  return cursor;
}

function parseJsonStringToken(
  raw: string,
  offset: number,
): { value: string; next: number } {
  if (raw[offset] !== '"') {
    throw new Error("MemoLens rejected malformed bootstrap JSON.");
  }
  let cursor = offset + 1;
  while (cursor < raw.length) {
    const character = raw[cursor];
    if (character === "\\") {
      cursor += 2;
      continue;
    }
    if (character === '"') {
      const token = raw.slice(offset, cursor + 1);
      try {
        const value = JSON.parse(token) as unknown;
        if (typeof value !== "string") throw new Error("not a string");
        return { value, next: cursor + 1 };
      } catch {
        throw new Error("MemoLens rejected malformed bootstrap JSON.");
      }
    }
    if (character.charCodeAt(0) < 0x20) {
      throw new Error("MemoLens rejected malformed bootstrap JSON.");
    }
    cursor += 1;
  }
  throw new Error("MemoLens rejected malformed bootstrap JSON.");
}

function parseFlatJsonValue(
  raw: string,
  offset: number,
): { value: string | number | boolean | null; next: number } {
  if (raw[offset] === '"') {
    return parseJsonStringToken(raw, offset);
  }
  const tail = raw.slice(offset);
  const primitive = /^(?:-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null)/.exec(tail);
  if (primitive === null) {
    throw new Error("MemoLens rejected malformed or nested bootstrap JSON.");
  }
  const token = primitive[0];
  try {
    return {
      value: JSON.parse(token) as number | boolean | null,
      next: offset + token.length,
    };
  } catch {
    throw new Error("MemoLens rejected malformed bootstrap JSON.");
  }
}

function parseClosedObject(raw: string, expectedKeys: readonly string[]): Record<string, unknown> {
  if (Buffer.byteLength(raw, "utf8") > LIBRARY_BOOTSTRAP_MAX_BYTES) {
    throw new Error("MemoLens bootstrap entry exceeds the bounded size.");
  }
  let cursor = skipJsonWhitespace(raw, 0);
  if (raw[cursor] !== "{") {
    throw new Error("MemoLens rejected a non-object bootstrap entry.");
  }
  cursor = skipJsonWhitespace(raw, cursor + 1);
  const record: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
  const decodedKeys = new Set<string>();
  if (raw[cursor] !== "}") {
    while (true) {
      const key = parseJsonStringToken(raw, cursor);
      if (decodedKeys.has(key.value)) {
        throw new Error("MemoLens rejected a duplicate decoded bootstrap member.");
      }
      decodedKeys.add(key.value);
      cursor = skipJsonWhitespace(raw, key.next);
      if (raw[cursor] !== ":") {
        throw new Error("MemoLens rejected malformed bootstrap JSON.");
      }
      cursor = skipJsonWhitespace(raw, cursor + 1);
      const parsed = parseFlatJsonValue(raw, cursor);
      record[key.value] = parsed.value;
      cursor = skipJsonWhitespace(raw, parsed.next);
      if (raw[cursor] === "}") break;
      if (raw[cursor] !== ",") {
        throw new Error("MemoLens rejected malformed bootstrap JSON.");
      }
      cursor = skipJsonWhitespace(raw, cursor + 1);
    }
  }
  cursor = skipJsonWhitespace(raw, cursor + 1);
  if (cursor !== raw.length) {
    throw new Error("MemoLens rejected malformed bootstrap JSON.");
  }
  if (!exactKeys(record, expectedKeys)) {
    throw new Error("MemoLens rejected a non-closed bootstrap request schema.");
  }
  return record;
}

function parseRequest(raw: string, fileRequestId: string): LibraryBootstrapRequest {
  const value = parseClosedObject(raw, REQUEST_KEYS);
  if (
    value.schema_version !== LIBRARY_BOOTSTRAP_SCHEMA_VERSION
    || value.kind !== "library_bootstrap_intent"
    || typeof value.request_id !== "string"
    || !REQUEST_ID.test(value.request_id)
    || value.request_id !== fileRequestId
    || typeof value.created_at_ms !== "number"
    || !Number.isSafeInteger(value.created_at_ms)
    || value.created_at_ms < 0
    || typeof value.expires_at_ms !== "number"
    || !Number.isSafeInteger(value.expires_at_ms)
    || value.expires_at_ms !== value.created_at_ms + LIBRARY_BOOTSTRAP_TTL_MS
  ) {
    throw new Error("MemoLens rejected an invalid bootstrap request contract.");
  }
  return value as unknown as LibraryBootstrapRequest;
}

function parseReceipt(raw: string, fileRequestId: string): LibraryBootstrapReceipt {
  const value = parseClosedObject(raw, RECEIPT_KEYS);
  if (
    value.schema_version !== LIBRARY_BOOTSTRAP_SCHEMA_VERSION
    || value.kind !== "library_bootstrap_receipt"
    || typeof value.request_id !== "string"
    || !REQUEST_ID.test(value.request_id)
    || value.request_id !== fileRequestId
    || ![
      "library_authority_committed",
      "library_authority_staged",
      "native_cancelled",
      "expired",
    ].includes(String(value.state))
    || typeof value.completed_at_ms !== "number"
    || !Number.isSafeInteger(value.completed_at_ms)
    || value.completed_at_ms < 0
    || typeof value.expires_at_ms !== "number"
    || !Number.isSafeInteger(value.expires_at_ms)
    || value.expires_at_ms < 0
  ) {
    throw new Error("MemoLens rejected an invalid bootstrap receipt contract.");
  }
  return value as unknown as LibraryBootstrapReceipt;
}

function bootstrapResult(
  requestId: string,
  status: BootstrapReceiptState,
): LibraryBootstrapBrokerResult {
  const committed = status === "library_authority_committed";
  return {
    object: "memolens.library_bootstrap_broker_result",
    schema_version: LIBRARY_BOOTSTRAP_SCHEMA_VERSION,
    request_id: requestId,
    status,
    core_library_created: committed,
    database_created: committed,
    project_created: committed,
    scan_started: false,
    editor_ready: false,
    timeline_edit_granted: false,
  };
}

/**
 * Main-process reader for the plugin's path-free app-state intent spool.
 *
 * All data reads are bounded and O_NOFOLLOW.  Names are claimed by atomic
 * no-replace hard-link publication before a native prompt; terminal receipts
 * remain path-free and make replay visible without a Core persistence dependency.
 */
export class LibraryBootstrapRequestSpool {
  private readonly now: () => number;
  private readonly spoolDir: string;

  constructor(options: LibraryBootstrapRequestSpoolOptions = {}) {
    this.now = options.now ?? Date.now;
    this.spoolDir = join(
      options.stateDir ?? getCanonicalAppStateDir(),
      LIBRARY_BOOTSTRAP_SPOOL_NAME,
    );
  }

  async hasPendingRequest(): Promise<boolean> {
    let entries = await this.inspectEntries();
    if (await this.reconcileCompletedEntries(entries)) {
      entries = await this.inspectEntries();
    }
    const terminal = new Set(
      entries
        .filter((entry) => entry.kind === "receipt")
        .map((entry) => entry.receipt.request_id),
    );
    return entries.some((entry) => (
      (entry.kind === "request" || entry.kind === "claim")
      && !terminal.has(entry.request.request_id)
    ));
  }

  async claimNext(): Promise<ClaimedBootstrapRequest | null> {
    while (true) {
      const entries = await this.inspectEntries();
      if (await this.reconcileCompletedEntries(entries)) {
        continue;
      }
      const claims = entries
        .filter((entry): entry is Extract<InspectedEntry, { kind: "claim" }> => (
          entry.kind === "claim"
        ))
        .sort(compareRequests);
      if (claims.length > 0) {
        return {
          request: claims[0].request,
          claimName: claims[0].name,
          expired: claims[0].request.expires_at_ms <= this.now(),
        };
      }
      const requests = entries
        .filter((entry): entry is Extract<InspectedEntry, { kind: "request" }> => (
          entry.kind === "request"
        ))
        .sort(compareRequests);
      if (requests.length === 0) {
        return null;
      }
      const selected = requests[0];
      const targetName = claimName(selected.request.request_id);
      try {
        await link(
          join(this.spoolDir, selected.name),
          join(this.spoolDir, targetName),
        );
        await unlink(join(this.spoolDir, selected.name));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") continue;
        if ((error as NodeJS.ErrnoException).code === "EEXIST") {
          throw new Error("MemoLens rejected a conflicting bootstrap claim.");
        }
        throw error;
      }
      await this.fsyncDirectory();
      const request = await this.readRequest(targetName, selected.request.request_id);
      return {
        request,
        claimName: targetName,
        expired: request.expires_at_ms <= this.now(),
      };
    }
  }

  async finishClaim(
    claimed: ClaimedBootstrapRequest,
    state: BootstrapReceiptState,
  ): Promise<void> {
    const current = await this.readRequest(
      claimed.claimName,
      claimed.request.request_id,
    );
    if (
      current.created_at_ms !== claimed.request.created_at_ms
      || current.expires_at_ms !== claimed.request.expires_at_ms
    ) {
      throw new Error("MemoLens rejected a changed bootstrap claim.");
    }
    const receipt: LibraryBootstrapReceipt = {
      schema_version: LIBRARY_BOOTSTRAP_SCHEMA_VERSION,
      kind: "library_bootstrap_receipt",
      request_id: claimed.request.request_id,
      state,
      completed_at_ms: this.now(),
      expires_at_ms: claimed.request.expires_at_ms,
    };
    await this.atomicWriteNoReplace(
      receiptName(claimed.request.request_id),
      `${JSON.stringify(receipt)}\n`,
    );
    await unlink(join(this.spoolDir, claimed.claimName));
    await this.fsyncDirectory();
  }

  private async ensureSpoolDirectory(): Promise<void> {
    await mkdir(this.spoolDir, { recursive: true, mode: 0o700 });
    const before = await lstat(this.spoolDir, { bigint: true });
    if (!before.isDirectory() || before.isSymbolicLink()) {
      throw new Error("MemoLens rejected an unsafe bootstrap spool directory.");
    }
    if ((Number(before.mode) & 0o777) !== 0o700) {
      throw new Error("MemoLens rejected unsafe bootstrap spool directory permissions.");
    }
    const handle = await open(
      this.spoolDir,
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
        throw new Error("MemoLens rejected a replaced bootstrap spool directory.");
      }
    } finally {
      await handle.close();
    }
  }

  private async inspectEntries(): Promise<InspectedEntry[]> {
    await this.ensureSpoolDirectory();
    const names = await readdir(this.spoolDir);
    if (names.length > LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES) {
      throw new Error("MemoLens bootstrap spool exceeded its bounded raw census.");
    }
    const ownedTemporaryNames: Array<{
      name: string;
      requestId: string;
      publishedName: string;
    }> = [];
    const finalNames: string[] = [];
    for (const name of names) {
      if (name.length > 160) {
        throw new Error("MemoLens rejected an unsafe bootstrap spool entry.");
      }
      const temporary = classifyOwnedTemporary(name);
      if (temporary !== null) {
        ownedTemporaryNames.push({ name, ...temporary });
        continue;
      }
      if (!FINAL_ENTRY.test(name)) {
        throw new Error("MemoLens rejected an unknown bootstrap spool entry.");
      }
      finalNames.push(name);
    }
    const terminalRequestIds = new Set(finalNames.flatMap((name) => {
      const match = FINAL_ENTRY.exec(name);
      return match?.[2] === "receipt" ? [match[1]] : [];
    }));
    const finalNameSet = new Set(finalNames);
    let removedOwnedTemporary = false;
    for (const temporary of ownedTemporaryNames) {
      const publishedTargetName = finalNameSet.has(temporary.publishedName)
        ? temporary.publishedName
        : null;
      removedOwnedTemporary = await this.removeOwnedTemporaryArtifact(
        temporary.name,
        publishedTargetName !== null || terminalRequestIds.has(temporary.requestId),
        publishedTargetName,
      ) || removedOwnedTemporary;
    }
    if (removedOwnedTemporary) await this.fsyncDirectory();
    if (finalNames.length > LIBRARY_BOOTSTRAP_MAX_ENTRIES) {
      throw new Error("MemoLens bootstrap spool exceeded its bounded census.");
    }
    const entries: InspectedEntry[] = [];
    for (const name of finalNames) {
      const match = FINAL_ENTRY.exec(name);
      if (match === null) {
        throw new Error("MemoLens rejected an unknown bootstrap spool entry.");
      }
      const requestId = match[1];
      const kind = match[2] as "request" | "claim" | "receipt";
      if (kind === "receipt") {
        const receipt = await this.readReceipt(name, requestId);
        entries.push({ kind, name, receipt });
      } else {
        const request = await this.readRequest(name, requestId);
        entries.push({ kind, name, request });
      }
    }
    const grouped = new Map<string, InspectedEntry[]>();
    for (const entry of entries) {
      const requestId = entry.kind === "receipt"
        ? entry.receipt.request_id
        : entry.request.request_id;
      const group = grouped.get(requestId) ?? [];
      group.push(entry);
      grouped.set(requestId, group);
    }
    for (const group of grouped.values()) {
      const receipt = group.find((entry) => entry.kind === "receipt");
      if (receipt !== undefined) {
        for (const entry of group) {
          if (
            entry.kind !== "receipt"
            && entry.request.expires_at_ms !== receipt.receipt.expires_at_ms
          ) {
            throw new Error("MemoLens rejected conflicting bootstrap expiry data.");
          }
        }
        // A closed terminal receipt wins over same-ID pre-terminal artifacts
        // left by a crash after receipt publication.  The next drain removes
        // those artifacts before considering more work.
        continue;
      }
      const request = group.find((entry) => entry.kind === "request");
      const claim = group.find((entry) => entry.kind === "claim");
      if (
        request?.kind === "request"
        && claim?.kind === "claim"
        && JSON.stringify(request.request) !== JSON.stringify(claim.request)
      ) {
        throw new Error("MemoLens rejected conflicting bootstrap request and claim data.");
      }
    }
    return entries;
  }

  private async removeOwnedTemporaryArtifact(
    name: string,
    hasPublishedProof: boolean,
    publishedTargetName: string | null,
  ): Promise<boolean> {
    const filePath = join(this.spoolDir, name);
    let before;
    try {
      before = await lstat(filePath, { bigint: true });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
      throw error;
    }
    if (
      !before.isFile()
      || before.isSymbolicLink()
      || before.size > BigInt(LIBRARY_BOOTSTRAP_MAX_BYTES)
      || (Number(before.mode) & 0o777) !== 0o600
      || (before.nlink !== 1n && !(publishedTargetName !== null && before.nlink === 2n))
    ) {
      throw new Error("MemoLens rejected an unsafe bootstrap temporary artifact.");
    }
    if (before.nlink === 2n) {
      const published = await lstat(join(this.spoolDir, publishedTargetName!), { bigint: true });
      if (
        !published.isFile()
        || published.isSymbolicLink()
        || published.dev !== before.dev
        || published.ino !== before.ino
        || published.size !== before.size
        || published.mtimeMs !== before.mtimeMs
        || published.nlink !== before.nlink
        || (Number(published.mode) & 0o777) !== 0o600
      ) {
        throw new Error("MemoLens rejected a mismatched published bootstrap temporary artifact.");
      }
    }
    const staleBeforeMs = BigInt(Math.max(0, this.now() - LIBRARY_BOOTSTRAP_TTL_MS));
    if (!hasPublishedProof && before.mtimeMs > staleBeforeMs) {
      return false;
    }
    const handle = await open(filePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    try {
      const opened = await handle.stat({ bigint: true });
      if (
        !opened.isFile()
        || opened.dev !== before.dev
        || opened.ino !== before.ino
        || opened.size !== before.size
        || opened.mtimeMs !== before.mtimeMs
        || opened.size > BigInt(LIBRARY_BOOTSTRAP_MAX_BYTES)
        || (Number(opened.mode) & 0o777) !== 0o600
        || (opened.nlink !== 1n && !(publishedTargetName !== null && opened.nlink === 2n))
      ) {
        throw new Error("MemoLens rejected a replaced bootstrap temporary artifact.");
      }
      let current;
      try {
        current = await lstat(filePath, { bigint: true });
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
        throw error;
      }
      if (
        current.dev !== opened.dev
        || current.ino !== opened.ino
        || current.size !== opened.size
        || current.mtimeMs !== opened.mtimeMs
        || (Number(current.mode) & 0o777) !== 0o600
        || current.nlink !== opened.nlink
      ) {
        throw new Error("MemoLens rejected a replaced bootstrap temporary artifact.");
      }
      if (opened.nlink === 2n) {
        const published = await lstat(join(this.spoolDir, publishedTargetName!), { bigint: true });
        if (
          published.dev !== opened.dev
          || published.ino !== opened.ino
          || published.nlink !== opened.nlink
        ) {
          throw new Error("MemoLens rejected a replaced published bootstrap artifact.");
        }
      }
      try {
        await unlink(filePath);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
        throw error;
      }
      return true;
    } finally {
      await handle.close();
    }
  }

  private async reconcileCompletedEntries(entries: InspectedEntry[]): Promise<boolean> {
    const terminal = new Set(
      entries
        .filter((entry) => entry.kind === "receipt")
        .map((entry) => entry.receipt.request_id),
    );
    let changed = false;
    for (const entry of entries) {
      if (entry.kind === "receipt") continue;
      if (terminal.has(entry.request.request_id)) {
        await unlink(join(this.spoolDir, entry.name));
        changed = true;
      }
    }
    if (changed) {
      await this.fsyncDirectory();
      return true;
    }
    const claims = new Map(
      entries
        .filter((entry): entry is Extract<InspectedEntry, { kind: "claim" }> => (
          entry.kind === "claim"
        ))
        .map((entry) => [entry.request.request_id, entry]),
    );
    for (const entry of entries) {
      if (entry.kind !== "request") continue;
      const claim = claims.get(entry.request.request_id);
      if (claim !== undefined) {
        await unlink(join(this.spoolDir, entry.name));
        changed = true;
      }
    }
    if (changed) await this.fsyncDirectory();
    return changed;
  }

  private async readRequest(name: string, requestId: string): Promise<LibraryBootstrapRequest> {
    return parseRequest(await this.readBoundedRegularFile(name), requestId);
  }

  private async readReceipt(name: string, requestId: string): Promise<LibraryBootstrapReceipt> {
    return parseReceipt(await this.readBoundedRegularFile(name), requestId);
  }

  private async readBoundedRegularFile(name: string): Promise<string> {
    const filePath = join(this.spoolDir, name);
    const before = await lstat(filePath, { bigint: true });
    if (
      !before.isFile()
      || before.isSymbolicLink()
      || before.size > BigInt(LIBRARY_BOOTSTRAP_MAX_BYTES)
      || (Number(before.mode) & 0o777) !== 0o600
    ) {
      if (before.size > BigInt(LIBRARY_BOOTSTRAP_MAX_BYTES)) {
        throw new Error("MemoLens bootstrap entry exceeds the bounded size.");
      }
      throw new Error("MemoLens rejected an unsafe bootstrap spool entry.");
    }
    const handle = await open(filePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    try {
      const opened = await handle.stat({ bigint: true });
      if (
        !opened.isFile()
        || opened.dev !== before.dev
        || opened.ino !== before.ino
        || opened.size > BigInt(LIBRARY_BOOTSTRAP_MAX_BYTES)
        || (Number(opened.mode) & 0o777) !== 0o600
      ) {
        throw new Error("MemoLens rejected a replaced bootstrap spool entry.");
      }
      const buffer = Buffer.alloc(LIBRARY_BOOTSTRAP_MAX_BYTES + 1);
      const { bytesRead } = await handle.read(
        buffer,
        0,
        LIBRARY_BOOTSTRAP_MAX_BYTES + 1,
        0,
      );
      if (bytesRead > LIBRARY_BOOTSTRAP_MAX_BYTES) {
        throw new Error("MemoLens bootstrap entry exceeds the bounded size.");
      }
      return buffer.subarray(0, bytesRead).toString("utf8");
    } finally {
      await handle.close();
    }
  }

  private async atomicWriteNoReplace(name: string, body: string): Promise<void> {
    if (Buffer.byteLength(body, "utf8") > LIBRARY_BOOTSTRAP_MAX_BYTES) {
      throw new Error("MemoLens refused to write an oversized bootstrap receipt.");
    }
    await this.ensureSpoolDirectory();
    const temporaryName = `.${name}.${randomUUID()}.tmp`;
    const temporaryPath = join(this.spoolDir, temporaryName);
    const targetPath = join(this.spoolDir, name);
    const handle = await open(
      temporaryPath,
      constants.O_WRONLY
        | constants.O_CREAT
        | constants.O_EXCL
        | (constants.O_NOFOLLOW ?? 0),
      0o600,
    );
    try {
      await handle.chmod(0o600);
      await handle.writeFile(body, { encoding: "utf8" });
      await handle.sync();
    } finally {
      await handle.close();
    }
    try {
      await link(temporaryPath, targetPath);
      await unlink(temporaryPath).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
      });
      await this.fsyncDirectory();
    } catch (error) {
      await unlink(temporaryPath).catch(() => undefined);
      if ((error as NodeJS.ErrnoException).code === "EEXIST") {
        throw new Error("MemoLens rejected a conflicting bootstrap receipt.");
      }
      throw error;
    }
  }

  private async fsyncDirectory(): Promise<void> {
    const handle = await open(
      this.spoolDir,
      constants.O_RDONLY | (constants.O_DIRECTORY ?? 0) | (constants.O_NOFOLLOW ?? 0),
    );
    try {
      await handle.sync();
    } finally {
      await handle.close();
    }
  }
}

type InspectedEntry =
  | { kind: "request"; name: string; request: LibraryBootstrapRequest }
  | { kind: "claim"; name: string; request: LibraryBootstrapRequest }
  | { kind: "receipt"; name: string; receipt: LibraryBootstrapReceipt };

function compareRequests(
  left: { request: LibraryBootstrapRequest },
  right: { request: LibraryBootstrapRequest },
): number {
  return left.request.created_at_ms - right.request.created_at_ms
    || left.request.request_id.localeCompare(right.request.request_id);
}

/** Coordinates one path-free intent with native selection and a durable commit callback. */
export class LibraryBootstrapBroker {
  private inFlight: Promise<LibraryBootstrapBrokerResult | null> | null = null;

  constructor(private readonly options: LibraryBootstrapBrokerOptions) {}

  handleNext(): Promise<LibraryBootstrapBrokerResult | null> {
    if (this.inFlight !== null) return this.inFlight;
    this.inFlight = this.handleNextOnce().finally(() => {
      this.inFlight = null;
    });
    return this.inFlight;
  }

  private async handleNextOnce(): Promise<LibraryBootstrapBrokerResult | null> {
    const claimed = await this.options.spool.claimNext();
    if (claimed === null) return null;
    const resumed = await this.options.resume?.(claimed) ?? null;
    if (resumed === "library_authority_committed") {
      await this.options.spool.finishClaim(claimed, resumed);
      return bootstrapResult(claimed.request.request_id, resumed);
    }
    if (claimed.expired) {
      await this.options.spool.finishClaim(claimed, "expired");
      return null;
    }
    const selection = await this.options.picker.pick();
    if (selection === null) {
      await this.options.spool.finishClaim(claimed, "native_cancelled");
      return bootstrapResult(claimed.request.request_id, "native_cancelled");
    }
    const verified = await this.options.authority.redeemSelection(
      selection.selectionTicket,
    );
    try {
      await verified.verifyCurrentIdentity();
      const committed = await this.options.commit(verified, claimed);
      const state = committed === "library_authority_committed"
        ? committed
        : "library_authority_staged";
      await this.options.spool.finishClaim(claimed, state);
      return bootstrapResult(claimed.request.request_id, state);
    } finally {
      await verified.release();
    }
  }
}
