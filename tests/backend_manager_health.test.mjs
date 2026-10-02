import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import { access, mkdir, mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import test from "node:test";

import { BackendProcessSupervisor } from "../electron-dist/electron/backendProcessSupervisor.js";
import {
  getDesktopSessionToken,
  isBackendIdentityVerified,
  markBackendTrustVerified,
  probeBackendHealth,
  revokeBackendTrust,
  verifyBackendHealthPayload,
} from "../electron-dist/electron/backendManager.js";
import {
  commitDesktopLibrarySelection,
  loadApprovedDesktopLibraryBinding,
  loadDesktopSettings,
  managedPythonCommand,
  resolveLibraryDbPath,
  saveDesktopSettings,
} from "../electron-dist/electron/desktopSettings.js";
import { LibrarySelectionAuthority } from "../electron-dist/electron/librarySelectionAuthority.js";

const challenge = "ab".repeat(32);
const SQLITE_POLICY_ID = "sqlite-wal-reset-2026-08-22-v1";
const safeHealthCapability = {
  policy_id: SQLITE_POLICY_ID,
  sqlite_version: "3.53.0",
  wal_reset_safe: true,
  journal_policy: "wal",
};
const safeRuntimeCapability = {
  object: "memolens.sqlite_runtime",
  schema_version: "1",
  policy_id: SQLITE_POLICY_ID,
  python_version: "3.14.0",
  sqlite_version: "3.53.0",
  wal_reset_safe: true,
  journal_policy: "wal",
  status: "ready",
  reason_code: null,
};
const identity = {
  status: "ok",
  object: "health.check",
  service: "memolens-backend",
  api_version: "1",
  sqlite_runtime: safeHealthCapability,
};

function healthProofMessage(health, healthChallenge = challenge) {
  return [
    "memolens.health.v1",
    `challenge=${healthChallenge}`,
    `object=${health.object}`,
    `status=${health.status}`,
    `service=${health.service}`,
    `api_version=${health.api_version}`,
    `policy_id=${health.sqlite_runtime.policy_id}`,
    `sqlite_version=${health.sqlite_runtime.sqlite_version}`,
    `wal_reset_safe=${health.sqlite_runtime.wal_reset_safe ? "true" : "false"}`,
    `journal_policy=${health.sqlite_runtime.journal_policy}`,
  ].join("\n");
}

function signedHealthPayload(health = identity, healthChallenge = challenge) {
  return {
    ...health,
    challenge_proof: createHmac("sha256", getDesktopSessionToken())
      .update(healthProofMessage(health, healthChallenge))
      .digest("hex"),
  };
}

function executableIdentity(command, generation = "1") {
  return {
    realpath: command,
    device: "1",
    inode: generation,
    size: "4096",
    mtimeNs: generation,
  };
}

const autoStartSettings = {
  pythonCommand: "python-custom",
  autoStartBackend: true,
  libraryConfigured: false,
  defaultLibraryDir: null,
  defaultDbPath: null,
};

class FakeChildProcess extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  exitCode = null;
  signalCode = null;
  killedSignals = [];

  constructor({ killBehavior } = {}) {
    super();
    this.killBehavior = killBehavior ?? (() => "exit");
  }

  kill(signal) {
    this.killedSignals.push(signal);
    const behavior = this.killBehavior(signal, this.killedSignals.length);
    if (behavior instanceof Error) {
      throw behavior;
    }
    if (behavior === false) {
      return false;
    }
    if (behavior === "stay") {
      return true;
    }
    this.signalCode = signal;
    this.emit("exit", null, signal);
    this.emit("close", null, signal);
    return true;
  }
}

function createSupervisorHarness({
  backendUrl = "http://127.0.0.1:5519",
  probeHealth = async () => false,
  processFactory = () => new FakeChildProcess(),
  readEnvFile = () => {
    throw new Error("missing .env");
  },
  environment = () => ({}),
  resolveExecutableIdentity = (command) => executableIdentity(command),
  runtimeCheckResult = {
    error: null,
    stdout: JSON.stringify(safeRuntimeCapability),
    stderr: "",
  },
  startupTimeoutMs = 30_000,
  healthPollIntervalMs = 500,
  retryDelayMs = 1_000,
  terminationGraceMs = 1_500,
  terminationForceMs = 1_500,
  sleep: sleepHook = async () => {},
} = {}) {
  const spawns = [];
  const processes = [];
  const healthUrls = [];
  const healthExpectations = [];
  const sleeps = [];
  const trustUpdates = [];
  const runtimeChecks = [];
  let currentTime = 0;
  let tokenNumber = 1;
  let tokenReads = 0;
  let trusted = true;
  let rotations = 0;

  const supervisor = new BackendProcessSupervisor({
    backendUrl,
    probeHealth: async (url, expectedRuntime) => {
      healthUrls.push(url);
      healthExpectations.push(expectedRuntime ?? null);
      return probeHealth(url, healthUrls.length, processes, expectedRuntime ?? null);
    },
    getSessionToken: () => {
      tokenReads += 1;
      return `token-${tokenNumber}`;
    },
    rotateSessionToken: () => {
      rotations += 1;
      tokenNumber += 1;
      trusted = false;
    },
    updateBackendTrust: (value) => {
      trusted = value;
      trustUpdates.push(value);
    },
    getAppStateDir: () => "/memo-state",
    spawnProcess: (command, args, options) => {
      const processRef = processFactory(processes.length);
      processes.push(processRef);
      spawns.push({ command, args, options });
      return processRef;
    },
    execFileProcess: (command, args, options, callback) => {
      runtimeChecks.push({ command, args, options });
      queueMicrotask(() => callback(
        runtimeCheckResult.error,
        runtimeCheckResult.stdout,
        runtimeCheckResult.stderr,
      ));
      return new FakeChildProcess();
    },
    resolveExecutableIdentity,
    readEnvFile,
    environment,
    now: () => currentTime,
    sleep: async (durationMs) => {
      sleeps.push(durationMs);
      currentTime += durationMs;
      await sleepHook(durationMs);
    },
    logger: { log() {}, error() {} },
    startupTimeoutMs,
    healthPollIntervalMs,
    retryDelayMs,
    terminationGraceMs,
    terminationForceMs,
  });

  return {
    supervisor,
    spawns,
    processes,
    healthUrls,
    healthExpectations,
    sleeps,
    trustUpdates,
    runtimeChecks,
    get rotations() {
      return rotations;
    },
    get tokenReads() {
      return tokenReads;
    },
    get trusted() {
      return trusted;
    },
  };
}

test("rejects arbitrary 2xx and spoofed MemoLens identity", () => {
  assert.equal(verifyBackendHealthPayload({ status: "ok" }, challenge), false);
  assert.equal(
    verifyBackendHealthPayload({ ...identity, challenge_proof: "00".repeat(32) }, challenge),
    false,
  );
});

test("accepts only a valid session-token challenge proof", () => {
  const verified = signedHealthPayload();
  assert.equal(
    verifyBackendHealthPayload(verified, challenge),
    true,
  );
  assert.equal(
    verifyBackendHealthPayload(
      { ...verified, challenge_proof: `${verified.challenge_proof}junk` },
      challenge,
    ),
    false,
  );

  const newerRuntime = { ...safeHealthCapability, sqlite_version: "3.53.1" };
  const newerPayload = signedHealthPayload({ ...identity, sqlite_runtime: newerRuntime });
  assert.equal(verifyBackendHealthPayload(newerPayload, challenge), true);
  assert.equal(
    verifyBackendHealthPayload(newerPayload, challenge, safeHealthCapability),
    false,
  );
});

test("requires an exact policy-safe SQLite capability bound into the HMAC proof", () => {
  const verified = signedHealthPayload();

  assert.equal(verifyBackendHealthPayload(verified, challenge), true);
  for (const sqlite_runtime of [
    undefined,
    null,
    { ...safeHealthCapability, wal_reset_safe: false },
    { ...safeHealthCapability, journal_policy: "delete" },
    { ...safeHealthCapability, sqlite_version: "unknown" },
    { ...safeHealthCapability, sqlite_version: "3.47.1" },
    { ...safeHealthCapability, sqlite_version: "3.52.0" },
    { ...safeHealthCapability, sqlite_version: "3.52.1" },
    { ...safeHealthCapability, policy_id: "stale-policy" },
    { ...safeHealthCapability, python_executable: "/private/python" },
  ]) {
    assert.equal(
      verifyBackendHealthPayload({ ...verified, sqlite_runtime }, challenge),
      false,
    );
  }

  assert.equal(
    verifyBackendHealthPayload({ ...verified, object: "not-health.check" }, challenge),
    false,
  );
  assert.equal(
    verifyBackendHealthPayload({ ...verified, unexpected: true }, challenge),
    false,
  );
  assert.equal(
    verifyBackendHealthPayload(
      {
        ...verified,
        sqlite_runtime: { ...safeHealthCapability, sqlite_version: "3.53.1" },
      },
      challenge,
    ),
    false,
  );
});

test("health probe bounds declared and streamed response bodies", async () => {
  const originalFetch = globalThis.fetch;
  let oversizedDeclaredReads = 0;
  let oversizedDeclaredCancels = 0;
  try {
    globalThis.fetch = async () => ({
      ok: true,
      headers: { get: () => "999999" },
      body: {
        cancel: async () => {
          oversizedDeclaredCancels += 1;
        },
        getReader: () => ({
          read: async () => {
            oversizedDeclaredReads += 1;
            return { done: true, value: undefined };
          },
        }),
      },
    });
    assert.equal(await probeBackendHealth("http://127.0.0.1:5519"), false);
    assert.equal(oversizedDeclaredReads, 0);
    assert.equal(oversizedDeclaredCancels, 1);

    let streamedReads = 0;
    let streamedCancels = 0;
    let released = 0;
    globalThis.fetch = async () => ({
      ok: true,
      headers: { get: () => null },
      body: {
        getReader: () => ({
          read: async () => {
            streamedReads += 1;
            return {
              done: false,
              value: new Uint8Array(5_000),
            };
          },
          cancel: async () => {
            streamedCancels += 1;
          },
          releaseLock: () => {
            released += 1;
          },
        }),
      },
    });
    assert.equal(await probeBackendHealth("http://127.0.0.1:5519"), false);
    assert.equal(streamedReads, 2);
    assert.equal(streamedCancels, 1);
    assert.equal(released, 1);

    globalThis.fetch = async (input) => {
      const requested = new URL(String(input));
      const healthChallenge = requested.searchParams.get("challenge");
      assert.ok(healthChallenge);
      return new Response(JSON.stringify(signedHealthPayload(identity, healthChallenge)), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    };
    assert.equal(
      await probeBackendHealth("http://127.0.0.1:5519", safeHealthCapability),
      true,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("revokes trust and rotates the bearer when backend identity is lost", () => {
  const priorToken = getDesktopSessionToken();
  markBackendTrustVerified();
  assert.equal(isBackendIdentityVerified(), true);

  revokeBackendTrust();

  assert.equal(isBackendIdentityVerified(), false);
  assert.notEqual(getDesktopSessionToken(), priorToken);
});

test("persists only a native-ticket-approved library and rejects renderer path settings", async () => {
  const appStateDir = await mkdtemp(join(tmpdir(), "memolens-desktop-state-"));
  const previousAppStateDir = process.env.MEMOLENS_APP_STATE_DIR;
  process.env.MEMOLENS_APP_STATE_DIR = appStateDir;

  try {
    const cleanSettings = await loadDesktopSettings(process.cwd());
    assert.equal(cleanSettings.libraryConfigured, false);

    const libraryPath = join(appStateDir, "outside", "photos");
    const equivalentPath = join(libraryPath, "child", "..");
    const dbPath = resolveLibraryDbPath(libraryPath);
    assert.equal(dbPath, resolveLibraryDbPath(equivalentPath));
    assert.equal(dirname(dbPath), join(appStateDir, "storage"));
    assert.match(basename(dbPath), /^photo-index-[0-9a-f]{24}\.db$/);
    assert.equal(basename(dbPath).includes("photos"), false);

    await assert.rejects(
      saveDesktopSettings(process.cwd(), {
        autoStartBackend: false,
        libraryConfigured: true,
        defaultLibraryDir: libraryPath,
        defaultDbPath: dbPath,
      }),
      /renderer-controlled library or runtime settings/,
    );
    const afterRejectedRendererSettings = await loadDesktopSettings(process.cwd());
    assert.equal(afterRejectedRendererSettings.autoStartBackend, true);
    assert.equal(afterRejectedRendererSettings.libraryConfigured, false);
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
    await assert.rejects(access(join(appStateDir, "desktop-settings.json")), { code: "ENOENT" });

    await mkdir(libraryPath, { recursive: true });
    const authority = new LibrarySelectionAuthority();
    const selection = await authority.issueSelection(libraryPath);
    const canonicalLibraryPath = await realpath(libraryPath);
    const canonicalDbPath = resolveLibraryDbPath(canonicalLibraryPath);
    assert.equal(selection.folderPath, canonicalLibraryPath);
    assert.equal(selection.dbPath, canonicalDbPath);
    assert.ok(selection.selectionTicket.length >= 32);
    const verified = await authority.redeemSelection(selection.selectionTicket);
    const committed = await commitDesktopLibrarySelection(process.cwd(), verified);
    await verified.release();
    assert.equal(committed.libraryConfigured, true);
    assert.equal(committed.defaultLibraryDir, canonicalLibraryPath);
    assert.equal(committed.defaultDbPath, canonicalDbPath);
    assert.equal(committed.pythonCommand, managedPythonCommand(process.cwd()));

    const updated = await saveDesktopSettings(process.cwd(), { autoStartBackend: false });
    assert.equal(updated.autoStartBackend, false);
    assert.equal(updated.defaultLibraryDir, canonicalLibraryPath);
    assert.equal(updated.defaultDbPath, canonicalDbPath);
  } finally {
    if (previousAppStateDir === undefined) {
      delete process.env.MEMOLENS_APP_STATE_DIR;
    } else {
      process.env.MEMOLENS_APP_STATE_DIR = previousAppStateDir;
    }
    await rm(appStateDir, { recursive: true, force: true });
  }
});

test("pins desktop Python to the platform-specific project managed virtualenv", () => {
  assert.equal(
    managedPythonCommand("/project", "linux"),
    join("/project", ".venv", "bin", "python"),
  );
  assert.equal(
    managedPythonCommand("C:\\project", "win32"),
    join("C:\\project", ".venv", "Scripts", "python.exe"),
  );
});

test("spawns one first attempt with the original dotenv and forced environment precedence", async () => {
  const projectRoot = join(tmpdir(), "memolens-supervisor-project");
  const healthResults = [false, true];
  const harness = createSupervisorHarness({
    backendUrl: " http://127.0.0.1:7711/// ",
    probeHealth: async () => healthResults.shift() ?? false,
    readEnvFile: (path) => {
      if (path === join(projectRoot, ".env")) {
        return [
          "DOT_ONLY=dot",
          "SHARED=project",
          "MEMOLENS_BACKEND_PORT=9999",
          "QUOTED=\"hello world\"",
          "PYTHONPATH=/attacker/dotenv",
          "OPENAI_API_KEY=dot-secret",
        ].join("\n");
      }
      if (path === join(projectRoot, "backend", ".env")) {
        return "SHARED=backend\nBACKEND_ONLY=backend";
      }
      throw new Error("unexpected env path");
    },
    environment: () => ({
      PATH: "/usr/bin",
      LANG: "en_US.UTF-8",
      SHARED: "process",
      PROCESS_ONLY: "process",
      MEMOLENS_APP_STATE_DIR: "/untrusted-state",
      MEMOLENS_BACKEND_DEBUG: "9",
      PYTHONPATH: "/attacker/process",
      PYTHONHOME: "/attacker/home",
      PYTHONUTF8: "1",
      OPENAI_API_KEY: "process-secret",
      MEMOLENS_MAIN_AUTHORITY_TOKEN: "must-not-reach-checker",
    }),
  });

  const status = await harness.supervisor.ensureReady(projectRoot, autoStartSettings);

  assert.deepEqual(status, {
    state: "started",
    message: "Local backend started by the desktop app.",
    url: "http://127.0.0.1:7711",
    startedByApp: true,
  });
  assert.equal(harness.spawns.length, 1);
  assert.equal(harness.runtimeChecks.length, 1);
  const expectedPython = managedPythonCommand(projectRoot);
  assert.equal(harness.runtimeChecks[0].command, expectedPython);
  assert.deepEqual(harness.runtimeChecks[0].args, [
    "-I",
    join(projectRoot, "scripts", "check_sqlite_runtime.py"),
    "--json",
  ]);
  assert.equal(harness.runtimeChecks[0].options.cwd, projectRoot);
  assert.equal(harness.runtimeChecks[0].options.shell, false);
  assert.ok(harness.runtimeChecks[0].options.timeout > 0);
  assert.ok(harness.runtimeChecks[0].options.timeout <= 5_000);
  assert.ok(harness.runtimeChecks[0].options.maxBuffer > 0);
  assert.ok(harness.runtimeChecks[0].options.maxBuffer <= 16_384);
  assert.deepEqual(harness.runtimeChecks[0].options.env, {
    PATH: "/usr/bin",
    LANG: "en_US.UTF-8",
  });
  assert.equal(harness.spawns[0].command, expectedPython);
  assert.deepEqual(harness.spawns[0].args, ["-I", "-u", "backend/app.py"]);
  assert.equal(harness.spawns[0].options.cwd, projectRoot);
  assert.deepEqual(harness.spawns[0].options.stdio, ["ignore", "pipe", "pipe"]);
  assert.deepEqual(harness.spawns[0].options.env, {
    DOT_ONLY: "dot",
    SHARED: "process",
    MEMOLENS_BACKEND_PORT: "7711",
    QUOTED: "hello world",
    BACKEND_ONLY: "backend",
    PROCESS_ONLY: "process",
    PATH: "/usr/bin",
    LANG: "en_US.UTF-8",
    OPENAI_API_KEY: "process-secret",
    MEMOLENS_APP_STATE_DIR: "/memo-state",
    MEMOLENS_BACKEND_DEBUG: "0",
    MEMOLENS_DESKTOP_SESSION_TOKEN: "token-1",
  });
  assert.equal(harness.spawns[0].options.env.PYTHONPATH, undefined);
  assert.equal(harness.spawns[0].options.env.PYTHONHOME, undefined);
  assert.equal(harness.spawns[0].options.env.PYTHONUTF8, undefined);
  assert.deepEqual(harness.healthUrls, [
    "http://127.0.0.1:7711",
    "http://127.0.0.1:7711",
  ]);
  assert.deepEqual(harness.trustUpdates, [false, true]);
  assert.equal(harness.rotations, 0);
  assert.deepEqual(harness.healthExpectations, [null, safeHealthCapability]);
});

test("fails before secrets or spawn when managed executable identity changes", async () => {
  let identityReads = 0;
  const harness = createSupervisorHarness({
    resolveExecutableIdentity: (command) => {
      identityReads += 1;
      return executableIdentity(command, identityReads < 3 ? "1" : "2");
    },
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.equal(status.state, "unavailable");
  assert.match(status.message, /managed Python runtime changed/i);
  assert.equal(harness.runtimeChecks.length, 1);
  assert.equal(harness.spawns.length, 0);
  assert.equal(harness.tokenReads, 0);
});

test("does not spawn for an already healthy backend or when auto-start is disabled", async () => {
  const connectedHarness = createSupervisorHarness({
    probeHealth: async () => true,
  });
  const connected = await connectedHarness.supervisor.ensureReady(
    "/project",
    autoStartSettings,
  );
  assert.deepEqual(connected, {
    state: "connected",
    message: "Local backend is online.",
    url: "http://127.0.0.1:5519",
    startedByApp: false,
  });
  assert.equal(connectedHarness.spawns.length, 0);
  assert.equal(connectedHarness.runtimeChecks.length, 0);
  assert.deepEqual(connectedHarness.trustUpdates, [false, true]);

  const disabledHarness = createSupervisorHarness();
  const disabled = await disabledHarness.supervisor.ensureReady("/project", {
    ...autoStartSettings,
    autoStartBackend: false,
  });
  assert.deepEqual(disabled, {
    state: "unavailable",
    message: "Backend is offline. Enable auto-start or launch the Python service manually.",
    url: "http://127.0.0.1:5519",
    startedByApp: false,
  });
  assert.equal(disabledHarness.spawns.length, 0);
  assert.equal(disabledHarness.runtimeChecks.length, 0);
  assert.deepEqual(disabledHarness.trustUpdates, [false]);
});

test("ensure-backend IPC accepts no renderer settings and uses only main-loaded configuration", () => {
  const mainSource = readFileSync("electron/main.ts", "utf-8");
  assert.match(
    mainSource,
    /handle\("memolens:ensure-backend", async \(event\) => \{\s*assertTrustedIpcSender\(event, trustedRendererEntries\);\s*const settings = await loadDesktopSettings\(PROJECT_ROOT\);\s*const status = await ensureBackendReady\(PROJECT_ROOT, settings\);/,
  );
  const preloadSource = readFileSync("electron/preload.cts", "utf-8");
  assert.match(preloadSource, /ensureBackend\(\): Promise<DesktopBackendStatus>/);
  assert.doesNotMatch(preloadSource, /ensureBackend\([^)]*settings/);
});

test("fails closed before spawn for unsafe, malformed, timed-out, or oversized probes", async () => {
  const unsafeCapability = {
    ...safeRuntimeCapability,
    sqlite_version: "3.47.1",
    wal_reset_safe: false,
    status: "unsafe",
    reason_code: "sqlite_wal_reset_unsafe",
  };
  const timeoutError = Object.assign(new Error("secret-timeout-detail"), { killed: true });
  const oversizedError = Object.assign(new Error("stdout maxBuffer exceeded"), {
    code: "ERR_CHILD_PROCESS_STDIO_MAXBUFFER",
  });
  const cases = [
    {
      name: "self-attested-withdrawn-version",
      result: {
        error: null,
        stdout: JSON.stringify({
          ...safeRuntimeCapability,
          sqlite_version: "3.52.0",
        }),
        stderr: "",
      },
      expected: /expected SQLite runtime capability/,
    },
    {
      name: "unsafe",
      result: { error: Object.assign(new Error("exit 78"), { code: 78 }), stdout: JSON.stringify(unsafeCapability), stderr: "" },
      expected: /unsafe for MemoLens writable WAL access/,
    },
    {
      name: "malformed",
      result: { error: null, stdout: "{not-json", stderr: "" },
      expected: /expected SQLite runtime capability/,
    },
    {
      name: "timeout",
      result: { error: timeoutError, stdout: "", stderr: "" },
      expected: /timed out/,
    },
    {
      name: "oversized",
      result: { error: oversizedError, stdout: "", stderr: "" },
      expected: /could not verify the configured Python runtime/,
    },
  ];

  for (const current of cases) {
    const harness = createSupervisorHarness({ runtimeCheckResult: current.result });
    const status = await harness.supervisor.ensureReady("/project", autoStartSettings);
    assert.equal(status.state, "unavailable", current.name);
    assert.equal(status.startedByApp, false, current.name);
    assert.match(status.message, current.expected, current.name);
    assert.equal(status.message.includes("secret-timeout-detail"), false, current.name);
    assert.equal(harness.runtimeChecks.length, 1, current.name);
    assert.equal(harness.spawns.length, 0, current.name);
    assert.equal(harness.tokenReads, 0, current.name);
    assert.equal(harness.trusted, false, current.name);
    assert.deepEqual(harness.trustUpdates, [false], current.name);
  }
});

test("retries exactly once with a rotated token after an exited first attempt", async () => {
  const healthResults = [false, false, true];
  let identityReads = 0;
  const harness = createSupervisorHarness({
    probeHealth: async () => healthResults.shift() ?? false,
    resolveExecutableIdentity: (command) => {
      identityReads += 1;
      return executableIdentity(command);
    },
    processFactory: (index) => {
      const processRef = new FakeChildProcess();
      if (index === 0) {
        processRef.exitCode = 1;
      }
      return processRef;
    },
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.equal(status.state, "started");
  assert.equal(status.message, "Local backend started by the desktop app (retry succeeded).");
  assert.equal(harness.spawns.length, 2);
  assert.equal(harness.runtimeChecks.length, 2);
  assert.equal(identityReads, 6);
  assert.equal(harness.spawns[0].options.env.MEMOLENS_DESKTOP_SESSION_TOKEN, "token-1");
  assert.equal(harness.spawns[1].options.env.MEMOLENS_DESKTOP_SESSION_TOKEN, "token-2");
  assert.deepEqual(harness.processes[0].killedSignals, []);
  assert.deepEqual(harness.sleeps, [1_000]);
  assert.equal(harness.rotations, 1);
  assert.deepEqual(harness.trustUpdates, [false, true]);
});

test("retains the final child start error and revokes each failed token", async () => {
  const harness = createSupervisorHarness({
    probeHealth: async (_url, probeNumber, processes) => {
      if (probeNumber === 2) {
        processes[0].emit("error", new Error("first spawn failed"));
      }
      if (probeNumber === 3) {
        processes[1].emit("error", new Error("second spawn failed"));
      }
      return false;
    },
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.deepEqual(status, {
    state: "unavailable",
    message: "Backend failed to start: second spawn failed",
    url: "http://127.0.0.1:5519",
    startedByApp: true,
  });
  assert.equal(harness.spawns.length, 2);
  assert.equal(harness.spawns[0].options.env.MEMOLENS_DESKTOP_SESSION_TOKEN, "token-1");
  assert.equal(harness.spawns[1].options.env.MEMOLENS_DESKTOP_SESSION_TOKEN, "token-3");
  assert.equal(harness.rotations, 3);
  assert.equal(harness.trusted, false);
});

test("ignores a stale child error after retry ownership has moved", async () => {
  const harness = createSupervisorHarness({
    probeHealth: async (_url, probeNumber, processes) => {
      if (probeNumber === 3) {
        processes[0].emit("error", new Error("stale first-child error"));
      }
      return probeNumber === 4;
    },
    processFactory: (index) => {
      const processRef = new FakeChildProcess();
      if (index === 0) {
        processRef.exitCode = 1;
      }
      return processRef;
    },
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.equal(status.state, "started");
  assert.equal(status.message, "Local backend started by the desktop app (retry succeeded).");
  assert.equal(harness.spawns.length, 2);
  assert.equal(harness.rotations, 1);
  assert.equal(harness.trusted, true);
});

test("keeps the startup deadline, polling interval, and single retry bounded", async () => {
  const harness = createSupervisorHarness({
    startupTimeoutMs: 10,
    healthPollIntervalMs: 5,
    retryDelayMs: 7,
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.deepEqual(status, {
    state: "unavailable",
    message: (
      "Backend did not become healthy. Check the Python environment configured "
      + "for the managed MemoLens runtime."
    ),
    url: "http://127.0.0.1:5519",
    startedByApp: true,
  });
  assert.equal(harness.spawns.length, 2);
  assert.equal(harness.healthUrls.length, 5);
  assert.deepEqual(harness.sleeps, [5, 5, 7, 5, 5]);
  assert.equal(harness.rotations, 1);
});

test("coalesces concurrent ensure calls into one bounded startup with no orphan child", async () => {
  const harness = createSupervisorHarness({
    startupTimeoutMs: 0,
    retryDelayMs: 0,
  });

  const statuses = await Promise.all(
    Array.from(
      { length: 12 },
      () => harness.supervisor.ensureReady("/project", autoStartSettings),
    ),
  );

  assert.equal(statuses.every((status) => status.state === "unavailable"), true);
  assert.equal(harness.runtimeChecks.length, 2);
  assert.equal(harness.spawns.length, 2);
  assert.equal(harness.tokenReads, 2);
  assert.deepEqual(harness.processes[0].killedSignals, ["SIGTERM"]);
  assert.deepEqual(harness.processes[1].killedSignals, []);

  await harness.supervisor.stop();

  assert.deepEqual(harness.processes[1].killedSignals, ["SIGTERM"]);
  assert.equal(harness.rotations, 2);
});

test("stop invalidates an in-flight generation before it can retry", async () => {
  let releaseRetrySleep;
  let markRetrySleepStarted;
  const retrySleepStarted = new Promise((resolve) => {
    markRetrySleepStarted = resolve;
  });
  const harness = createSupervisorHarness({
    startupTimeoutMs: 0,
    retryDelayMs: 9,
    sleep: async (durationMs) => {
      if (durationMs === 9) {
        markRetrySleepStarted();
        await new Promise((resolve) => {
          releaseRetrySleep = resolve;
        });
      }
    },
  });

  const startup = harness.supervisor.ensureReady("/project", autoStartSettings);
  await retrySleepStarted;
  await harness.supervisor.stop();
  releaseRetrySleep();
  const status = await startup;

  assert.deepEqual(status, {
    state: "unavailable",
    message: "Backend startup was cancelled before authority could be established.",
    url: "http://127.0.0.1:5519",
    startedByApp: true,
  });
  assert.equal(harness.runtimeChecks.length, 1);
  assert.equal(harness.spawns.length, 1);
  assert.deepEqual(harness.processes[0].killedSignals, ["SIGTERM"]);
  assert.equal(harness.rotations, 2);
});

test("stop revokes trust immediately and terminates only the live managed child", async () => {
  const healthResults = [false, true];
  const harness = createSupervisorHarness({
    probeHealth: async () => healthResults.shift() ?? false,
  });

  await harness.supervisor.ensureReady("/project", autoStartSettings);
  const processRef = harness.processes[0];
  assert.equal(harness.trusted, true);

  await harness.supervisor.stop();

  assert.equal(harness.trusted, false);
  assert.equal(harness.rotations, 1);
  assert.deepEqual(processRef.killedSignals, ["SIGTERM"]);

  processRef.exitCode = 0;
  processRef.emit("exit", 0, "SIGTERM");
  assert.equal(harness.rotations, 1);
});

test("does not spawn a replacement while the prior child ignores termination", async () => {
  const harness = createSupervisorHarness({
    startupTimeoutMs: 0,
    retryDelayMs: 0,
    terminationGraceMs: 3,
    terminationForceMs: 4,
    processFactory: () => new FakeChildProcess({
      killBehavior: () => "stay",
    }),
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.deepEqual(status, {
    state: "unavailable",
    message: (
      "MemoLens could not confirm that the previous backend stopped; "
      + "no replacement backend was started."
    ),
    url: "http://127.0.0.1:5519",
    startedByApp: true,
  });
  assert.equal(harness.spawns.length, 1);
  assert.equal(harness.runtimeChecks.length, 1);
  assert.deepEqual(harness.processes[0].killedSignals, ["SIGTERM", "SIGKILL"]);
  assert.deepEqual(harness.sleeps, [3, 4]);
  assert.equal(harness.rotations, 1);

  const stopped = await harness.supervisor.stop();
  assert.equal(stopped, false);
  assert.equal(harness.spawns.length, 1);
  assert.deepEqual(
    harness.processes[0].killedSignals,
    ["SIGTERM", "SIGKILL", "SIGTERM", "SIGKILL"],
  );
});

test("treats kill false as unconfirmed and never starts a second writer", async () => {
  const harness = createSupervisorHarness({
    startupTimeoutMs: 0,
    retryDelayMs: 0,
    processFactory: () => new FakeChildProcess({
      killBehavior: () => false,
    }),
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.equal(status.state, "unavailable");
  assert.match(status.message, /could not confirm that the previous backend stopped/);
  assert.equal(harness.spawns.length, 1);
  assert.deepEqual(harness.processes[0].killedSignals, ["SIGTERM", "SIGKILL"]);
  assert.deepEqual(harness.sleeps, []);
});

test("escalates to SIGKILL before one bounded replacement attempt", async () => {
  const healthResults = [false, false, true];
  const harness = createSupervisorHarness({
    probeHealth: async () => healthResults.shift() ?? false,
    startupTimeoutMs: 1,
    healthPollIntervalMs: 1,
    retryDelayMs: 0,
    terminationGraceMs: 3,
    terminationForceMs: 4,
    processFactory: (index) => new FakeChildProcess({
      killBehavior: index === 0
        ? (signal) => signal === "SIGTERM" ? "stay" : "exit"
        : () => "exit",
    }),
  });

  const status = await harness.supervisor.ensureReady("/project", autoStartSettings);

  assert.equal(status.state, "started");
  assert.equal(status.message, "Local backend started by the desktop app (retry succeeded).");
  assert.equal(harness.spawns.length, 2);
  assert.deepEqual(harness.processes[0].killedSignals, ["SIGTERM", "SIGKILL"]);
  assert.deepEqual(harness.sleeps, [1, 3, 0]);
  assert.equal(harness.rotations, 1);
});

test("an immediate ensure waits for stop to confirm exit before spawning", async () => {
  let releaseTermination;
  let markTerminationWaiting;
  const terminationWaiting = new Promise((resolve) => {
    markTerminationWaiting = resolve;
  });
  const healthResults = [false, true, false, true];
  const harness = createSupervisorHarness({
    probeHealth: async () => healthResults.shift() ?? false,
    terminationGraceMs: 23,
    terminationForceMs: 29,
    processFactory: (index) => new FakeChildProcess({
      killBehavior: index === 0
        ? (signal) => signal === "SIGTERM" ? "stay" : "exit"
        : () => "exit",
    }),
    sleep: async (durationMs) => {
      if (durationMs === 23) {
        markTerminationWaiting();
        await new Promise((resolve) => {
          releaseTermination = resolve;
        });
      }
    },
  });

  const initial = await harness.supervisor.ensureReady("/project", autoStartSettings);
  assert.equal(initial.state, "started");

  const stopping = harness.supervisor.stop();
  await terminationWaiting;
  const restarting = harness.supervisor.ensureReady("/project", autoStartSettings);
  await Promise.resolve();
  assert.equal(harness.spawns.length, 1);

  releaseTermination();
  assert.equal(await stopping, true);
  const restarted = await restarting;
  assert.equal(restarted.state, "started");
  assert.equal(harness.spawns.length, 2);
  assert.deepEqual(harness.processes[0].killedSignals, ["SIGTERM", "SIGKILL"]);
});
