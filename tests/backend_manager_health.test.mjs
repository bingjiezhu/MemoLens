import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { EventEmitter } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import test from "node:test";

import { BackendProcessSupervisor } from "../electron-dist/electron/backendProcessSupervisor.js";
import {
  getDesktopSessionToken,
  isBackendIdentityVerified,
  markBackendTrustVerified,
  revokeBackendTrust,
  verifyBackendHealthPayload,
} from "../electron-dist/electron/backendManager.js";
import {
  loadDesktopSettings,
  resolveLibraryDbPath,
  saveDesktopSettings,
} from "../electron-dist/electron/desktopSettings.js";

const challenge = "ab".repeat(32);
const identity = {
  status: "ok",
  service: "memolens-backend",
  api_version: "1",
};

const autoStartSettings = {
  pythonCommand: "python-custom",
  autoStartBackend: true,
  defaultLibraryDir: null,
  defaultDbPath: null,
};

class FakeChildProcess extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  exitCode = null;
  killedSignals = [];

  kill(signal) {
    this.killedSignals.push(signal);
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
  startupTimeoutMs = 30_000,
  healthPollIntervalMs = 500,
  retryDelayMs = 1_000,
} = {}) {
  const spawns = [];
  const processes = [];
  const healthUrls = [];
  const sleeps = [];
  const trustUpdates = [];
  let currentTime = 0;
  let tokenNumber = 1;
  let trusted = true;
  let rotations = 0;

  const supervisor = new BackendProcessSupervisor({
    backendUrl,
    probeHealth: async (url) => {
      healthUrls.push(url);
      return probeHealth(url, healthUrls.length, processes);
    },
    getSessionToken: () => `token-${tokenNumber}`,
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
    readEnvFile,
    environment,
    now: () => currentTime,
    sleep: async (durationMs) => {
      sleeps.push(durationMs);
      currentTime += durationMs;
    },
    logger: { log() {}, error() {} },
    startupTimeoutMs,
    healthPollIntervalMs,
    retryDelayMs,
  });

  return {
    supervisor,
    spawns,
    processes,
    healthUrls,
    sleeps,
    trustUpdates,
    get rotations() {
      return rotations;
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
  const challengeProof = createHmac("sha256", getDesktopSessionToken())
    .update(challenge)
    .digest("hex");
  assert.equal(
    verifyBackendHealthPayload({ ...identity, challenge_proof: challengeProof }, challenge),
    true,
  );
  assert.equal(
    verifyBackendHealthPayload({ ...identity, challenge_proof: `${challengeProof}junk` }, challenge),
    false,
  );
});

test("revokes trust and rotates the bearer when backend identity is lost", () => {
  const priorToken = getDesktopSessionToken();
  markBackendTrustVerified();
  assert.equal(isBackendIdentityVerified(), true);

  revokeBackendTrust();

  assert.equal(isBackendIdentityVerified(), false);
  assert.notEqual(getDesktopSessionToken(), priorToken);
});

test("uses an opaque stable app-state database and persists the selected library", async () => {
  const appStateDir = await mkdtemp(join(tmpdir(), "memolens-desktop-state-"));
  const previousAppStateDir = process.env.MEMOLENS_APP_STATE_DIR;
  process.env.MEMOLENS_APP_STATE_DIR = appStateDir;

  try {
    const libraryPath = join(appStateDir, "outside", "photos");
    const equivalentPath = join(libraryPath, "child", "..");
    const dbPath = resolveLibraryDbPath(libraryPath);
    assert.equal(dbPath, resolveLibraryDbPath(equivalentPath));
    assert.equal(dirname(dbPath), join(appStateDir, "storage"));
    assert.match(basename(dbPath), /^photo-index-[0-9a-f]{24}\.db$/);
    assert.equal(basename(dbPath).includes("photos"), false);

    await saveDesktopSettings(process.cwd(), {
      pythonCommand: "python3",
      autoStartBackend: true,
      defaultLibraryDir: libraryPath,
      defaultDbPath: dbPath,
    });
    const restored = await loadDesktopSettings(process.cwd());
    assert.equal(restored.defaultLibraryDir, libraryPath);
    assert.equal(restored.defaultDbPath, dbPath);
  } finally {
    if (previousAppStateDir === undefined) {
      delete process.env.MEMOLENS_APP_STATE_DIR;
    } else {
      process.env.MEMOLENS_APP_STATE_DIR = previousAppStateDir;
    }
    await rm(appStateDir, { recursive: true, force: true });
  }
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
        ].join("\n");
      }
      if (path === join(projectRoot, "backend", ".env")) {
        return "SHARED=backend\nBACKEND_ONLY=backend";
      }
      throw new Error("unexpected env path");
    },
    environment: () => ({
      SHARED: "process",
      PROCESS_ONLY: "process",
      MEMOLENS_APP_STATE_DIR: "/untrusted-state",
      MEMOLENS_BACKEND_DEBUG: "9",
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
  assert.equal(harness.spawns[0].command, "python-custom");
  assert.deepEqual(harness.spawns[0].args, ["backend/app.py"]);
  assert.equal(harness.spawns[0].options.cwd, projectRoot);
  assert.deepEqual(harness.spawns[0].options.stdio, ["ignore", "pipe", "pipe"]);
  assert.deepEqual(harness.spawns[0].options.env, {
    DOT_ONLY: "dot",
    SHARED: "process",
    MEMOLENS_BACKEND_PORT: "7711",
    QUOTED: "hello world",
    BACKEND_ONLY: "backend",
    PROCESS_ONLY: "process",
    MEMOLENS_APP_STATE_DIR: "/memo-state",
    MEMOLENS_BACKEND_DEBUG: "0",
    MEMOLENS_DESKTOP_SESSION_TOKEN: "token-1",
    PYTHONUNBUFFERED: "1",
  });
  assert.deepEqual(harness.healthUrls, [
    "http://127.0.0.1:7711",
    "http://127.0.0.1:7711",
  ]);
  assert.deepEqual(harness.trustUpdates, [false, true]);
  assert.equal(harness.rotations, 0);
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
  assert.deepEqual(disabledHarness.trustUpdates, [false]);
});

test("retries exactly once with a rotated token after an exited first attempt", async () => {
  const healthResults = [false, false, true];
  const harness = createSupervisorHarness({
    probeHealth: async () => healthResults.shift() ?? false,
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
      + "in the desktop settings (python-custom)."
    ),
    url: "http://127.0.0.1:5519",
    startedByApp: true,
  });
  assert.equal(harness.spawns.length, 2);
  assert.equal(harness.healthUrls.length, 5);
  assert.deepEqual(harness.sleeps, [5, 5, 7, 5, 5]);
  assert.equal(harness.rotations, 1);
});

test("stop revokes trust immediately and terminates only the live managed child", async () => {
  const healthResults = [false, true];
  const harness = createSupervisorHarness({
    probeHealth: async () => healthResults.shift() ?? false,
  });

  await harness.supervisor.ensureReady("/project", autoStartSettings);
  const processRef = harness.processes[0];
  assert.equal(harness.trusted, true);

  harness.supervisor.stop();

  assert.equal(harness.trusted, false);
  assert.equal(harness.rotations, 1);
  assert.deepEqual(processRef.killedSignals, ["SIGTERM"]);

  processRef.exitCode = 0;
  processRef.emit("exit", 0, "SIGTERM");
  assert.equal(harness.rotations, 2);
});
