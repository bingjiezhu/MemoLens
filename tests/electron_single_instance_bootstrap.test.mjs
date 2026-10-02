import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";

import {
  LIBRARY_BOOTSTRAP_WAKE_ARG,
  chooseDesktopStartupRoute,
  runDesktopStartupRoute,
} from "../electron-dist/electron/desktopStartupRouter.js";

test("pending or exact fixed wake selects broker-only startup", () => {
  assert.equal(
    chooseDesktopStartupRoute({ argv: ["MemoLens"], hasPendingBootstrap: true }),
    "library_bootstrap_broker",
  );
  assert.equal(
    chooseDesktopStartupRoute({
      argv: ["MemoLens", LIBRARY_BOOTSTRAP_WAKE_ARG],
      hasPendingBootstrap: false,
    }),
    "library_bootstrap_broker",
  );
  assert.throws(
    () => chooseDesktopStartupRoute({
      argv: ["MemoLens", `${LIBRARY_BOOTSTRAP_WAKE_ARG}=lb_${"a".repeat(64)}`],
      hasPendingBootstrap: true,
    }),
    /must not carry a request payload/,
  );
});

test("broker-only route performs zero normal-backend business IPC and renderer calls", async () => {
  let normalBackendCalls = 0;
  let windows = 0;
  let brokerRuns = 0;
  let businessIpcRegistrations = 0;
  const sequence = [];
  const route = await runDesktopStartupRoute({
    argv: ["MemoLens"],
    hasPendingBootstrap: true,
    async runBootstrapBroker() { brokerRuns += 1; sequence.push("broker"); },
    registerBusinessIpc() { businessIpcRegistrations += 1; sequence.push("ipc"); },
    async ensureBackend() { normalBackendCalls += 1; sequence.push("backend"); },
    createWindow() { windows += 1; sequence.push("window"); },
  });
  assert.equal(route, "library_bootstrap_broker");
  assert.equal(brokerRuns, 1);
  assert.equal(businessIpcRegistrations, 0);
  assert.equal(normalBackendCalls, 0);
  assert.equal(windows, 0);
  assert.deepEqual(sequence, ["broker"]);

  const normal = await runDesktopStartupRoute({
    argv: ["MemoLens"],
    hasPendingBootstrap: false,
    async runBootstrapBroker() { brokerRuns += 1; sequence.push("broker"); },
    registerBusinessIpc() { businessIpcRegistrations += 1; sequence.push("ipc"); },
    async ensureBackend() { normalBackendCalls += 1; sequence.push("backend"); },
    createWindow() { windows += 1; sequence.push("window"); },
  });
  assert.equal(normal, "normal");
  assert.equal(businessIpcRegistrations, 1);
  assert.equal(normalBackendCalls, 1);
  assert.equal(windows, 1);
  assert.deepEqual(sequence, ["broker", "ipc", "backend", "window"]);
});

test("only a committed broker result promotes cold startup to the normal runtime and window", async () => {
  for (const status of ["native_cancelled", "expired", "library_authority_staged", "library_authority_committed"]) {
    const calls = [];
    const route = await runDesktopStartupRoute({
      argv: ["MemoLens"],
      hasPendingBootstrap: true,
      async runBootstrapBroker() { calls.push("broker"); return { status }; },
      registerBusinessIpc() { calls.push("ipc"); },
      async ensureBackend() { calls.push("backend"); },
      createWindow() { calls.push("window"); },
    });
    assert.equal(route, status === "library_authority_committed" ? "normal" : "library_bootstrap_broker");
    assert.deepEqual(calls, status === "library_authority_committed"
      ? ["broker", "ipc", "backend", "window"] : ["broker"]);
  }
  const calls = [];
  await assert.rejects(runDesktopStartupRoute({
    argv: ["MemoLens"], hasPendingBootstrap: true,
    async runBootstrapBroker() { throw new Error("active proof failed"); },
    registerBusinessIpc() { calls.push("ipc"); },
    async ensureBackend() { calls.push("backend"); },
    createWindow() { calls.push("window"); },
  }), /active proof failed/);
  assert.deepEqual(calls, []);
});

test("warm committed bootstrap restores one window while cancellation and cold routing create none", async () => {
  const source = await readFile(new URL("../electron/main.ts", import.meta.url), "utf8");
  const functionSource = source.slice(source.indexOf("async function runPendingLibraryBootstrap"), source.indexOf("function createWindow"));
  const javascript = functionSource.replace(": Promise<boolean>", "");
  for (const status of ["library_authority_committed", "native_cancelled", "expired", "library_authority_staged", "failed"]) {
    let created = 0;
    let authenticated = 0;
    let focused = 0;
    let restored = 0;
    const windows = [];
    const context = {
      console: { log() {} },
      normalStartupCompleted: true,
      libraryBootstrapRequestSpool: { async hasPendingRequest() { return true; } },
      libraryBootstrapBroker: { async handleNext() { if (status === "failed") throw new Error("bootstrap failed"); return { status, request_id: "test" }; } },
      configureDesktopSessionAuthentication() { authenticated += 1; },
      BrowserWindow: { getAllWindows() { return windows; } },
      createWindow() {
        created += 1;
        const window = { isMinimized() { return true; }, restore() { restored += 1; }, show() {}, focus() { focused += 1; } };
        windows.push(window);
        return window;
      },
    };
    const runPending = runInNewContext(`${javascript}\nrunPendingLibraryBootstrap`, context);
    if (status === "failed") await assert.rejects(runPending(), /bootstrap failed/);
    else await Promise.all([runPending(), runPending()]);
    assert.equal(created, status === "library_authority_committed" ? 1 : 0);
    assert.equal(authenticated, status === "library_authority_committed" ? 2 : 0);
    assert.equal(focused, status === "library_authority_committed" ? 2 : 0);
    assert.equal(restored, status === "library_authority_committed" ? 2 : 0);
    context.normalStartupCompleted = false;
    windows.length = 0;
    if (status !== "failed") await runPending();
    assert.equal(created, status === "library_authority_committed" ? 1 : 0);
  }
});

test("main acquires one instance and routes bootstrap before backend or window startup", async () => {
  const source = await readFile(new URL("../electron/main.ts", import.meta.url), "utf8");
  const lockIndex = source.indexOf("app.requestSingleInstanceLock()");
  const secondInstanceIndex = source.indexOf('app.on("second-instance"');
  const readyIndex = source.indexOf("app.whenReady().then");
  assert.ok(lockIndex >= 0);
  assert.ok(secondInstanceIndex > lockIndex);
  assert.ok(readyIndex > secondInstanceIndex);
  const routeIndex = source.indexOf("runDesktopStartupRoute({");
  assert.ok(routeIndex >= 0);
  assert.doesNotMatch(
    source.slice(readyIndex, routeIndex),
    /registerProductionIpcHandlers\(\);|await runBackendBootstrap\(\);|createWindow\(\);/,
  );
  assert.match(
    source.slice(routeIndex),
    /runDesktopStartupRoute\(\{[\s\S]*registerBusinessIpc: registerProductionIpcHandlers,[\s\S]*ensureBackend: runBackendBootstrap,[\s\S]*createWindow,[\s\S]*\}\)/,
  );
  assert.match(
    source.slice(routeIndex),
    /if \(route === "library_bootstrap_broker"\) \{[\s\S]*app\.quit\(\);[\s\S]*return;/,
  );
  assert.match(
    source,
    /function registerProductionIpcHandlers\(\): void \{[\s\S]*productionIpcHandlers\.handle\(/,
  );
  assert.doesNotMatch(
    source.slice(0, source.indexOf("function registerProductionIpcHandlers")),
    /productionIpcHandlers\.handle\(/,
  );
  assert.match(
    source,
    /app\.on\("before-quit", \(event\) => \{[\s\S]*if \(!backendLifecycleActivated\) \{[\s\S]*return;[\s\S]*\}/,
  );
});
