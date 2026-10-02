import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, realpath, readdir, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { commitLibraryBootstrapCandidate, getDesktopSessionToken, matchesLibraryBootstrapRuntime, probeBackendHealthV2 } from "../electron-dist/electron/backendManager.js";
import { commitBootstrapLibraryProjection } from "../electron-dist/electron/desktopSettings.js";
import { runDesktopStartupRoute } from "../electron-dist/electron/desktopStartupRouter.js";
import { LibraryBootstrapBindingStore } from "../electron-dist/electron/libraryBootstrapBinding.js";
import { LibraryBootstrapBroker, LibraryBootstrapRequestSpool } from "../electron-dist/electron/libraryBootstrapBroker.js";
import { LibraryBootstrapCoordinator } from "../electron-dist/electron/libraryBootstrapCoordinator.js";
import { LibrarySelectionAuthority, NativeLibrarySelectionCoordinator } from "../electron-dist/electron/librarySelectionAuthority.js";

test("fresh bootstrap survives shutdown and normal restart with the same database and plugin scan", { timeout: 90_000 }, async (context) => {
  const project = resolve(import.meta.dirname, "..");
  const runtime = spawnSync("bash", [join(project, "scripts/run_python.sh"), "-c", "import sys; print(sys.executable)"], {
    cwd: project, env: process.env, encoding: "utf8", timeout: 20_000,
  });
  assert.ifError(runtime.error);
  assert.equal(runtime.status, 0, runtime.stderr || runtime.stdout);
  const python = runtime.stdout.trim();
  assert.ok(python, "Python runtime resolver returned no executable");
  const fixture = await realpath(await mkdtemp(join(tmpdir(), "memolens-bootstrap-restart-")));
  const stateDir = join(fixture, "state");
  const library = join(fixture, "empty-library");
  await mkdir(library);
  const oldState = process.env.MEMOLENS_APP_STATE_DIR;
  process.env.MEMOLENS_APP_STATE_DIR = stateDir;
  const listener = createServer();
  listener.listen(0, "127.0.0.1");
  await once(listener, "listening");
  const port = listener.address().port;
  await new Promise((done) => listener.close(done));
  const url = `http://127.0.0.1:${port}`;
  const mainToken = "bootstrap-restart-test-main-token";
  const env = {
    ...process.env, MEMOLENS_APP_STATE_DIR: stateDir,
    MEMOLENS_BACKEND_PORT: String(port), MEMOLENS_BACKEND_DEBUG: "0",
    MEMOLENS_DESKTOP_SESSION_TOKEN: getDesktopSessionToken(),
    MEMOLENS_MAIN_AUTHORITY_TOKEN: mainToken,
    MEMOLENS_RUNTIME_AUTHORITY_EPOCH: "bootstrap-restart-test-epoch",
    MEMOLENS_NETWORK_PROFILE: "offline", EMBEDDING_BACKEND: "semantic_hash",
    MEMOLENS_PLUGIN_TRUST_LOCAL_API: "0",
  };
  for (const key of ["MEMOLENS_DB_PATH", "MEMOLENS_LIBRARY_DIR", "MEMOLENS_BOOTSTRAP_REQUEST_ID", "SQLITE_DB_PATH", "IMAGE_LIBRARY_DIR"]) delete env[key];
  const cli = join(project, ".agents/plugins/plugins/memolens/scripts/memolens_cli.py");
  let child = null;
  let output = "";
  function plugin(...args) {
    const result = spawnSync(python, [cli, ...args], { cwd: project, env, encoding: "utf8", timeout: 20_000 });
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stderr || result.stdout);
    return JSON.parse(result.stdout);
  }
  async function start(requestId = null) {
    output = "";
    child = spawn(python, ["-I", "-u", "backend/app.py"], {
      cwd: project, env: { ...env, ...(requestId === null ? {} : { MEMOLENS_BOOTSTRAP_REQUEST_ID: requestId }) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.on("data", (data) => { output = (output + data).slice(-12_000); });
    child.stderr.on("data", (data) => { output = (output + data).slice(-12_000); });
    await once(child, "spawn");
  }
  async function stop() {
    if (child === null || child.pid === undefined || child.exitCode !== null || child.signalCode !== null) return;
    const current = child;
    const exited = once(current, "exit");
    current.kill("SIGTERM");
    const watchdog = setTimeout(() => current.kill("SIGKILL"), 10_000);
    try {
      const [code, signal] = await exited;
      assert.equal(signal, null, output);
      assert.equal(code, 0, output);
    } finally { clearTimeout(watchdog); child = null; }
  }
  async function health() {
    for (let attempt = 0; attempt < 200; attempt += 1) {
      assert.equal(child?.exitCode, null, output);
      const runtime = await probeBackendHealthV2(url);
      if (runtime !== null) return runtime;
      await new Promise((done) => setTimeout(done, 100));
    }
    assert.fail(`Backend did not become healthy: ${output}`);
  }
  try {
    const request = plugin("library-bootstrap-start", "--request-idempotency-key", "fresh-restart-regression");
    const authority = new LibrarySelectionAuthority();
    const spool = new LibraryBootstrapRequestSpool({ stateDir });
    let activeIdentity;
    const coordinator = new LibraryBootstrapCoordinator({
      bindingStore: new LibraryBootstrapBindingStore({ stateDir }), authority,
      async ensureCandidate(envelope) {
        await start(envelope.request_id);
        assert.ok(matchesLibraryBootstrapRuntime(await health(), {
          mode: "bootstrap_candidate", requestId: envelope.request_id,
          candidateBindingSha256: envelope.candidate_binding_sha256,
        }));
        return { state: "started", message: "test candidate", url, startedByApp: true };
      },
      commitCore: (envelope) => commitLibraryBootstrapCandidate(url, envelope.request_id, envelope.candidate_binding_sha256, { mainAuthorityToken: mainToken }),
      async verifyActive(envelope) {
        activeIdentity = await health();
        assert.ok(matchesLibraryBootstrapRuntime(activeIdentity, {
          mode: "active", requestId: envelope.request_id,
          candidateBindingSha256: envelope.candidate_binding_sha256,
        }));
      },
      commitProjection: (binding) => commitBootstrapLibraryProjection(project, binding),
    });
    const broker = new LibraryBootstrapBroker({
      spool, authority,
      picker: new NativeLibrarySelectionCoordinator({ authority, chooseFolder: async () => library }),
      resume: (claimed) => coordinator.resume(claimed),
      commit: (binding, claimed) => coordinator.commit(binding, claimed),
    });
    const startup = [];
    assert.equal(await runDesktopStartupRoute({
      argv: ["MemoLens"], hasPendingBootstrap: await spool.hasPendingRequest(),
      runBootstrapBroker: () => broker.handleNext(),
      registerBusinessIpc() { startup.push("ipc"); },
      async ensureBackend() { assert.equal((await health()).database_uuid, activeIdentity.database_uuid); startup.push("backend"); },
      createWindow() { startup.push("window"); },
    }), "normal");
    assert.deepEqual(startup, ["ipc", "backend", "window"]);
    assert.equal(plugin("library-bootstrap-status", request.request_id).status, "library_authority_committed");
    const envelope = await new LibraryBootstrapBindingStore({ stateDir }).load(request.request_id);
    const replay = await commitLibraryBootstrapCandidate(url, envelope.request_id, envelope.candidate_binding_sha256, { mainAuthorityToken: mainToken });
    assert.equal(replay.replayed, true);
    assert.equal((await health()).database_uuid, activeIdentity.database_uuid);
    const saved = JSON.parse(await readFile(join(stateDir, "backend-settings.json"), "utf8"));
    assert.equal(saved.image_library_dir, library);
    await stop();
    await start(); // No bootstrap request ID and no explicit database/library override.
    const restarted = await health();
    assert.equal(restarted.mode, "active");
    assert.equal(restarted.bootstrap_request_id, null);
    assert.equal(restarted.database_uuid, activeIdentity.database_uuid);
    assert.notEqual(restarted.runtime_generation_id, activeIdentity.runtime_generation_id);
    let status;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      status = plugin("status");
      if (status.database.library_scan?.status === "succeeded") break;
      await new Promise((done) => setTimeout(done, 100));
    }
    assert.equal(status.status, "ok");
    assert.equal(status.mode, "safe_default_read_only");
    assert.equal(status.database.library_scan.status, "succeeded", JSON.stringify(status.database.library_scan));
    assert.equal(status.database.library_scan.no_supported_media, true);
    await stop();
    await start();
    assert.equal((await health()).database_uuid, activeIdentity.database_uuid);
    assert.equal(plugin("status").database.library_scan.status, "succeeded");
    const databases = (await readdir(join(stateDir, "storage"))).filter((name) => name.endsWith(".db"));
    assert.equal(databases.length, 1);
    context.diagnostic(JSON.stringify({ database_uuid: restarted.database_uuid, normal_restart: true, scan: status.database.library_scan.status, plugin: status.mode, database_count: databases.length }));
  } finally {
    await stop();
    if (oldState === undefined) delete process.env.MEMOLENS_APP_STATE_DIR;
    else process.env.MEMOLENS_APP_STATE_DIR = oldState;
    await rm(fixture, { recursive: true, force: true });
  }
});
