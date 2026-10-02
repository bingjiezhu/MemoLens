import {
  execFile,
  spawn,
  type ChildProcess,
} from "node:child_process";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { StringDecoder } from "node:string_decoder";

import sqliteRuntimePolicyDocument from "../core/sqlite_runtime_policy.json" with { type: "json" };
import type {
  DesktopBackendStatus,
  DesktopSettings,
} from "../src/query/types.js";
import { managedPythonCommand } from "./desktopSettings.js";

const DEFAULT_STARTUP_TIMEOUT_MS = 30000;
const DEFAULT_HEALTH_POLL_INTERVAL_MS = 500;
const DEFAULT_RETRY_DELAY_MS = 1000;
const DEFAULT_TERMINATION_GRACE_MS = 5000;
const DEFAULT_TERMINATION_FORCE_MS = 1500;
const SQLITE_RUNTIME_CHECK_TIMEOUT_MS = 3_000;
const SQLITE_RUNTIME_CHECK_MAX_BUFFER_BYTES = 16 * 1024;

interface BackendSpawnOptions {
  cwd: string;
  env: NodeJS.ProcessEnv;
  stdio: ["ignore", "pipe", "pipe"];
}

type SpawnBackendProcess = (
  command: string,
  args: string[],
  options: BackendSpawnOptions,
) => ChildProcess;

interface RuntimeCheckExecOptions {
  cwd: string;
  encoding: "utf8";
  env: NodeJS.ProcessEnv;
  killSignal: "SIGKILL";
  maxBuffer: number;
  shell: false;
  timeout: number;
  windowsHide: true;
}

type RuntimeCheckError = Error & {
  code?: number | string | null;
  killed?: boolean;
  signal?: NodeJS.Signals | string | null;
};

type ExecFileProcess = (
  command: string,
  args: string[],
  options: RuntimeCheckExecOptions,
  callback: (
    error: RuntimeCheckError | null,
    stdout: string,
    stderr: string,
  ) => void,
) => unknown;

interface BackendLogger {
  log(message: string): void;
  error(message: string): void;
}

export interface SQLiteHealthCapability {
  policy_id: string;
  sqlite_version: string;
  wal_reset_safe: true;
  journal_policy: "wal";
}

export interface BootstrapCandidateRuntimeExpectation {
  mode: "bootstrap_candidate";
  requestId: string;
  candidateBindingSha256: string;
}

export type BootstrapRuntimeProbeResult = "expected" | "wrong" | "unavailable";

interface SQLiteRuntimeCapability {
  object: "memolens.sqlite_runtime";
  schema_version: "1";
  policy_id: string;
  python_version: string;
  sqlite_version: string;
  wal_reset_safe: boolean;
  journal_policy: "wal";
  status: "ready" | "unsafe";
  reason_code: null | "sqlite_wal_reset_unsafe";
}

export interface ManagedExecutableIdentity {
  realpath: string;
  device: string;
  inode: string;
  size: string;
  mtimeNs: string;
}

type ResolveExecutableIdentity = (command: string) => ManagedExecutableIdentity;

export interface BackendProcessSupervisorOptions {
  backendUrl: string;
  probeHealth(url: string, expectedRuntime?: SQLiteHealthCapability): Promise<boolean>;
  probeBootstrapRuntime?(
    url: string,
    expectation: BootstrapCandidateRuntimeExpectation,
    expectedRuntime?: SQLiteHealthCapability,
  ): Promise<BootstrapRuntimeProbeResult>;
  getSessionToken(): string;
  getMainAuthorityToken?(): string;
  getRuntimeAuthorityEpoch?(): string;
  rotateSessionToken(): void;
  updateBackendTrust(trusted: boolean): void;
  getAppStateDir(): string;
  spawnProcess?: SpawnBackendProcess;
  execFileProcess?: ExecFileProcess;
  resolveExecutableIdentity?: ResolveExecutableIdentity;
  readEnvFile?: (path: string) => string;
  environment?: () => NodeJS.ProcessEnv;
  now?: () => number;
  sleep?: (durationMs: number) => Promise<void>;
  logger?: BackendLogger;
  startupTimeoutMs?: number;
  healthPollIntervalMs?: number;
  retryDelayMs?: number;
  terminationGraceMs?: number;
  terminationForceMs?: number;
}

function normalizeBackendUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

function resolveBackendPort(url: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.port.trim().length > 0) {
      return parsed.port;
    }
    return parsed.protocol === "https:" ? "443" : "80";
  } catch {
    return "5519";
  }
}

function defaultSleep(durationMs: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, durationMs);
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return actual.length === sortedExpected.length
    && actual.every((key, index) => key === sortedExpected[index]);
}

function isCanonicalSQLiteVersion(value: unknown): value is string {
  return typeof value === "string"
    && value.length <= 32
    && /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/.test(value);
}

interface SQLiteRuntimePolicy {
  object: "memolens.sqlite_runtime_policy";
  schema_version: "1";
  policy_id: string;
  sqlite_major: number;
  backport_minimums: readonly (readonly [number, number, number])[];
  mainline_minimum: readonly [number, number, number];
  withdrawn_branches: readonly (readonly [number, number])[];
}

function isIntegerTuple(value: unknown, length: number): value is number[] {
  return Array.isArray(value)
    && value.length === length
    && value.every((part) => Number.isSafeInteger(part) && part >= 0);
}

function parseSQLiteRuntimePolicy(value: unknown): SQLiteRuntimePolicy {
  if (!isRecord(value) || !hasExactKeys(value, [
    "object",
    "schema_version",
    "policy_id",
    "sqlite_major",
    "backport_minimums",
    "mainline_minimum",
    "withdrawn_branches",
  ])) {
    throw new Error("MemoLens SQLite runtime policy has an invalid shape.");
  }
  if (
    value.object !== "memolens.sqlite_runtime_policy"
    || value.schema_version !== "1"
    || typeof value.policy_id !== "string"
    || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.policy_id)
    || !Number.isSafeInteger(value.sqlite_major)
    || !Array.isArray(value.backport_minimums)
    || !value.backport_minimums.every((entry) => isIntegerTuple(entry, 3))
    || !isIntegerTuple(value.mainline_minimum, 3)
    || !Array.isArray(value.withdrawn_branches)
    || !value.withdrawn_branches.every((entry) => isIntegerTuple(entry, 2))
  ) {
    throw new Error("MemoLens SQLite runtime policy contains invalid values.");
  }
  const policy = value as unknown as SQLiteRuntimePolicy;
  const mainlineBranch = policy.mainline_minimum.slice(0, 2).join(".");
  const backportBranches = policy.backport_minimums.map(
    ([major, minor]) => `${major}.${minor}`,
  );
  const withdrawnBranches = policy.withdrawn_branches.map(
    ([major, minor]) => `${major}.${minor}`,
  );
  const withdrawn = new Set(withdrawnBranches);
  if (
    policy.mainline_minimum[0] !== policy.sqlite_major
    || policy.backport_minimums.some(([major]) => major !== policy.sqlite_major)
    || policy.withdrawn_branches.some(([major]) => major !== policy.sqlite_major)
    || new Set(backportBranches).size !== backportBranches.length
    || new Set(withdrawnBranches).size !== withdrawnBranches.length
    || backportBranches.some((branch) => withdrawn.has(branch))
    || withdrawn.has(mainlineBranch)
    || policy.backport_minimums.some(
      (minimum) => compareVersionTuples(minimum, policy.mainline_minimum) >= 0,
    )
  ) {
    throw new Error("MemoLens SQLite runtime policy is internally inconsistent.");
  }
  return policy;
}

const SQLITE_RUNTIME_POLICY = parseSQLiteRuntimePolicy(sqliteRuntimePolicyDocument);

function sqliteVersionTuple(value: string): [number, number, number] {
  const [major, minor, patch] = value.split(".").map((part) => Number(part));
  return [major, minor, patch];
}

function compareVersionTuples(
  left: readonly [number, number, number],
  right: readonly [number, number, number],
): number {
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) {
      return left[index] < right[index] ? -1 : 1;
    }
  }
  return 0;
}

function sqliteVersionAllowedByPolicy(value: string): boolean {
  const version = sqliteVersionTuple(value);
  if (version[0] !== SQLITE_RUNTIME_POLICY.sqlite_major) {
    return false;
  }
  if (SQLITE_RUNTIME_POLICY.withdrawn_branches.some(
    ([major, minor]) => version[0] === major && version[1] === minor,
  )) {
    return false;
  }
  const backport = SQLITE_RUNTIME_POLICY.backport_minimums.find(
    ([major, minor]) => version[0] === major && version[1] === minor,
  );
  if (backport) {
    return version[2] >= backport[2];
  }
  return compareVersionTuples(version, SQLITE_RUNTIME_POLICY.mainline_minimum) >= 0;
}

export function isExactSafeSQLiteHealthCapability(
  value: unknown,
): value is SQLiteHealthCapability {
  if (!isRecord(value) || !hasExactKeys(value, [
    "policy_id",
    "sqlite_version",
    "wal_reset_safe",
    "journal_policy",
  ])) {
    return false;
  }
  return value.policy_id === SQLITE_RUNTIME_POLICY.policy_id
    && isCanonicalSQLiteVersion(value.sqlite_version)
    && sqliteVersionAllowedByPolicy(value.sqlite_version)
    && value.wal_reset_safe === true
    && value.journal_policy === "wal";
}

function parseSQLiteRuntimeCheck(value: unknown): {
  classification: "safe" | "unsafe";
  capability: SQLiteRuntimeCapability;
} | null {
  if (!isRecord(value) || !hasExactKeys(value, [
    "object",
    "schema_version",
    "policy_id",
    "python_version",
    "sqlite_version",
    "wal_reset_safe",
    "journal_policy",
    "status",
    "reason_code",
  ])) {
    return null;
  }
  if (
    value.object !== "memolens.sqlite_runtime"
    || value.schema_version !== "1"
    || value.policy_id !== SQLITE_RUNTIME_POLICY.policy_id
    || typeof value.python_version !== "string"
    || value.python_version.length === 0
    || value.python_version.length > 64
    || !/^[0-9A-Za-z][0-9A-Za-z.+-]*$/.test(value.python_version)
    || !isCanonicalSQLiteVersion(value.sqlite_version)
    || value.journal_policy !== "wal"
  ) {
    return null;
  }
  const versionIsSafe = sqliteVersionAllowedByPolicy(value.sqlite_version);
  if (
    versionIsSafe
    && value.wal_reset_safe === true
    && value.status === "ready"
    && value.reason_code === null
  ) {
    return {
      classification: "safe",
      capability: value as unknown as SQLiteRuntimeCapability,
    };
  }
  if (
    !versionIsSafe
    && value.wal_reset_safe === false
    && value.status === "unsafe"
    && value.reason_code === "sqlite_wal_reset_unsafe"
  ) {
    return {
      classification: "unsafe",
      capability: value as unknown as SQLiteRuntimeCapability,
    };
  }
  return null;
}

function runtimeCheckEnvironment(environment: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const allowed = new Set([
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMP",
    "TEMP",
    "TMPDIR",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
  ]);
  return Object.fromEntries(
    Object.entries(environment).filter(
      (entry): entry is [string, string] => allowed.has(entry[0]) && typeof entry[1] === "string",
    ),
  );
}

const PYTHON_STARTUP_ENVIRONMENT = /^PYTHON/i;
const OBVIOUS_SECRET_ENVIRONMENT = /(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD)(?:_|$)/i;
const MEMOLENS_AUTHORITY_ENVIRONMENT = new Set([
  "MEMOLENS_DESKTOP_SESSION_TOKEN",
  "MEMOLENS_MAIN_AUTHORITY_TOKEN",
  "MEMOLENS_RUNTIME_AUTHORITY_EPOCH",
  "MEMOLENS_BOOTSTRAP_REQUEST_ID",
  "MEMOLENS_BOOTSTRAP_BINDING_SHA256",
]);

function backendEnvironment(environment: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(
    Object.entries(environment).filter(
      (entry): entry is [string, string] => (
        typeof entry[1] === "string"
        && !PYTHON_STARTUP_ENVIRONMENT.test(entry[0])
        && !MEMOLENS_AUTHORITY_ENVIRONMENT.has(entry[0])
      ),
    ),
  );
}

function environmentSecretValues(environment: NodeJS.ProcessEnv): string[] {
  return Object.entries(environment)
    .filter((entry): entry is [string, string] => (
      OBVIOUS_SECRET_ENVIRONMENT.test(entry[0])
      && typeof entry[1] === "string"
      && entry[1].length >= 4
    ))
    .map(([, value]) => value);
}

function resolveExecutableIdentity(command: string): ManagedExecutableIdentity {
  const resolved = realpathSync(command);
  const metadata = statSync(resolved, { bigint: true });
  if (!metadata.isFile()) {
    throw new Error("Managed Python runtime is not a regular file.");
  }
  if (process.platform !== "win32" && (metadata.mode & 0o111n) === 0n) {
    throw new Error("Managed Python runtime is not executable.");
  }
  return {
    realpath: resolved,
    device: metadata.dev.toString(),
    inode: metadata.ino.toString(),
    size: metadata.size.toString(),
    mtimeNs: metadata.mtimeNs.toString(),
  };
}

function isManagedExecutableIdentity(value: unknown): value is ManagedExecutableIdentity {
  if (!isRecord(value) || !hasExactKeys(value, [
    "realpath",
    "device",
    "inode",
    "size",
    "mtimeNs",
  ])) {
    return false;
  }
  return typeof value.realpath === "string"
    && isAbsolute(value.realpath)
    && [value.device, value.inode, value.size, value.mtimeNs].every(
      (part) => typeof part === "string" && /^[0-9]+$/.test(part),
    );
}

function sameExecutableIdentity(
  left: ManagedExecutableIdentity,
  right: ManagedExecutableIdentity,
): boolean {
  return left.realpath === right.realpath
    && left.device === right.device
    && left.inode === right.inode
    && left.size === right.size
    && left.mtimeNs === right.mtimeNs;
}

function healthCapabilityFromRuntime(
  capability: SQLiteRuntimeCapability,
): SQLiteHealthCapability {
  return {
    policy_id: capability.policy_id,
    sqlite_version: capability.sqlite_version,
    wal_reset_safe: true,
    journal_policy: "wal",
  };
}

const REDACTED = "[redacted]";

function protectedSecrets(values: readonly string[]): string[] {
  return [...new Set(values.filter((value) => value.length > 0))]
    .sort((left, right) => right.length - left.length);
}

function redactComplete(message: string, secrets: readonly string[]): string {
  return secrets.reduce(
    (current, value) => current.replaceAll(value, REDACTED),
    message,
  );
}

class StreamingSecretRedactor {
  private readonly secrets: readonly string[];
  private readonly decoder = new StringDecoder("utf8");
  private pending = "";
  private ended = false;

  constructor(values: readonly string[]) {
    this.secrets = protectedSecrets(values);
  }

  push(chunk: Buffer): string {
    if (this.ended) {
      return "";
    }
    return this.consume(this.decoder.write(chunk), false);
  }

  flush(): string {
    if (this.ended) {
      return "";
    }
    this.ended = true;
    const decodedTail = this.decoder.end();
    return this.consume(decodedTail, true);
  }

  private consume(value: string, flush: boolean): string {
    this.pending += value;
    let output = "";
    while (this.pending.length > 0) {
      const possible = this.secrets.filter((secret) => secret.startsWith(this.pending));
      const exact = possible.some((secret) => secret.length === this.pending.length);
      if (!flush && possible.length > 0 && (!exact || possible.some(
        (secret) => secret.length > this.pending.length
      ))) {
        break;
      }
      if (exact) {
        output += REDACTED;
        this.pending = "";
        continue;
      }
      const completed = this.secrets.find((secret) => this.pending.startsWith(secret));
      if (completed) {
        output += REDACTED;
        this.pending = this.pending.slice(completed.length);
        continue;
      }
      if (!flush && possible.length > 0) {
        break;
      }
      output += this.pending[0];
      this.pending = this.pending.slice(1);
    }
    return output;
  }
}

interface ManagedRuntimeAdmission {
  command: string;
  identity: ManagedExecutableIdentity;
  runtime: SQLiteRuntimeCapability;
  health: SQLiteHealthCapability;
}

type ManagedRuntimeAdmissionResult =
  | { admission: ManagedRuntimeAdmission; error: null }
  | { admission: null; error: string };

type SQLiteRuntimeCheckResult =
  | { capability: SQLiteRuntimeCapability; error: null }
  | { capability: null; error: string };

export class BackendProcessSupervisor {
  private readonly backendUrl: string;
  private readonly probeHealth: (
    url: string,
    expectedRuntime?: SQLiteHealthCapability,
  ) => Promise<boolean>;
  private readonly probeBootstrapRuntime: ((
    url: string,
    expectation: BootstrapCandidateRuntimeExpectation,
    expectedRuntime?: SQLiteHealthCapability,
  ) => Promise<BootstrapRuntimeProbeResult>) | null;
  private readonly getSessionToken: () => string;
  private readonly getMainAuthorityToken: (() => string) | null;
  private readonly getRuntimeAuthorityEpoch: (() => string) | null;
  private readonly rotateSessionToken: () => void;
  private readonly updateBackendTrust: (trusted: boolean) => void;
  private readonly getAppStateDir: () => string;
  private readonly spawnProcess: SpawnBackendProcess;
  private readonly execFileProcess: ExecFileProcess;
  private readonly resolveExecutableIdentity: ResolveExecutableIdentity;
  private readonly readEnvFile: (path: string) => string;
  private readonly environment: () => NodeJS.ProcessEnv;
  private readonly now: () => number;
  private readonly sleep: (durationMs: number) => Promise<void>;
  private readonly logger: BackendLogger;
  private readonly startupTimeoutMs: number;
  private readonly healthPollIntervalMs: number;
  private readonly retryDelayMs: number;
  private readonly terminationGraceMs: number;
  private readonly terminationForceMs: number;

  private managedProcess: ChildProcess | null = null;
  private terminatingProcess: ChildProcess | null = null;
  private terminationInFlight: Promise<boolean> | null = null;
  private startError: string | null = null;
  private ensureInFlight: Promise<DesktopBackendStatus> | null = null;
  private bootstrapEnsureInFlight: Promise<DesktopBackendStatus> | null = null;
  private lifecycleGeneration = 0;

  constructor(options: BackendProcessSupervisorOptions) {
    if (Boolean(options.getMainAuthorityToken) !== Boolean(options.getRuntimeAuthorityEpoch)) {
      throw new Error("Main authority token and runtime epoch must be configured together.");
    }
    this.backendUrl = normalizeBackendUrl(options.backendUrl);
    this.probeHealth = options.probeHealth;
    this.probeBootstrapRuntime = options.probeBootstrapRuntime ?? null;
    this.getSessionToken = options.getSessionToken;
    this.getMainAuthorityToken = options.getMainAuthorityToken ?? null;
    this.getRuntimeAuthorityEpoch = options.getRuntimeAuthorityEpoch ?? null;
    this.rotateSessionToken = options.rotateSessionToken;
    this.updateBackendTrust = options.updateBackendTrust;
    this.getAppStateDir = options.getAppStateDir;
    this.spawnProcess = options.spawnProcess
      ?? ((command, args, spawnOptions) => spawn(command, args, spawnOptions));
    this.execFileProcess = options.execFileProcess
      ?? ((command, args, execOptions, callback) => {
        execFile(command, args, execOptions, (error, stdout, stderr) => {
          callback(
            error as RuntimeCheckError | null,
            stdout,
            stderr,
          );
        });
      });
    this.resolveExecutableIdentity = options.resolveExecutableIdentity
      ?? resolveExecutableIdentity;
    this.readEnvFile = options.readEnvFile ?? ((path) => readFileSync(path, "utf-8"));
    this.environment = options.environment ?? (() => process.env);
    this.now = options.now ?? Date.now;
    this.sleep = options.sleep ?? defaultSleep;
    this.logger = options.logger ?? console;
    this.startupTimeoutMs = options.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS;
    this.healthPollIntervalMs = options.healthPollIntervalMs
      ?? DEFAULT_HEALTH_POLL_INTERVAL_MS;
    this.retryDelayMs = options.retryDelayMs ?? DEFAULT_RETRY_DELAY_MS;
    this.terminationGraceMs = options.terminationGraceMs ?? DEFAULT_TERMINATION_GRACE_MS;
    this.terminationForceMs = options.terminationForceMs ?? DEFAULT_TERMINATION_FORCE_MS;
  }

  async ensureReady(
    projectRoot: string,
    settings: DesktopSettings,
  ): Promise<DesktopBackendStatus> {
    if (this.ensureInFlight !== null) {
      return this.ensureInFlight;
    }
    const generation = this.lifecycleGeneration;
    const operation = this.ensureReadyOnce(projectRoot, settings, generation);
    this.ensureInFlight = operation;
    try {
      return await operation;
    } finally {
      if (this.ensureInFlight === operation) {
        this.ensureInFlight = null;
      }
    }
  }

  async ensureBootstrapCandidate(
    projectRoot: string,
    settings: DesktopSettings,
    expectation: BootstrapCandidateRuntimeExpectation,
  ): Promise<DesktopBackendStatus> {
    if (
      !/^lb_[0-9a-f]{64}$/.test(expectation.requestId)
      || !/^[0-9a-f]{64}$/.test(expectation.candidateBindingSha256)
    ) {
      throw new Error("MemoLens rejected an invalid bootstrap candidate identity.");
    }
    if (this.probeBootstrapRuntime === null) {
      throw new Error("MemoLens bootstrap runtime proof is not configured.");
    }
    if (this.bootstrapEnsureInFlight !== null) {
      return this.bootstrapEnsureInFlight;
    }
    if (this.ensureInFlight !== null) {
      await this.ensureInFlight;
    }
    const generation = this.lifecycleGeneration;
    const operation = this.ensureBootstrapCandidateOnce(
      projectRoot,
      settings,
      expectation,
      generation,
    );
    this.bootstrapEnsureInFlight = operation;
    try {
      return await operation;
    } finally {
      if (this.bootstrapEnsureInFlight === operation) {
        this.bootstrapEnsureInFlight = null;
      }
    }
  }

  private async ensureBootstrapCandidateOnce(
    projectRoot: string,
    settings: DesktopSettings,
    expectation: BootstrapCandidateRuntimeExpectation,
    generation: number,
  ): Promise<DesktopBackendStatus> {
    this.updateBackendTrust(false);
    const pendingTermination = this.terminationInFlight;
    if (pendingTermination !== null && !await pendingTermination) {
      return this.terminationFailureStatus(false);
    }
    if (!this.isGenerationCurrent(generation)) return this.cancelledStatus(false);

    const firstObservation = await this.probeBootstrapRuntime!(
      this.backendUrl,
      expectation,
    );
    if (!this.isGenerationCurrent(generation)) return this.cancelledStatus(false);
    if (firstObservation === "expected") {
      this.updateBackendTrust(true);
      return {
        state: "connected",
        message: "Exact Library bootstrap candidate is online.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }
    if (firstObservation === "wrong" && this.managedProcess === null) {
      return {
        state: "unavailable",
        message: "A different authenticated MemoLens runtime owns the backend endpoint.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    if (this.managedProcess !== null) {
      this.rotateSessionToken();
      if (!await this.terminateManagedProcess()) {
        return this.terminationFailureStatus(false);
      }
    }
    if (!settings.autoStartBackend) {
      return {
        state: "unavailable",
        message: "Library bootstrap requires the managed local backend runtime.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const admission = await this.admitManagedRuntime(projectRoot);
      if (!this.isGenerationCurrent(generation)) return this.cancelledStatus(attempt > 0);
      if (admission.error !== null) {
        return {
          state: "unavailable",
          message: admission.error,
          url: this.backendUrl,
          startedByApp: attempt > 0,
        };
      }
      const spawnError = this.spawnAdmittedAttempt(
        projectRoot,
        admission.admission,
        this.backendUrl,
        expectation.requestId,
      );
      if (spawnError !== null) {
        return {
          state: "unavailable",
          message: spawnError,
          url: this.backendUrl,
          startedByApp: attempt > 0,
        };
      }
      if (await this.waitForBootstrapCandidate(
        this.backendUrl,
        admission.admission.health,
        expectation,
        generation,
      )) {
        return this.startedStatus(
          attempt === 0
            ? "Exact Library bootstrap candidate started by the desktop app."
            : "Exact Library bootstrap candidate started by the desktop app (retry succeeded).",
        );
      }
      if (!this.isGenerationCurrent(generation)) return this.cancelledStatus(true);
      this.rotateSessionToken();
      if (!await this.terminateManagedProcess()) {
        return this.terminationFailureStatus(true);
      }
      if (attempt === 0) {
        await this.sleep(this.retryDelayMs);
        if (!this.isGenerationCurrent(generation)) return this.cancelledStatus(true);
      }
    }
    return {
      state: "unavailable",
      message: this.startError
        ? `Library bootstrap candidate failed to start: ${this.startError}`
        : "The exact Library bootstrap candidate did not become healthy.",
      url: this.backendUrl,
      startedByApp: true,
    };
  }

  private async ensureReadyOnce(
    projectRoot: string,
    settings: DesktopSettings,
    generation: number,
  ): Promise<DesktopBackendStatus> {
    // A health proof is required on every ensure operation. Until it succeeds,
    // the Electron session must not attach the bearer to renderer traffic.
    this.updateBackendTrust(false);

    const pendingTermination = this.terminationInFlight;
    if (pendingTermination !== null && !await pendingTermination) {
      return this.terminationFailureStatus(false);
    }
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(false);
    }

    const alreadyHealthy = await this.probeHealth(this.backendUrl);
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(false);
    }
    if (alreadyHealthy) {
      this.updateBackendTrust(true);
      return {
        state: "connected",
        message: "Local backend is online.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    if (!settings.autoStartBackend) {
      return {
        state: "unavailable",
        message: "Backend is offline. Enable auto-start or launch the Python service manually.",
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    const firstAdmission = await this.admitManagedRuntime(projectRoot);
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(false);
    }
    if (firstAdmission.error !== null) {
      if (this.managedProcess !== null) {
        this.rotateSessionToken();
        if (!await this.terminateManagedProcess()) {
          return this.terminationFailureStatus(false);
        }
      }
      return {
        state: "unavailable",
        message: firstAdmission.error,
        url: this.backendUrl,
        startedByApp: false,
      };
    }

    // Kill any previously managed process that has exited or is unresponsive.
    if (this.managedProcess !== null && this.processHasExited(this.managedProcess)) {
      this.rotateSessionToken();
      if (!await this.terminateManagedProcess()) {
        return this.terminationFailureStatus(false);
      }
    }

    if (this.managedProcess === null) {
      const spawnError = this.spawnAdmittedAttempt(
        projectRoot,
        firstAdmission.admission,
        this.backendUrl,
      );
      if (spawnError !== null) {
        return {
          state: "unavailable",
          message: spawnError,
          url: this.backendUrl,
          startedByApp: false,
        };
      }
    }

    if (await this.waitForHealthy(
      this.backendUrl,
      firstAdmission.admission.health,
      generation,
    )) {
      return this.startedStatus("Local backend started by the desktop app.");
    }
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(true);
    }

    // First attempt failed — kill the process and try once more.
    this.logger.log("[memolens-backend] first start attempt failed, retrying…");
    this.rotateSessionToken();
    if (!await this.terminateManagedProcess()) {
      return this.terminationFailureStatus(true);
    }
    await this.sleep(this.retryDelayMs);
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(true);
    }
    const retryAdmission = await this.admitManagedRuntime(projectRoot);
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(true);
    }
    if (retryAdmission.error !== null) {
      return {
        state: "unavailable",
        message: retryAdmission.error,
        url: this.backendUrl,
        startedByApp: true,
      };
    }
    const retrySpawnError = this.spawnAdmittedAttempt(
      projectRoot,
      retryAdmission.admission,
      this.backendUrl,
    );
    if (retrySpawnError !== null) {
      return {
        state: "unavailable",
        message: retrySpawnError,
        url: this.backendUrl,
        startedByApp: true,
      };
    }

    if (await this.waitForHealthy(
      this.backendUrl,
      retryAdmission.admission.health,
      generation,
    )) {
      return this.startedStatus("Local backend started by the desktop app (retry succeeded).");
    }
    if (!this.isGenerationCurrent(generation)) {
      return this.cancelledStatus(true);
    }

    return {
      state: "unavailable",
      message: this.startError
        ? `Backend failed to start: ${this.startError}`
        : "Backend did not become healthy. Check the Python environment configured for the managed MemoLens runtime.",
      url: this.backendUrl,
      startedByApp: true,
    };
  }

  async stop(): Promise<boolean> {
    this.lifecycleGeneration += 1;
    // Do not let a post-stop caller join the cancelled generation. The old
    // operation will observe the generation mismatch and its finally block is
    // identity-guarded, so it cannot clear a newer single-flight operation.
    this.ensureInFlight = null;
    this.bootstrapEnsureInFlight = null;
    this.rotateSessionToken();
    return this.terminateManagedProcess();
  }

  private isGenerationCurrent(generation: number): boolean {
    return generation === this.lifecycleGeneration;
  }

  private cancelledStatus(startedByApp: boolean): DesktopBackendStatus {
    return {
      state: "unavailable",
      message: "Backend startup was cancelled before authority could be established.",
      url: this.backendUrl,
      startedByApp,
    };
  }

  private terminationFailureStatus(startedByApp: boolean): DesktopBackendStatus {
    return {
      state: "unavailable",
      message: (
        "MemoLens could not confirm that the previous backend stopped; "
        + "no replacement backend was started."
      ),
      url: this.backendUrl,
      startedByApp,
    };
  }

  private startedStatus(message: string): DesktopBackendStatus {
    this.updateBackendTrust(true);
    return {
      state: "started",
      message,
      url: this.backendUrl,
      startedByApp: true,
    };
  }

  private spawnAttempt(
    projectRoot: string,
    pythonCommand: string,
    url: string,
    bootstrapRequestId: string | null = null,
  ): void {
    this.startError = null;
    const desktopSessionToken = this.getSessionToken();
    let authorityEnvironment: Record<string, string> = {};
    const protectedLogValues = [desktopSessionToken];
    if (this.getMainAuthorityToken && this.getRuntimeAuthorityEpoch) {
      const mainAuthorityToken = this.getMainAuthorityToken();
      const runtimeAuthorityEpoch = this.getRuntimeAuthorityEpoch();
      if (!mainAuthorityToken || !runtimeAuthorityEpoch) {
        throw new Error("Main authority runtime credentials must not be empty.");
      }
      authorityEnvironment = {
        MEMOLENS_MAIN_AUTHORITY_TOKEN: mainAuthorityToken,
        MEMOLENS_RUNTIME_AUTHORITY_EPOCH: runtimeAuthorityEpoch,
      };
      protectedLogValues.push(mainAuthorityToken, runtimeAuthorityEpoch);
    }
    const inheritedEnvironment = backendEnvironment({
      ...this.loadDotEnvVars(projectRoot),
      ...this.environment(),
    });
    const childEnvironment: NodeJS.ProcessEnv = {
      ...inheritedEnvironment,
      MEMOLENS_APP_STATE_DIR: this.getAppStateDir(),
      MEMOLENS_BACKEND_PORT: resolveBackendPort(url),
      MEMOLENS_BACKEND_DEBUG: "0",
      MEMOLENS_DESKTOP_SESSION_TOKEN: desktopSessionToken,
      ...authorityEnvironment,
      ...(bootstrapRequestId === null ? {} : {
        MEMOLENS_BOOTSTRAP_REQUEST_ID: bootstrapRequestId,
      }),
    };
    protectedLogValues.push(...environmentSecretValues(childEnvironment));
    const nextProcess = this.spawnProcess(pythonCommand, ["-I", "-u", "backend/app.py"], {
      cwd: projectRoot,
      env: childEnvironment,
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.managedProcess = nextProcess;
    this.attachLogging(nextProcess, protectedLogValues);
  }

  private captureManagedExecutableIdentity(
    command: string,
  ): ManagedExecutableIdentity | null {
    try {
      const identity = this.resolveExecutableIdentity(command);
      return isManagedExecutableIdentity(identity) ? identity : null;
    } catch {
      return null;
    }
  }

  private async admitManagedRuntime(
    projectRoot: string,
  ): Promise<ManagedRuntimeAdmissionResult> {
    const command = managedPythonCommand(projectRoot);
    const before = this.captureManagedExecutableIdentity(command);
    if (before === null) {
      return {
        admission: null,
        error: "The managed MemoLens Python runtime is missing or invalid. Run setup and try again.",
      };
    }

    const checked = await this.checkSQLiteRuntime(projectRoot, command);
    const after = this.captureManagedExecutableIdentity(command);
    if (after === null || !sameExecutableIdentity(before, after)) {
      return {
        admission: null,
        error: "The managed Python runtime changed during SQLite admission. Run setup and try again.",
      };
    }
    if (checked.error !== null) {
      return { admission: null, error: checked.error };
    }
    return {
      admission: {
        command,
        identity: after,
        runtime: checked.capability,
        health: healthCapabilityFromRuntime(checked.capability),
      },
      error: null,
    };
  }

  private spawnAdmittedAttempt(
    projectRoot: string,
    admission: ManagedRuntimeAdmission,
    url: string,
    bootstrapRequestId: string | null = null,
  ): string | null {
    const current = this.captureManagedExecutableIdentity(admission.command);
    if (current === null || !sameExecutableIdentity(current, admission.identity)) {
      return "The managed Python runtime changed after SQLite admission; MemoLens did not start it.";
    }
    this.spawnAttempt(projectRoot, admission.command, url, bootstrapRequestId);
    return null;
  }

  private async checkSQLiteRuntime(
    projectRoot: string,
    pythonCommand: string,
  ): Promise<SQLiteRuntimeCheckResult> {
    const result = await new Promise<{
      error: RuntimeCheckError | null;
      stdout: string;
      stderr: string;
    }>((resolve) => {
      let settled = false;
      const finish = (
        error: RuntimeCheckError | null,
        stdout: string,
        stderr: string,
      ): void => {
        if (settled) {
          return;
        }
        settled = true;
        resolve({ error, stdout, stderr });
      };
      try {
        this.execFileProcess(
          pythonCommand,
          ["-I", join(projectRoot, "scripts", "check_sqlite_runtime.py"), "--json"],
          {
            cwd: projectRoot,
            encoding: "utf8",
            env: runtimeCheckEnvironment(this.environment()),
            killSignal: "SIGKILL",
            maxBuffer: SQLITE_RUNTIME_CHECK_MAX_BUFFER_BYTES,
            shell: false,
            timeout: SQLITE_RUNTIME_CHECK_TIMEOUT_MS,
            windowsHide: true,
          },
          finish,
        );
      } catch (error) {
        finish(error instanceof Error ? error : new Error("runtime check failed"), "", "");
      }
    });

    if (
      Buffer.byteLength(result.stdout, "utf8") > SQLITE_RUNTIME_CHECK_MAX_BUFFER_BYTES
      || Buffer.byteLength(result.stderr, "utf8") > SQLITE_RUNTIME_CHECK_MAX_BUFFER_BYTES
      || result.error?.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER"
    ) {
      return { capability: null, error: (
        "MemoLens could not verify the configured Python runtime because its "
        + "SQLite check output exceeded the safety limit. Run "
        + "scripts/check_sqlite_runtime.py --json with that interpreter."
      ) };
    }
    if (result.error?.killed === true) {
      return { capability: null, error: (
        "The SQLite runtime check timed out before MemoLens could auto-start the backend. "
        + "Verify that the configured Python command starts promptly, then run "
        + "scripts/check_sqlite_runtime.py --json with it."
      ) };
    }

    let capability: unknown;
    try {
      capability = JSON.parse(result.stdout.trim());
    } catch {
      return { capability: null, error: (
        "The configured Python command did not return MemoLens's expected SQLite "
        + "runtime capability. Run scripts/check_sqlite_runtime.py --json with that interpreter."
      ) };
    }
    const parsed = parseSQLiteRuntimeCheck(capability);
    if (parsed?.classification === "unsafe") {
      return { capability: null, error: (
        "The configured Python runtime is unsafe for MemoLens writable WAL access. "
        + "Choose a runtime that passes scripts/check_sqlite_runtime.py --json."
      ) };
    }
    if (parsed?.classification !== "safe") {
      return { capability: null, error: (
        "The configured Python command did not return MemoLens's expected SQLite "
        + "runtime capability. Run scripts/check_sqlite_runtime.py --json with that interpreter."
      ) };
    }
    if (result.error !== null) {
      return { capability: null, error: (
        "MemoLens could not verify the configured Python runtime. Run "
        + "scripts/check_sqlite_runtime.py --json with that interpreter and select one that passes."
      ) };
    }
    return { capability: parsed.capability, error: null };
  }

  private attachLogging(processRef: ChildProcess, protectedValues: readonly string[]): void {
    const secrets = protectedSecrets(protectedValues);
    const stdoutRedactor = new StreamingSecretRedactor(secrets);
    const stderrRedactor = new StreamingSecretRedactor(secrets);
    const emitStdout = (value: string): void => {
      const message = value.trim();
      if (message.length > 0) {
        this.logger.log(`[memolens-backend] ${message}`);
      }
    };
    const emitStderr = (value: string): void => {
      const message = value.trim();
      if (message.length > 0) {
        this.logger.error(`[memolens-backend] ${message}`);
      }
    };
    processRef.on("error", (error: Error) => {
      const safeMessage = redactComplete(error.message, secrets);
      this.logger.error(`[memolens-backend] failed to start: ${safeMessage}`);
      if (
        this.managedProcess === processRef
        && this.terminatingProcess !== processRef
      ) {
        this.startError = safeMessage;
        this.managedProcess = null;
        this.rotateSessionToken();
      }
    });

    processRef.stdout?.on("data", (chunk: Buffer) => {
      emitStdout(stdoutRedactor.push(chunk));
    });
    processRef.stdout?.on("end", () => emitStdout(stdoutRedactor.flush()));
    processRef.stdout?.on("close", () => emitStdout(stdoutRedactor.flush()));

    processRef.stderr?.on("data", (chunk: Buffer) => {
      emitStderr(stderrRedactor.push(chunk));
    });
    processRef.stderr?.on("end", () => emitStderr(stderrRedactor.flush()));
    processRef.stderr?.on("close", () => emitStderr(stderrRedactor.flush()));

    processRef.on("exit", (code, signal) => {
      this.logger.log(
        `[memolens-backend] exited with code=${code ?? "null"} signal=${signal ?? "null"}`,
      );
      if (
        this.managedProcess === processRef
        && this.terminatingProcess !== processRef
      ) {
        this.managedProcess = null;
        this.rotateSessionToken();
      }
    });
  }

  private async waitForHealthy(
    url: string,
    expectedRuntime: SQLiteHealthCapability,
    generation: number,
  ): Promise<boolean> {
    const deadline = this.now() + this.startupTimeoutMs;
    while (this.now() < deadline) {
      if (!this.isGenerationCurrent(generation)) {
        return false;
      }
      const healthy = await this.probeHealth(url, expectedRuntime);
      if (!this.isGenerationCurrent(generation)) {
        return false;
      }
      if (healthy) {
        this.startError = null;
        return true;
      }

      if (this.startError) {
        return false;
      }

      if (
        this.managedProcess === null
        || this.processHasExited(this.managedProcess)
      ) {
        return false;
      }

      await this.sleep(this.healthPollIntervalMs);
      if (!this.isGenerationCurrent(generation)) {
        return false;
      }
    }
    return false;
  }

  private async waitForBootstrapCandidate(
    url: string,
    expectedRuntime: SQLiteHealthCapability,
    expectation: BootstrapCandidateRuntimeExpectation,
    generation: number,
  ): Promise<boolean> {
    const deadline = this.now() + this.startupTimeoutMs;
    while (this.now() < deadline) {
      if (!this.isGenerationCurrent(generation)) return false;
      const observed = await this.probeBootstrapRuntime!(
        url,
        expectation,
        expectedRuntime,
      );
      if (!this.isGenerationCurrent(generation)) return false;
      if (observed === "expected") {
        this.startError = null;
        return true;
      }
      if (observed === "wrong" || this.startError) return false;
      if (this.managedProcess === null || this.processHasExited(this.managedProcess)) {
        return false;
      }
      await this.sleep(this.healthPollIntervalMs);
    }
    return false;
  }

  private loadDotEnvVars(projectRoot: string): Record<string, string> {
    const envVars: Record<string, string> = {};
    const envFiles = [
      join(projectRoot, ".env"),
      join(projectRoot, "backend", ".env"),
    ];

    for (const envPath of envFiles) {
      try {
        const content = this.readEnvFile(envPath);
        for (const rawLine of content.split("\n")) {
          const line = rawLine.trim();
          if (!line || line.startsWith("#") || !line.includes("=")) {
            continue;
          }
          const eqIndex = line.indexOf("=");
          const key = line.slice(0, eqIndex).trim();
          let value = line.slice(eqIndex + 1).trim();
          if (
            value.length >= 2
            && value[0] === value[value.length - 1]
            && (value[0] === "'" || value[0] === "\"" || value[0] === "`")
          ) {
            value = value.slice(1, -1);
          }
          if (key) {
            envVars[key] = value;
          }
        }
      } catch {
        // .env file may not exist; that is fine.
      }
    }

    return envVars;
  }

  private processHasExited(processRef: ChildProcess): boolean {
    return processRef.exitCode !== null || processRef.signalCode !== null;
  }

  private async signalAndWaitForExit(
    processRef: ChildProcess,
    signal: NodeJS.Signals,
    timeoutMs: number,
  ): Promise<boolean> {
    if (this.processHasExited(processRef)) {
      return true;
    }

    let exitObserved = false;
    let resolveExit!: () => void;
    const exited = new Promise<void>((resolve) => {
      resolveExit = resolve;
    });
    const markExited = (): void => {
      if (!exitObserved) {
        exitObserved = true;
        resolveExit();
      }
    };
    const cleanup = (): void => {
      processRef.removeListener("exit", markExited);
      processRef.removeListener("close", markExited);
    };
    processRef.once("exit", markExited);
    processRef.once("close", markExited);

    let signalAccepted = false;
    try {
      signalAccepted = processRef.kill(signal);
    } catch {
      signalAccepted = false;
    }

    if (exitObserved || this.processHasExited(processRef)) {
      cleanup();
      return true;
    }
    if (!signalAccepted) {
      cleanup();
      return false;
    }

    try {
      await Promise.race([exited, this.sleep(timeoutMs)]);
      return exitObserved || this.processHasExited(processRef);
    } catch {
      return exitObserved || this.processHasExited(processRef);
    } finally {
      cleanup();
    }
  }

  private async terminateManagedProcess(): Promise<boolean> {
    if (this.terminationInFlight !== null) {
      return this.terminationInFlight;
    }
    const processRef = this.managedProcess;
    if (processRef === null) {
      return true;
    }

    const operation = (async (): Promise<boolean> => {
      this.terminatingProcess = processRef;
      try {
        let terminated = this.processHasExited(processRef);
        if (!terminated) {
          terminated = await this.signalAndWaitForExit(
            processRef,
            "SIGTERM",
            this.terminationGraceMs,
          );
        }
        if (!terminated) {
          terminated = await this.signalAndWaitForExit(
            processRef,
            "SIGKILL",
            this.terminationForceMs,
          );
        }
        if (terminated && this.managedProcess === processRef) {
          this.managedProcess = null;
        }
        return terminated;
      } finally {
        if (this.terminatingProcess === processRef) {
          this.terminatingProcess = null;
        }
      }
    })();
    this.terminationInFlight = operation;
    try {
      return await operation;
    } finally {
      if (this.terminationInFlight === operation) {
        this.terminationInFlight = null;
      }
    }
  }
}
