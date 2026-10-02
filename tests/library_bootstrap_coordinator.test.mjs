import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { link, mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

// Keep Phase2 binding/runtime fault contracts inside the package's existing
// focused Electron gate without widening package.json from this slice.
import "./library_bootstrap_binding.test.mjs";
import "./library_bootstrap_phase2.test.mjs";
import "./library_bootstrap_restart.test.mjs";

import {
  commitDesktopLibrarySelection,
  loadApprovedDesktopLibraryBinding,
} from "../electron-dist/electron/desktopSettings.js";
import {
  LibrarySelectionAuthority,
  NativeLibrarySelectionCoordinator,
} from "../electron-dist/electron/librarySelectionAuthority.js";
import {
  LIBRARY_BOOTSTRAP_MAX_BYTES,
  LIBRARY_BOOTSTRAP_MAX_ENTRIES,
  LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES,
  LibraryBootstrapBroker,
  LibraryBootstrapRequestSpool,
} from "../electron-dist/electron/libraryBootstrapBroker.js";

const NOW = 1_800_000_000_000;

async function withIsolatedState(run) {
  const root = await mkdtemp(join(tmpdir(), "memolens-library-bootstrap-"));
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

async function writeRequest(stateDir, requestId, overrides = {}) {
  const spoolDir = join(stateDir, "library-bootstrap-spool");
  await mkdir(spoolDir, { recursive: true, mode: 0o700 });
  const payload = {
    schema_version: "1",
    kind: "library_bootstrap_intent",
    request_id: requestId,
    created_at_ms: NOW,
    expires_at_ms: NOW + 300_000,
    ...overrides,
  };
  const requestPath = join(spoolDir, `${requestId}.request.json`);
  await writeFile(requestPath, `${JSON.stringify(payload)}\n`, { mode: 0o600, flag: "wx" });
  return requestPath;
}

async function writeReceipt(stateDir, requestId, state = "library_authority_staged") {
  const spoolDir = join(stateDir, "library-bootstrap-spool");
  await mkdir(spoolDir, { recursive: true, mode: 0o700 });
  const receiptPath = join(spoolDir, `${requestId}.receipt.json`);
  await writeFile(receiptPath, `${JSON.stringify({
    schema_version: "1",
    kind: "library_bootstrap_receipt",
    request_id: requestId,
    state,
    completed_at_ms: NOW,
    expires_at_ms: NOW + 300_000,
  })}\n`, { mode: 0o600, flag: "wx" });
  return receiptPath;
}

function runPythonBootstrap(stateDir, expression) {
  const result = spawnSync(
    "bash",
    ["./scripts/run_python.sh", "-c", expression],
    {
      cwd: process.cwd(),
      encoding: "utf8",
      env: {
        ...process.env,
        MEMOLENS_APP_STATE_DIR: stateDir,
        PYTHONWARNINGS: "error",
      },
    },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

test("Python writer and Electron broker share the exact request and receipt bytes", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const scripts = ".agents/plugins/plugins/memolens/scripts";
    const started = runPythonBootstrap(
      stateDir,
      `import json,sys;sys.path.insert(0,${JSON.stringify(scripts)});from memolens_library_bootstrap import start_library_bootstrap;print(json.dumps(start_library_bootstrap("cross-language-contract",now_ms=${NOW})))`,
    );
    assert.equal(started.status, "queued_open_memolens");
    assert.equal(started.expires_at_ms, NOW + 300_000);

    const spool = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    const claimed = await spool.claimNext();
    assert.ok(claimed);
    assert.equal(claimed.request.request_id, started.request_id);
    assert.equal(claimed.request.expires_at_ms, started.expires_at_ms);
    await spool.finishClaim(claimed, "native_cancelled");

    const status = runPythonBootstrap(
      stateDir,
      `import json,sys;sys.path.insert(0,${JSON.stringify(scripts)});from memolens_library_bootstrap import library_bootstrap_status;print(json.dumps(library_bootstrap_status(${JSON.stringify(started.request_id)},now_ms=${NOW + 1})))`,
    );
    assert.equal(status.status, "native_cancelled");
    assert.equal(status.request_id, started.request_id);
    assert.equal(status.expires_at_ms, started.expires_at_ms);
    assert.equal(status.backend_contacted_by_plugin, false);
  });
});

test("broker stages desktop Library authority and creates no database or project", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const requestId = `lb_${"a".repeat(64)}`;
    const libraryDir = join(root, "chosen-library");
    await Promise.all([mkdir(libraryDir), writeRequest(stateDir, requestId)]);
    const authority = new LibrarySelectionAuthority();
    const picker = new NativeLibrarySelectionCoordinator({
      authority,
      async chooseFolder() { return libraryDir; },
    });
    let commitCount = 0;
    const broker = new LibraryBootstrapBroker({
      spool: new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }),
      picker,
      authority,
      async commit(binding) {
        commitCount += 1;
        return commitDesktopLibrarySelection(process.cwd(), binding);
      },
    });

    const result = await broker.handleNext();
    assert.deepEqual(result, {
      object: "memolens.library_bootstrap_broker_result",
      schema_version: "1",
      request_id: requestId,
      status: "library_authority_staged",
      core_library_created: false,
      database_created: false,
      project_created: false,
      scan_started: false,
      editor_ready: false,
      timeline_edit_granted: false,
    });
    assert.equal(commitCount, 1);
    const persisted = await loadApprovedDesktopLibraryBinding();
    assert.ok(persisted);
    assert.equal(persisted.canonicalRoot, await realpath(libraryDir));
    assert.equal(await stat(join(stateDir, "desktop-settings.json")).then(() => true), true);
    await assert.rejects(stat(persisted.dbPath), { code: "ENOENT" });
    assert.deepEqual(await readdir(libraryDir), []);
    const spoolEntries = await readdir(join(stateDir, "library-bootstrap-spool"));
    assert.deepEqual(spoolEntries, [`${requestId}.receipt.json`]);
    const receipt = JSON.parse(await readFile(
      join(stateDir, "library-bootstrap-spool", spoolEntries[0]),
      "utf8",
    ));
    assert.deepEqual(Object.keys(receipt).sort(), [
      "completed_at_ms", "expires_at_ms", "kind", "request_id", "schema_version", "state",
    ]);
    assert.equal(receipt.state, "library_authority_staged");
    assert.equal(receipt.expires_at_ms, NOW + 300_000);
    assert.doesNotMatch(JSON.stringify(receipt), /path|db|device|inode|project/i);
  });
});

test("native cancellation consumes intent with zero settings database or project writes", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"b".repeat(64)}`;
    await writeRequest(stateDir, requestId);
    const authority = new LibrarySelectionAuthority();
    const picker = new NativeLibrarySelectionCoordinator({
      authority,
      async chooseFolder() { return null; },
    });
    let commitCount = 0;
    const broker = new LibraryBootstrapBroker({
      spool: new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }),
      picker,
      authority,
      async commit() { commitCount += 1; throw new Error("commit must not run"); },
    });

    const result = await broker.handleNext();
    assert.equal(result.status, "native_cancelled");
    assert.equal(result.request_id, requestId);
    assert.equal(commitCount, 0);
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
    assert.deepEqual(await readdir(stateDir), ["library-bootstrap-spool"]);
    const receipt = JSON.parse(await readFile(
      join(
        stateDir,
        "library-bootstrap-spool",
        `${requestId}.receipt.json`,
      ),
      "utf8",
    ));
    assert.equal(receipt.state, "native_cancelled");
  });
});

test("expired request becomes a path-free receipt without opening a native chooser", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"e".repeat(64)}`;
    await writeRequest(stateDir, requestId, {
      created_at_ms: NOW - 300_001,
      expires_at_ms: NOW - 1,
    });
    const authority = new LibrarySelectionAuthority();
    let pickerCalls = 0;
    const picker = new NativeLibrarySelectionCoordinator({
      authority,
      async chooseFolder() { pickerCalls += 1; return null; },
    });
    const spool = new LibraryBootstrapRequestSpool({ now: () => NOW });
    const broker = new LibraryBootstrapBroker({
      spool,
      picker,
      authority,
      async commit() { throw new Error("expired request reached commit"); },
    });
    assert.equal(await broker.handleNext(), null);
    assert.equal(pickerCalls, 0);
    assert.equal(await spool.hasPendingRequest(), false);
    const receipt = JSON.parse(await readFile(
      join(stateDir, "library-bootstrap-spool", `${requestId}.receipt.json`),
      "utf8",
    ));
    assert.equal(receipt.state, "expired");
    assert.doesNotMatch(JSON.stringify(receipt), /path|db|device|inode|project/i);
  });
});

test("closed request parser rejects decoded-key duplicates including escaped aliases", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"d".repeat(64)}`;
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    await mkdir(spoolDir, { recursive: true, mode: 0o700 });
    const raw = `{"schema_version":"1","schema\\u005fversion":"1","kind":"library_bootstrap_intent","request_id":"${requestId}","created_at_ms":${NOW},"expires_at_ms":${NOW + 300_000}}\n`;
    assert.match(raw, /schema\\u005fversion/);
    await writeFile(
      join(spoolDir, `${requestId}.request.json`),
      raw,
      { mode: 0o600, flag: "wx" },
    );
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /duplicate decoded bootstrap member/,
    );
  });
});

test("terminal receipt removes only safe self-generated crash temporaries", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"f".repeat(64)}`;
    await writeRequest(stateDir, requestId);
    await writeReceipt(stateDir, requestId);
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    const temporaryName = `.${requestId}.receipt.json.00000000-0000-4000-8000-000000000000.tmp`;
    const pythonTemporaryName = `.${requestId}.${"0".repeat(32)}.tmp`;
    await Promise.all([
      link(join(spoolDir, `${requestId}.receipt.json`), join(spoolDir, temporaryName)),
      writeFile(
        join(spoolDir, pythonTemporaryName),
        "bounded interrupted request bytes",
        { mode: 0o600, flag: "wx" },
      ),
    ]);
    const spool = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    assert.equal(await spool.hasPendingRequest(), false);
    assert.deepEqual(await readdir(spoolDir), [`${requestId}.receipt.json`]);
  });

  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"7".repeat(64)}`;
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    await mkdir(spoolDir, { recursive: true, mode: 0o700 });
    await writeFile(
      join(spoolDir, `.${requestId}.receipt.json.00000000-0000-4000-8000-000000000000.tmp`),
      "unsafe mode",
      { mode: 0o644, flag: "wx" },
    );
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /unsafe bootstrap temporary artifact/,
    );
  });

  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"8".repeat(64)}`;
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    await mkdir(spoolDir, { recursive: true, mode: 0o700 });
    await writeFile(
      join(spoolDir, `.${requestId}.${"1".repeat(32)}.tmp`),
      "unsafe python-writer mode",
      { mode: 0o644, flag: "wx" },
    );
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /unsafe bootstrap temporary artifact/,
    );
  });

  await withIsolatedState(async ({ stateDir }) => {
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    await mkdir(spoolDir, { recursive: true, mode: 0o700 });
    await writeFile(join(spoolDir, ".unknown.tmp"), "unknown", { mode: 0o600 });
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /unknown bootstrap spool entry/,
    );
  });
});

test("published Python request hardlink temp is cleaned without losing the request", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const requestId = `lb_${"6".repeat(64)}`;
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    await mkdir(spoolDir, { recursive: true, mode: 0o700 });
    const temporaryName = `.${requestId}.${"3".repeat(32)}.tmp`;
    const temporaryPath = join(spoolDir, temporaryName);
    const requestPath = join(spoolDir, `${requestId}.request.json`);
    await writeFile(temporaryPath, `${JSON.stringify({
      schema_version: "1",
      kind: "library_bootstrap_intent",
      request_id: requestId,
      created_at_ms: NOW,
      expires_at_ms: NOW + 300_000,
    })}\n`, { mode: 0o600, flag: "wx" });
    await link(temporaryPath, requestPath);
    const [temporaryBefore, requestBefore] = await Promise.all([
      stat(temporaryPath),
      stat(requestPath),
    ]);
    assert.equal(temporaryBefore.ino, requestBefore.ino);
    assert.equal(temporaryBefore.nlink, 2);

    const spool = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    assert.equal(await spool.hasPendingRequest(), true);
    assert.deepEqual(await readdir(spoolDir), [`${requestId}.request.json`]);
    assert.equal((await stat(requestPath)).nlink, 1);
  });
});

test("owned crash temporary is cleaned before the final-entry census", async () => {
  await withIsolatedState(async ({ stateDir }) => {
    const requestIds = Array.from(
      { length: LIBRARY_BOOTSTRAP_MAX_ENTRIES },
      (_, index) => `lb_${index.toString(16).padStart(64, "0")}`,
    );
    await Promise.all(requestIds.map((requestId) => writeReceipt(stateDir, requestId)));
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    const temporaryName = `.${requestIds[0]}.${"2".repeat(32)}.tmp`;
    await writeFile(join(spoolDir, temporaryName), "owned crash temp", {
      mode: 0o600,
      flag: "wx",
    });

    const spool = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    assert.equal(await spool.hasPendingRequest(), false);
    const names = await readdir(spoolDir);
    assert.equal(names.length, LIBRARY_BOOTSTRAP_MAX_ENTRIES);
    assert.equal(names.includes(temporaryName), false);
  });
});

test("Phase-0 residual: commit-to-receipt crash re-prompts and can recommit", async () => {
  const requestId = `lb_${"9".repeat(64)}`;
  const claimed = {
    request: {
      schema_version: "1",
      kind: "library_bootstrap_intent",
      request_id: requestId,
      created_at_ms: NOW,
      expires_at_ms: NOW + 300_000,
    },
    claimName: `${requestId}.claim.json`,
  };
  let pickerCalls = 0;
  let commitCalls = 0;
  let releaseCalls = 0;
  const makeBroker = () => new LibraryBootstrapBroker({
    spool: {
      async claimNext() { return claimed; },
      async finishClaim() { throw new Error("simulated crash before receipt publication"); },
    },
    picker: {
      async pick() {
        pickerCalls += 1;
        return { selectionTicket: "ticket" };
      },
    },
    authority: {
      async redeemSelection() {
        return {
          async verifyCurrentIdentity() {},
          async release() { releaseCalls += 1; },
        };
      },
    },
    async commit() { commitCalls += 1; },
  });

  await assert.rejects(makeBroker().handleNext(), /crash before receipt publication/);
  await assert.rejects(makeBroker().handleNext(), /crash before receipt publication/);
  assert.equal(pickerCalls, 2);
  assert.equal(commitCalls, 2);
  assert.equal(releaseCalls, 2);
});

test("spool rejects symlink malformed oversized conflict and bounded-census inputs", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const requestId = `lb_${"c".repeat(64)}`;
    const spoolDir = join(stateDir, "library-bootstrap-spool");
    const outside = join(root, "outside");
    await Promise.all([mkdir(stateDir, { recursive: true }), mkdir(outside)]);
    await symlink(outside, spoolDir);
    const symlinkSpool = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    await assert.rejects(symlinkSpool.hasPendingRequest(), /unsafe bootstrap spool directory/);
    assert.deepEqual(await readdir(outside), []);

    await rm(spoolDir);
    await writeRequest(stateDir, requestId, { path_hint: "/forged" });
    const malformedSpool = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    await assert.rejects(malformedSpool.hasPendingRequest(), /closed bootstrap request schema/);

    await rm(spoolDir, { recursive: true, force: true });
    await mkdir(spoolDir, { mode: 0o700 });
    await writeFile(
      join(spoolDir, `${requestId}.request.json`),
      "x".repeat(LIBRARY_BOOTSTRAP_MAX_BYTES + 1),
      { mode: 0o600 },
    );
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /exceeds the bounded size/,
    );

    await rm(spoolDir, { recursive: true, force: true });
    await mkdir(spoolDir, { mode: 0o700 });
    await Promise.all(Array.from(
      { length: LIBRARY_BOOTSTRAP_MAX_ENTRIES + 1 },
      (_, index) => {
        const boundedRequestId = `lb_${index.toString(16).padStart(64, "0")}`;
        return writeFile(
          join(spoolDir, `${boundedRequestId}.receipt.json`),
          `${JSON.stringify({
            schema_version: "1",
            kind: "library_bootstrap_receipt",
            request_id: boundedRequestId,
            state: "library_authority_staged",
            completed_at_ms: NOW,
            expires_at_ms: NOW + 300_000,
          })}\n`,
          { mode: 0o600, flag: "wx" },
        );
      },
    ));
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /bounded census/,
    );

    await rm(spoolDir, { recursive: true, force: true });
    await mkdir(spoolDir, { mode: 0o700 });
    await Promise.all(Array.from(
      { length: LIBRARY_BOOTSTRAP_MAX_RAW_ENTRIES + 1 },
      (_, index) => writeFile(
        join(spoolDir, `raw-${index}`),
        "x",
        { mode: 0o600, flag: "wx" },
      ),
    ));
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /bounded raw census/,
    );

    await rm(spoolDir, { recursive: true, force: true });
    await writeRequest(stateDir, requestId);
    const receiptPath = join(spoolDir, `${requestId}.receipt.json`);
    await writeFile(
      receiptPath,
      JSON.stringify({
        schema_version: "1",
        kind: "library_bootstrap_receipt",
        request_id: requestId,
        state: "library_authority_staged",
        completed_at_ms: NOW,
        expires_at_ms: NOW + 300_001,
      }),
      { mode: 0o600 },
    );
    await assert.rejects(
      new LibraryBootstrapRequestSpool({ now: () => NOW + 1 }).hasPendingRequest(),
      /conflicting bootstrap expiry data/,
    );
    await rm(receiptPath);
    await writeFile(
      receiptPath,
      JSON.stringify({
        schema_version: "1",
        kind: "library_bootstrap_receipt",
        request_id: requestId,
        state: "library_authority_staged",
        completed_at_ms: NOW,
        expires_at_ms: NOW + 300_000,
      }),
      { mode: 0o600 },
    );
    const reconciled = new LibraryBootstrapRequestSpool({ now: () => NOW + 1 });
    assert.equal(await reconciled.hasPendingRequest(), false);
    assert.deepEqual(
      await readdir(spoolDir),
      [`${requestId}.receipt.json`],
    );
  });
});
