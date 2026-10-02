import { createHmac, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";

import type {
  DesktopBackendStatus,
  DesktopSettings,
} from "../src/query/types.js";
import {
  BackendProcessSupervisor,
  isExactSafeSQLiteHealthCapability,
  type SQLiteHealthCapability,
} from "./backendProcessSupervisor.js";
import { getCanonicalAppStateDir } from "./appPaths.js";
import { DEFAULT_BACKEND_URL } from "./desktopSettings.js";
import { guardedFetch } from "./networkPolicy.js";

const EXPECTED_BACKEND_SERVICE = "memolens-backend";
const EXPECTED_API_VERSION = "1";
const MAX_HEALTH_RESPONSE_BYTES = 8_192;
const LIBRARY_BOOTSTRAP_REQUEST_ID = /^lb_[0-9a-f]{64}$/;
const LOWER_SHA256 = /^[0-9a-f]{64}$/;
const LOWER_UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const RUNTIME_GENERATION_ID = /^runtime_generation_[0-9a-f]{64}$/;
let desktopSessionToken = randomBytes(32).toString("hex");
let mainAuthorityToken = randomBytes(32).toString("hex");
let runtimeAuthorityEpoch = randomUUID();

let backendIdentityVerified = false;

export interface LibraryBootstrapRuntimeIdentity {
  mode: "bootstrap_candidate" | "active";
  bootstrap_request_id: string | null;
  candidate_binding_sha256: string | null;
  database_uuid: string | null;
  schema_version: number | null;
  runtime_generation_id: string | null;
}

export interface LibraryBootstrapRuntimeExpectation {
  mode: "bootstrap_candidate" | "active";
  requestId: string;
  candidateBindingSha256: string;
}

export interface LibraryBootstrapCommitResponse {
  object: "memolens.library_bootstrap_result";
  schema_version: "1";
  request_id: string;
  status: "library_authority_committed";
  scan_started: false;
  scan_stage: "awaiting_scan_worker";
  editor_state: "awaiting_grounded_timeline";
  blueprint_authority: "unverified";
  timeline_edit_granted: false;
}

export interface LibraryBootstrapCommitResult {
  response: LibraryBootstrapCommitResponse;
  replayed: boolean;
}

function updateBackendTrust(trusted: boolean): void {
  backendIdentityVerified = trusted;
}

function rotateRuntimeCredentials(): void {
  desktopSessionToken = randomBytes(32).toString("hex");
  mainAuthorityToken = randomBytes(32).toString("hex");
  runtimeAuthorityEpoch = randomUUID();
  updateBackendTrust(false);
}

export function getDesktopSessionToken(): string {
  return desktopSessionToken;
}

/**
 * Main-process-only issuer credential.  Never expose this value through the
 * preload bridge, renderer request injection, CORS, diagnostics, or logs.
 */
export function getMainAuthorityToken(): string {
  return mainAuthorityToken;
}

/**
 * Binds every short-lived Agent capability to this managed backend runtime.
 */
export function getRuntimeAuthorityEpoch(): string {
  return runtimeAuthorityEpoch;
}

export function isBackendIdentityVerified(): boolean {
  return backendIdentityVerified;
}

export function revokeBackendTrust(): void {
  rotateRuntimeCredentials();
}

export function markBackendTrustVerified(): void {
  updateBackendTrust(true);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

function sameSQLiteHealthCapability(
  left: SQLiteHealthCapability,
  right: SQLiteHealthCapability,
): boolean {
  return left.policy_id === right.policy_id
    && left.sqlite_version === right.sqlite_version
    && left.wal_reset_safe === right.wal_reset_safe
    && left.journal_policy === right.journal_policy;
}

function backendHealthProofMessage(
  challenge: string,
  health: Record<string, unknown>,
  runtime: SQLiteHealthCapability,
): string {
  return [
    "memolens.health.v1",
    `challenge=${challenge}`,
    `object=${health.object}`,
    `status=${health.status}`,
    `service=${health.service}`,
    `api_version=${health.api_version}`,
    `policy_id=${runtime.policy_id}`,
    `sqlite_version=${runtime.sqlite_version}`,
    "wal_reset_safe=true",
    `journal_policy=${runtime.journal_policy}`,
  ].join("\n");
}

function healthValue(value: string | number | null): string {
  return value === null ? "null" : String(value);
}

function backendHealthV2ProofMessage(
  challenge: string,
  health: Record<string, unknown>,
  sqliteRuntime: SQLiteHealthCapability,
  runtime: LibraryBootstrapRuntimeIdentity,
): string {
  return [
    "memolens.health.v2",
    `challenge=${challenge}`,
    `object=${health.object}`,
    `status=${health.status}`,
    `service=${health.service}`,
    `api_version=${health.api_version}`,
    "schema_version=2",
    `policy_id=${sqliteRuntime.policy_id}`,
    `sqlite_version=${sqliteRuntime.sqlite_version}`,
    `wal_reset_safe=${sqliteRuntime.wal_reset_safe ? "true" : "false"}`,
    `journal_policy=${sqliteRuntime.journal_policy}`,
    `runtime_mode=${runtime.mode}`,
    `bootstrap_request_id=${healthValue(runtime.bootstrap_request_id)}`,
    `candidate_binding_sha256=${healthValue(runtime.candidate_binding_sha256)}`,
    `database_uuid=${healthValue(runtime.database_uuid)}`,
    `runtime_schema_version=${healthValue(runtime.schema_version)}`,
    `runtime_generation_id=${healthValue(runtime.runtime_generation_id)}`,
  ].join("\n");
}

function parseRuntimeIdentity(value: unknown): LibraryBootstrapRuntimeIdentity | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "mode",
    "bootstrap_request_id",
    "candidate_binding_sha256",
    "database_uuid",
    "schema_version",
    "runtime_generation_id",
  ])) {
    return null;
  }
  if (value.mode === "bootstrap_candidate") {
    if (
      typeof value.bootstrap_request_id !== "string"
      || !LIBRARY_BOOTSTRAP_REQUEST_ID.test(value.bootstrap_request_id)
      || typeof value.candidate_binding_sha256 !== "string"
      || !LOWER_SHA256.test(value.candidate_binding_sha256)
      || value.database_uuid !== null
      || value.schema_version !== null
      || value.runtime_generation_id !== null
    ) {
      return null;
    }
  } else if (value.mode === "active") {
    const bootstrapPairIsNull = value.bootstrap_request_id === null
      && value.candidate_binding_sha256 === null;
    const bootstrapPairIsValid = typeof value.bootstrap_request_id === "string"
      && LIBRARY_BOOTSTRAP_REQUEST_ID.test(value.bootstrap_request_id)
      && typeof value.candidate_binding_sha256 === "string"
      && LOWER_SHA256.test(value.candidate_binding_sha256);
    if (
      (!bootstrapPairIsNull && !bootstrapPairIsValid)
      || typeof value.database_uuid !== "string"
      || !LOWER_UUID_V4.test(value.database_uuid)
      || value.schema_version !== 20
      || typeof value.runtime_generation_id !== "string"
      || !RUNTIME_GENERATION_ID.test(value.runtime_generation_id)
    ) {
      return null;
    }
  } else {
    return null;
  }
  return value as unknown as LibraryBootstrapRuntimeIdentity;
}

export function matchesLibraryBootstrapRuntime(
  runtime: LibraryBootstrapRuntimeIdentity,
  expected: LibraryBootstrapRuntimeExpectation,
): boolean {
  return LIBRARY_BOOTSTRAP_REQUEST_ID.test(expected.requestId)
    && LOWER_SHA256.test(expected.candidateBindingSha256)
    && runtime.mode === expected.mode
    && runtime.bootstrap_request_id === expected.requestId
    && runtime.candidate_binding_sha256 === expected.candidateBindingSha256;
}

export function verifyBackendHealthV2Payload(
  payload: unknown,
  challenge: string,
  expectedRuntime?: SQLiteHealthCapability,
): LibraryBootstrapRuntimeIdentity | null {
  if (
    !isRecord(payload)
    || !hasExactKeys(payload, [
      "status",
      "object",
      "service",
      "api_version",
      "schema_version",
      "challenge_proof",
      "sqlite_runtime",
      "runtime",
    ])
    || !LOWER_SHA256.test(challenge)
    || payload.status !== "ok"
    || payload.object !== "health.check"
    || payload.service !== EXPECTED_BACKEND_SERVICE
    || payload.api_version !== EXPECTED_API_VERSION
    || payload.schema_version !== "2"
    || !isExactSafeSQLiteHealthCapability(payload.sqlite_runtime)
  ) {
    return null;
  }
  const sqliteRuntime = payload.sqlite_runtime;
  if (expectedRuntime && !sameSQLiteHealthCapability(sqliteRuntime, expectedRuntime)) {
    return null;
  }
  const runtime = parseRuntimeIdentity(payload.runtime);
  const suppliedProof = typeof payload.challenge_proof === "string"
    ? payload.challenge_proof
    : "";
  if (runtime === null || !LOWER_SHA256.test(suppliedProof)) {
    return null;
  }
  const expectedProof = createHmac("sha256", getDesktopSessionToken())
    .update(backendHealthV2ProofMessage(challenge, payload, sqliteRuntime, runtime))
    .digest();
  return timingSafeEqual(Buffer.from(suppliedProof, "hex"), expectedProof)
    ? runtime
    : null;
}

export function verifyBackendHealthPayload(
  payload: unknown,
  challenge: string,
  expectedRuntime?: SQLiteHealthCapability,
): boolean {
  if (
    !isRecord(payload)
    || !hasExactKeys(payload, [
      "status",
      "object",
      "service",
      "api_version",
      "challenge_proof",
      "sqlite_runtime",
    ])
    || !/^[0-9a-f]{64}$/.test(challenge)
    || payload.status !== "ok"
    || payload.object !== "health.check"
    || payload.service !== EXPECTED_BACKEND_SERVICE
    || payload.api_version !== EXPECTED_API_VERSION
    || !isExactSafeSQLiteHealthCapability(payload.sqlite_runtime)
  ) {
    return false;
  }
  const health = payload;
  const runtime = payload.sqlite_runtime;
  if (expectedRuntime && !sameSQLiteHealthCapability(runtime, expectedRuntime)) {
    return false;
  }
  const suppliedProof = typeof health.challenge_proof === "string"
    ? health.challenge_proof
    : "";
  if (!/^[0-9a-f]{64}$/.test(suppliedProof)) {
    return false;
  }

  const expectedProof = createHmac("sha256", getDesktopSessionToken())
    .update(backendHealthProofMessage(challenge, health, runtime))
    .digest();
  const suppliedProofBytes = Buffer.from(suppliedProof, "hex");
  return timingSafeEqual(suppliedProofBytes, expectedProof);
}

async function readBoundedHealthJson(response: Response): Promise<unknown | null> {
  const rawLength = response.headers.get("content-length");
  if (rawLength !== null) {
    if (!/^(?:0|[1-9][0-9]*)$/.test(rawLength)) {
      await response.body?.cancel().catch(() => undefined);
      return null;
    }
    const declaredLength = Number(rawLength);
    if (!Number.isSafeInteger(declaredLength) || declaredLength > MAX_HEALTH_RESPONSE_BYTES) {
      await response.body?.cancel().catch(() => undefined);
      return null;
    }
  }

  if (response.body === null) {
    const text = await response.text();
    if (Buffer.byteLength(text, "utf8") > MAX_HEALTH_RESPONSE_BYTES) {
      return null;
    }
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return null;
    }
  }

  const reader = response.body.getReader();
  const chunks: Buffer[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) {
        break;
      }
      const chunk = Buffer.from(next.value);
      totalBytes += chunk.byteLength;
      if (totalBytes > MAX_HEALTH_RESPONSE_BYTES) {
        await reader.cancel().catch(() => undefined);
        return null;
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }
  try {
    return JSON.parse(Buffer.concat(chunks, totalBytes).toString("utf8")) as unknown;
  } catch {
    return null;
  }
}

export async function probeBackendHealth(
  url: string,
  expectedRuntime?: SQLiteHealthCapability,
): Promise<boolean> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 1500);
  const challenge = randomBytes(32).toString("hex");

  try {
    const response = await guardedFetch(
      `${url}/healthz?challenge=${challenge}`,
      {
        signal: controller.signal,
      },
    );
    if (!response.ok) {
      return false;
    }
    const payload = await readBoundedHealthJson(response);
    return payload !== null
      && verifyBackendHealthPayload(payload, challenge, expectedRuntime);
  } catch {
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function probeBackendHealthV2(
  url: string,
  expectedRuntime?: SQLiteHealthCapability,
): Promise<LibraryBootstrapRuntimeIdentity | null> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 1500);
  const challenge = randomBytes(32).toString("hex");
  try {
    const response = await guardedFetch(
      `${url}/healthz?contract=2&challenge=${challenge}`,
      { signal: controller.signal },
    );
    if (!response.ok) return null;
    const payload = await readBoundedHealthJson(response);
    return payload === null
      ? null
      : verifyBackendHealthV2Payload(payload, challenge, expectedRuntime);
  } catch {
    return null;
  } finally {
    clearTimeout(timeoutId);
  }
}

function parseLibraryBootstrapCommitResponse(
  payload: unknown,
  requestId: string,
): LibraryBootstrapCommitResponse | null {
  if (!isRecord(payload) || !hasExactKeys(payload, [
    "object",
    "schema_version",
    "request_id",
    "status",
    "scan_started",
    "scan_stage",
    "editor_state",
    "blueprint_authority",
    "timeline_edit_granted",
  ])) {
    return null;
  }
  return payload.object === "memolens.library_bootstrap_result"
    && payload.schema_version === "1"
    && payload.request_id === requestId
    && payload.status === "library_authority_committed"
    && payload.scan_started === false
    && payload.scan_stage === "awaiting_scan_worker"
    && payload.editor_state === "awaiting_grounded_timeline"
    && payload.blueprint_authority === "unverified"
    && payload.timeline_edit_granted === false
    ? payload as unknown as LibraryBootstrapCommitResponse
    : null;
}

export async function commitLibraryBootstrapCandidate(
  url: string,
  requestId: string,
  candidateBindingSha256: string,
  options: {
    fetchImpl?: typeof fetch;
    mainAuthorityToken?: string;
  } = {},
): Promise<LibraryBootstrapCommitResult> {
  if (
    !LIBRARY_BOOTSTRAP_REQUEST_ID.test(requestId)
    || !LOWER_SHA256.test(candidateBindingSha256)
  ) {
    throw new Error("MemoLens rejected an invalid Library bootstrap command identity.");
  }
  const mainAuthority = options.mainAuthorityToken ?? getMainAuthorityToken();
  if (mainAuthority.length === 0) {
    throw new Error("MemoLens main authority is unavailable for Library bootstrap.");
  }
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 5_000);
  try {
    const response = await (options.fetchImpl ?? guardedFetch)(
      `${url}/v1/main/library-bootstrap`,
      {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "X-MemoLens-Main-Authority": mainAuthority,
          "Idempotency-Key": requestId,
        },
        body: JSON.stringify({
          object: "memolens.main_library_bootstrap_command",
          schema_version: "1",
          request_id: requestId,
          candidate_binding_sha256: candidateBindingSha256,
        }),
      },
    );
    if (response.status !== 201) {
      await response.body?.cancel().catch(() => undefined);
      throw new Error(`MemoLens Library bootstrap Core commit failed (${response.status}).`);
    }
    const replayHeader = response.headers.get("Idempotency-Replayed");
    if (replayHeader !== null && replayHeader !== "true") {
      await response.body?.cancel().catch(() => undefined);
      throw new Error("MemoLens rejected an invalid Library bootstrap replay header.");
    }
    const payload = await readBoundedHealthJson(response);
    const parsed = parseLibraryBootstrapCommitResponse(payload, requestId);
    if (parsed === null) {
      throw new Error("MemoLens rejected an invalid Library bootstrap Core response.");
    }
    return { response: parsed, replayed: replayHeader === "true" };
  } finally {
    clearTimeout(timeoutId);
  }
}

const backendProcessSupervisor = new BackendProcessSupervisor({
  backendUrl: DEFAULT_BACKEND_URL,
  probeHealth: probeBackendHealth,
  async probeBootstrapRuntime(url, expectation, expectedRuntime) {
    const runtime = await probeBackendHealthV2(url, expectedRuntime);
    if (runtime === null) return "unavailable";
    return matchesLibraryBootstrapRuntime(runtime, expectation)
      ? "expected"
      : "wrong";
  },
  getSessionToken: getDesktopSessionToken,
  getMainAuthorityToken,
  getRuntimeAuthorityEpoch,
  rotateSessionToken: rotateRuntimeCredentials,
  updateBackendTrust,
  getAppStateDir: getCanonicalAppStateDir,
});

export async function ensureBackendReady(
  projectRoot: string,
  settings: DesktopSettings,
): Promise<DesktopBackendStatus> {
  return backendProcessSupervisor.ensureReady(projectRoot, settings);
}

export async function ensureLibraryBootstrapCandidateReady(
  projectRoot: string,
  settings: DesktopSettings,
  expectation: Omit<LibraryBootstrapRuntimeExpectation, "mode">,
): Promise<DesktopBackendStatus> {
  return backendProcessSupervisor.ensureBootstrapCandidate(
    projectRoot,
    settings,
    { mode: "bootstrap_candidate", ...expectation },
  );
}

export function stopManagedBackend(): Promise<boolean> {
  return backendProcessSupervisor.stop();
}
