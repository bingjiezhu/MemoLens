import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { EventEmitter } from "node:events";
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  commitLibraryBootstrapCandidate,
  getDesktopSessionToken,
  matchesLibraryBootstrapRuntime,
  verifyBackendHealthV2Payload,
} from "../electron-dist/electron/backendManager.js";
import { BackendProcessSupervisor } from "../electron-dist/electron/backendProcessSupervisor.js";
import {
  commitBootstrapLibraryProjection,
  loadApprovedDesktopLibraryBinding,
} from "../electron-dist/electron/desktopSettings.js";
import { LibraryBootstrapBindingStore } from "../electron-dist/electron/libraryBootstrapBinding.js";
import { LibraryBootstrapBroker, LibraryBootstrapRequestSpool } from "../electron-dist/electron/libraryBootstrapBroker.js";
import { LibraryBootstrapCoordinator } from "../electron-dist/electron/libraryBootstrapCoordinator.js";
import {
  LibrarySelectionAuthority,
  NativeLibrarySelectionCoordinator,
} from "../electron-dist/electron/librarySelectionAuthority.js";

const NOW = 1_800_000_000_000;
const REQUEST_ID = `lb_${"a".repeat(64)}`;
const BINDING_SHA256 = "b".repeat(64);
const CHALLENGE = "c".repeat(64);
const SQLITE_POLICY_ID = "sqlite-wal-reset-2026-08-22-v1";
const SQLITE_RUNTIME = {
  policy_id: SQLITE_POLICY_ID,
  sqlite_version: "3.53.0",
  wal_reset_safe: true,
  journal_policy: "wal",
};
const SAFE_RUNTIME_CHECK = {
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

test("committed bootstrap projection preserves backend preferences and publishes the exact native library", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const selected = join(root, "restored-library");
    await mkdir(selected);
    const authority = new LibrarySelectionAuthority();
    const ticket = await authority.issueSelection(selected);
    const verified = await authority.redeemSelection(ticket.selectionTicket);
    try {
      await mkdir(stateDir, { recursive: true });
      await writeFile(join(stateDir, "backend-settings.json"), JSON.stringify({
        vision_profile_name: "local", query_profile_name: "local", process_image_width: 777,
        image_library_dir: "/stale-root", db_path: "/stale-db",
      }));
      await commitBootstrapLibraryProjection(process.cwd(), verified);
      const restored = JSON.parse(await readFile(join(stateDir, "backend-settings.json"), "utf8"));
      assert.deepEqual(restored, {
        vision_profile_name: "local", query_profile_name: "local", process_image_width: 777,
        image_library_dir: verified.canonicalRoot, db_path: verified.dbPath,
      });
      assert.equal((await loadApprovedDesktopLibraryBinding()).dbPath, verified.dbPath);
      assert.equal((await stat(join(stateDir, "backend-settings.json"))).mode & 0o777, 0o600);
      assert.equal((await readdir(stateDir)).some((name) => name.endsWith(".tmp")), false);
    } finally { await verified.release(); }
  });
});

test("partial bootstrap projection failure is repairable without losing the backend preferences", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const selected = join(root, "projection-repair-library");
    await mkdir(selected);
    const authority = new LibrarySelectionAuthority();
    const ticket = await authority.issueSelection(selected);
    const verified = await authority.redeemSelection(ticket.selectionTicket);
    try {
      const desktopPath = join(stateDir, "desktop-settings.json");
      await mkdir(desktopPath, { recursive: true });
      await writeFile(join(stateDir, "backend-settings.json"), JSON.stringify({ query_profile_name: "local" }));
      await assert.rejects(commitBootstrapLibraryProjection(process.cwd(), verified));
      assert.equal(await loadApprovedDesktopLibraryBinding(), null);
      const partial = JSON.parse(await readFile(join(stateDir, "backend-settings.json"), "utf8"));
      assert.equal(partial.db_path, verified.dbPath);
      await rm(desktopPath, { recursive: true });
      await commitBootstrapLibraryProjection(process.cwd(), verified);
      assert.equal((await loadApprovedDesktopLibraryBinding()).dbPath, verified.dbPath);
      assert.deepEqual(JSON.parse(await readFile(join(stateDir, "backend-settings.json"), "utf8")), partial);
    } finally { await verified.release(); }
  });
});

function healthValue(value) {
  return value === null ? "null" : String(value);
}

function signedHealthV2(runtime, overrides = {}) {
  const payload = {
    status: "ok",
    object: "health.check",
    service: "memolens-backend",
    api_version: "1",
    schema_version: "2",
    challenge_proof: "",
    sqlite_runtime: SQLITE_RUNTIME,
    runtime,
    ...overrides,
  };
  const message = [
    "memolens.health.v2",
    `challenge=${CHALLENGE}`,
    "object=health.check",
    "status=ok",
    "service=memolens-backend",
    "api_version=1",
    "schema_version=2",
    `policy_id=${payload.sqlite_runtime.policy_id}`,
    `sqlite_version=${payload.sqlite_runtime.sqlite_version}`,
    `wal_reset_safe=${payload.sqlite_runtime.wal_reset_safe ? "true" : "false"}`,
    `journal_policy=${payload.sqlite_runtime.journal_policy}`,
    `runtime_mode=${runtime.mode}`,
    `bootstrap_request_id=${healthValue(runtime.bootstrap_request_id)}`,
    `candidate_binding_sha256=${healthValue(runtime.candidate_binding_sha256)}`,
    `database_uuid=${healthValue(runtime.database_uuid)}`,
    `runtime_schema_version=${healthValue(runtime.schema_version)}`,
    `runtime_generation_id=${healthValue(runtime.runtime_generation_id)}`,
  ].join("\n");
  payload.challenge_proof = createHmac("sha256", getDesktopSessionToken())
    .update(message)
    .digest("hex");
  return payload;
}

function coreResponse(requestId = REQUEST_ID) {
  return {
    object: "memolens.library_bootstrap_result",
    schema_version: "1",
    request_id: requestId,
    status: "library_authority_committed",
    scan_started: false,
    scan_stage: "awaiting_scan_worker",
    editor_state: "awaiting_grounded_timeline",
    blueprint_authority: "unverified",
    timeline_edit_granted: false,
  };
}

test("health v2 proves exact candidate and active runtime identities", () => {
  const candidate = {
    mode: "bootstrap_candidate",
    bootstrap_request_id: REQUEST_ID,
    candidate_binding_sha256: BINDING_SHA256,
    database_uuid: null,
    schema_version: null,
    runtime_generation_id: null,
  };
  const parsedCandidate = verifyBackendHealthV2Payload(
    signedHealthV2(candidate),
    CHALLENGE,
    SQLITE_RUNTIME,
  );
  assert.deepEqual(parsedCandidate, candidate);
  assert.equal(matchesLibraryBootstrapRuntime(parsedCandidate, {
    mode: "bootstrap_candidate",
    requestId: REQUEST_ID,
    candidateBindingSha256: BINDING_SHA256,
  }), true);
  assert.equal(matchesLibraryBootstrapRuntime(parsedCandidate, {
    mode: "bootstrap_candidate",
    requestId: `lb_${"d".repeat(64)}`,
    candidateBindingSha256: BINDING_SHA256,
  }), false);

  const active = {
    mode: "active",
    bootstrap_request_id: REQUEST_ID,
    candidate_binding_sha256: BINDING_SHA256,
    database_uuid: "12345678-1234-4123-8123-123456789abc",
    schema_version: 20,
    runtime_generation_id: `runtime_generation_${"e".repeat(64)}`,
  };
  assert.deepEqual(
    verifyBackendHealthV2Payload(signedHealthV2(active), CHALLENGE),
    active,
  );
  assert.equal(
    verifyBackendHealthV2Payload(
      signedHealthV2(candidate, { unexpected: true }),
      CHALLENGE,
    ),
    null,
  );
  assert.equal(
    verifyBackendHealthV2Payload({
      ...signedHealthV2(candidate),
      challenge_proof: "00".repeat(32),
    }, CHALLENGE),
    null,
  );
});

test("main bootstrap client sends a path-free closed command and accepts only frozen response", async () => {
  const observations = [];
  const first = await commitLibraryBootstrapCandidate(
    "http://127.0.0.1:5519",
    REQUEST_ID,
    BINDING_SHA256,
    {
      mainAuthorityToken: "main-token",
      async fetchImpl(url, init) {
        observations.push({ url, init });
        return new Response(JSON.stringify(coreResponse()), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      },
    },
  );
  assert.equal(first.replayed, false);
  assert.deepEqual(first.response, coreResponse());
  assert.equal(observations[0].url, "http://127.0.0.1:5519/v1/main/library-bootstrap");
  assert.equal(observations[0].init.method, "POST");
  assert.deepEqual(observations[0].init.headers, {
    "Content-Type": "application/json",
    "X-MemoLens-Main-Authority": "main-token",
    "Idempotency-Key": REQUEST_ID,
  });
  assert.equal("Origin" in observations[0].init.headers, false);
  assert.deepEqual(JSON.parse(observations[0].init.body), {
    object: "memolens.main_library_bootstrap_command",
    schema_version: "1",
    request_id: REQUEST_ID,
    candidate_binding_sha256: BINDING_SHA256,
  });

  const replay = await commitLibraryBootstrapCandidate(
    "http://127.0.0.1:5519",
    REQUEST_ID,
    BINDING_SHA256,
    {
      mainAuthorityToken: "main-token",
      async fetchImpl() {
        return new Response(JSON.stringify(coreResponse()), {
          status: 201,
          headers: { "Idempotency-Replayed": "true" },
        });
      },
    },
  );
  assert.equal(replay.replayed, true);
  await assert.rejects(
    commitLibraryBootstrapCandidate(
      "http://127.0.0.1:5519",
      REQUEST_ID,
      BINDING_SHA256,
      {
        mainAuthorityToken: "main-token",
        async fetchImpl() {
          return new Response(JSON.stringify({ ...coreResponse(), path: "/private" }), {
            status: 201,
          });
        },
      },
    ),
    /invalid Library bootstrap Core response/,
  );
});

class FakeChildProcess extends EventEmitter {
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  exitCode = null;
  signalCode = null;
  killedSignals = [];

  kill(signal) {
    this.killedSignals.push(signal);
    this.signalCode = signal;
    this.emit("exit", null, signal);
    this.emit("close", null, signal);
    return true;
  }
}

function createCandidateSupervisor(probeBootstrapRuntime) {
  const spawns = [];
  let currentTime = 0;
  const supervisor = new BackendProcessSupervisor({
    backendUrl: "http://127.0.0.1:5519",
    probeHealth: async () => false,
    probeBootstrapRuntime,
    getSessionToken: () => "desktop-token",
    getMainAuthorityToken: () => "main-token",
    getRuntimeAuthorityEpoch: () => "runtime-epoch",
    rotateSessionToken() {},
    updateBackendTrust() {},
    getAppStateDir: () => "/memo-state",
    spawnProcess(command, args, options) {
      spawns.push({ command, args, options });
      return new FakeChildProcess();
    },
    execFileProcess(_command, _args, _options, callback) {
      queueMicrotask(() => callback(null, JSON.stringify(SAFE_RUNTIME_CHECK), ""));
      return new FakeChildProcess();
    },
    resolveExecutableIdentity(command) {
      return {
        realpath: command,
        device: "1",
        inode: "2",
        size: "3",
        mtimeNs: "4",
      };
    },
    readEnvFile: () => "MEMOLENS_BOOTSTRAP_REQUEST_ID=lb_bad\nMEMOLENS_BOOTSTRAP_BINDING_SHA256=bad",
    environment: () => ({
      MEMOLENS_BOOTSTRAP_REQUEST_ID: "lb_inherited",
      MEMOLENS_BOOTSTRAP_BINDING_SHA256: "inherited-digest",
    }),
    now: () => currentTime,
    sleep: async (duration) => { currentTime += duration; },
    logger: { log() {}, error() {} },
  });
  return { supervisor, spawns };
}

test("candidate supervisor passes only request ID and refuses to kill an external wrong runtime", async () => {
  let probeCalls = 0;
  const started = createCandidateSupervisor(async () => {
    probeCalls += 1;
    return probeCalls === 1 ? "unavailable" : "expected";
  });
  const status = await started.supervisor.ensureBootstrapCandidate(
    "/project",
    {
      pythonCommand: "ignored",
      autoStartBackend: true,
      libraryConfigured: false,
      defaultLibraryDir: null,
      defaultDbPath: null,
    },
    {
      mode: "bootstrap_candidate",
      requestId: REQUEST_ID,
      candidateBindingSha256: BINDING_SHA256,
    },
  );
  assert.equal(status.state, "started");
  assert.equal(started.spawns.length, 1);
  assert.equal(started.spawns[0].options.env.MEMOLENS_BOOTSTRAP_REQUEST_ID, REQUEST_ID);
  assert.equal(started.spawns[0].options.env.MEMOLENS_BOOTSTRAP_BINDING_SHA256, undefined);

  const wrong = createCandidateSupervisor(async () => "wrong");
  const wrongStatus = await wrong.supervisor.ensureBootstrapCandidate(
    "/project",
    {
      pythonCommand: "ignored",
      autoStartBackend: true,
      libraryConfigured: false,
      defaultLibraryDir: null,
      defaultDbPath: null,
    },
    {
      mode: "bootstrap_candidate",
      requestId: REQUEST_ID,
      candidateBindingSha256: BINDING_SHA256,
    },
  );
  assert.equal(wrongStatus.state, "unavailable");
  assert.match(wrongStatus.message, /different authenticated MemoLens runtime/);
  assert.equal(wrong.spawns.length, 0);
});

async function withIsolatedState(run) {
  const root = await mkdtemp(join(tmpdir(), "memolens-phase2-"));
  const stateDir = join(root, "app-state");
  const prior = process.env.MEMOLENS_APP_STATE_DIR;
  process.env.MEMOLENS_APP_STATE_DIR = stateDir;
  try {
    await run({ root, stateDir });
  } finally {
    if (prior === undefined) delete process.env.MEMOLENS_APP_STATE_DIR;
    else process.env.MEMOLENS_APP_STATE_DIR = prior;
    await rm(root, { recursive: true, force: true });
  }
}

async function writeRequest(stateDir) {
  const spoolDir = join(stateDir, "library-bootstrap-spool");
  await mkdir(spoolDir, { recursive: true, mode: 0o700 });
  await writeFile(join(spoolDir, `${REQUEST_ID}.request.json`), `${JSON.stringify({
    schema_version: "1",
    kind: "library_bootstrap_intent",
    request_id: REQUEST_ID,
    created_at_ms: NOW,
    expires_at_ms: NOW + 300_000,
  })}\n`, { mode: 0o600, flag: "wx" });
}

function createBrokerHarness({
  root,
  stateDir,
  nowRef,
  failStage = null,
  events = [],
}) {
  const libraryRoot = join(root, "chosen-library");
  const authority = new LibrarySelectionAuthority({ now: () => nowRef.value });
  let pickerCalls = 0;
  const picker = new NativeLibrarySelectionCoordinator({
    authority,
    async chooseFolder() {
      pickerCalls += 1;
      return libraryRoot;
    },
  });
  const realStore = new LibraryBootstrapBindingStore({
    stateDir,
    now: () => nowRef.value,
  });
  const failures = new Set(failStage === null ? [] : [failStage]);
  let coreCommitted = false;
  let finishFailures = failStage === "receipt" ? 1 : 0;
  const realSpool = new LibraryBootstrapRequestSpool({
    stateDir,
    now: () => nowRef.value,
  });
  const spool = {
    claimNext: () => realSpool.claimNext(),
    async finishClaim(claimed, state) {
      if (state === "library_authority_committed" && finishFailures > 0) {
        finishFailures -= 1;
        events.push("receipt_crash");
        throw new Error("fault after projection before receipt");
      }
      events.push(`receipt:${state}`);
      return realSpool.finishClaim(claimed, state);
    },
  };
  const coordinator = new LibraryBootstrapCoordinator({
    bindingStore: {
      load: (requestId) => realStore.load(requestId),
      async seal(input) {
        const result = await realStore.seal(input);
        events.push("sealed_binding_fsync");
        return result;
      },
    },
    authority,
    now: () => nowRef.value,
    async ensureCandidate() {
      events.push("candidate_health");
      if (failures.delete("candidate")) throw new Error("candidate fault");
      return {
        state: "connected",
        message: "candidate",
        url: "http://127.0.0.1:5519",
        startedByApp: true,
      };
    },
    async commitCore(envelope) {
      events.push("core_commit");
      const replayed = coreCommitted;
      coreCommitted = true;
      return { response: coreResponse(envelope.request_id), replayed };
    },
    async verifyActive() {
      events.push("active_health");
      if (failures.delete("active")) throw new Error("active fault");
    },
    async commitProjection(binding) {
      events.push("settings_projection");
      if (failures.delete("projection")) throw new Error("projection fault");
      await commitBootstrapLibraryProjection(process.cwd(), binding);
    },
  });
  const broker = new LibraryBootstrapBroker({
    spool,
    picker,
    authority,
    resume: (claimed) => coordinator.resume(claimed),
    commit: (binding, claimed) => coordinator.commit(binding, claimed),
  });
  return {
    broker,
    get pickerCalls() { return pickerCalls; },
  };
}

test("Phase2 transaction reaches settings and receipt only after Core and active proof", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    await mkdir(join(root, "chosen-library"));
    await writeRequest(stateDir);
    const events = [];
    const nowRef = { value: NOW + 1 };
    const harness = createBrokerHarness({ root, stateDir, nowRef, events });
    const result = await harness.broker.handleNext();
    assert.equal(result.status, "library_authority_committed");
    assert.equal(result.core_library_created, true);
    assert.deepEqual(events, [
      "sealed_binding_fsync",
      "candidate_health",
      "core_commit",
      "active_health",
      "settings_projection",
      "receipt:library_authority_committed",
    ]);
    assert.equal(harness.pickerCalls, 1);
    assert.ok(await loadApprovedDesktopLibraryBinding());
    const receipt = JSON.parse(await readFile(
      join(stateDir, "library-bootstrap-spool", `${REQUEST_ID}.receipt.json`),
      "utf8",
    ));
    assert.equal(receipt.state, "library_authority_committed");
    assert.equal(receipt.expires_at_ms, NOW + 300_000);
    assert.doesNotMatch(JSON.stringify(receipt), /path|db|device|inode/i);
  });
});

for (const failStage of ["candidate", "active", "projection", "receipt"]) {
  test(`sealed binding resumes across TTL after ${failStage} crash without another native prompt`, async () => {
    await withIsolatedState(async ({ root, stateDir }) => {
      await mkdir(join(root, "chosen-library"));
      await writeRequest(stateDir);
      const events = [];
      const nowRef = { value: NOW + 1 };
      const harness = createBrokerHarness({ root, stateDir, nowRef, failStage, events });
      await assert.rejects(harness.broker.handleNext(), /fault/);
      assert.equal(harness.pickerCalls, 1);
      assert.deepEqual(await readdir(join(stateDir, "library-bootstrap-spool")), [
        `${REQUEST_ID}.claim.json`,
      ]);
      const beforeRepair = await loadApprovedDesktopLibraryBinding();
      assert.equal(beforeRepair === null, failStage !== "receipt");
      if (failStage !== "receipt") {
        await assert.rejects(readFile(join(stateDir, "backend-settings.json")), /ENOENT/);
      }

      // A sealed native confirmation remains recoverable even after intent TTL.
      nowRef.value = NOW + 300_001;
      const result = await harness.broker.handleNext();
      assert.equal(result.status, "library_authority_committed");
      assert.equal(harness.pickerCalls, 1);
      assert.ok(await loadApprovedDesktopLibraryBinding());
      assert.equal((await stat(join(
        stateDir,
        "library-bootstrap-bindings",
        `${REQUEST_ID}.binding.json`,
      ), { bigint: true })).nlink, 1n);
      const receipt = JSON.parse(await readFile(
        join(stateDir, "library-bootstrap-spool", `${REQUEST_ID}.receipt.json`),
        "utf8",
      ));
      assert.equal(receipt.state, "library_authority_committed");
    });
  });
}

test("Phase2 coordinator has no renderer or indexing dependency", async () => {
  const source = await readFile(
    new URL("../electron/libraryBootstrapCoordinator.ts", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /BrowserWindow|ipcMain|indexingCoordinator|start-indexing/);
});
