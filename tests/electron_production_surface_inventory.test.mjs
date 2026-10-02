import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";

import * as ts from "typescript/unstable/ast";
import { API as TypeScriptAPI } from "typescript/unstable/sync";

import {
  ELECTRON_INVOKE_CHANNELS,
  ELECTRON_OUTBOUND_EVENT_CHANNELS,
  ELECTRON_PRODUCTION_ACTION_IDS,
  ELECTRON_PRODUCTION_SURFACES,
  ProductionIpcHandlerRegistry,
  sendProductionIpcEvent,
} from "../electron-dist/electron/productionSurfaceRegistry.js";
import {
  assertTrustedIpcSender,
} from "../electron-dist/electron/ipcAuthority.js";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const ELECTRON_ROOT = join(ROOT, "electron");
const typescriptApi = new TypeScriptAPI();
const typescriptSnapshot = typescriptApi.updateSnapshot({
  openProjects: [join(ROOT, "tsconfig.electron.json")],
});
const electronProject = typescriptSnapshot.getProjects().find(
  ({ configFileName }) => configFileName === join(ROOT, "tsconfig.electron.json"),
);
assert.ok(electronProject, "TypeScript project snapshot did not load tsconfig.electron.json");

after(() => {
  typescriptSnapshot.dispose();
  typescriptApi.close();
});

function visit(node, callback) {
  callback(node);
  node.forEachChild((child) => visit(child, callback));
}

function literalChannel(call, label, argumentIndex = 0) {
  const argument = call.arguments[argumentIndex];
  assert.ok(
    argument && (ts.isStringLiteral(argument) || ts.isNoSubstitutionTemplateLiteral(argument)),
    `${label} must use a literal channel so production discovery cannot hide computed actions`,
  );
  return argument.text;
}

function isIdentifierCall(node, name) {
  return ts.isCallExpression(node)
    && ts.isIdentifier(node.expression)
    && node.expression.text === name;
}

function isPropertyCall(node, objectName, methodName) {
  return ts.isCallExpression(node)
    && ts.isPropertyAccessExpression(node.expression)
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === objectName
    && node.expression.name.text === methodName;
}

function discoverIdentifierCalls(sourceFile, name, argumentIndex = 0) {
  const channels = [];
  visit(sourceFile, (node) => {
    if (isIdentifierCall(node, name)) {
      channels.push(literalChannel(node, name, argumentIndex));
    }
  });
  return channels;
}

function discoverPropertyCalls(sourceFile, objectName, methodName) {
  const channels = [];
  visit(sourceFile, (node) => {
    if (isPropertyCall(node, objectName, methodName)) {
      channels.push(literalChannel(node, `${objectName}.${methodName}`));
    }
  });
  return channels;
}

function sorted(values) {
  return [...values].sort();
}

function electronSource(name) {
  const path = join(ELECTRON_ROOT, name);
  const sourceFile = electronProject.program.getSourceFile(path);
  assert.ok(sourceFile, `TypeScript project snapshot did not load ${name}`);
  return sourceFile;
}

function loadElectronSources() {
  return electronProject.rootFiles
    .filter((path) => dirname(path) === ELECTRON_ROOT)
    .sort()
    .map((path) => ({
      name: path.slice(ELECTRON_ROOT.length + 1),
      path,
      sourceFile: electronProject.program.getSourceFile(path),
    }))
    .map((entry) => {
      assert.ok(entry.sourceFile, `TypeScript project snapshot did not load ${entry.name}`);
      return entry;
    });
}

test("Electron registry is closed, unique, and action-ID stable", () => {
  const channels = ELECTRON_PRODUCTION_SURFACES.map(({ channel }) => channel);
  assert.equal(new Set(channels).size, channels.length);
  assert.equal(new Set(ELECTRON_PRODUCTION_ACTION_IDS).size, ELECTRON_PRODUCTION_ACTION_IDS.length);
  assert.deepEqual(sorted(ELECTRON_INVOKE_CHANNELS), sorted([
    ...ELECTRON_PRODUCTION_SURFACES
      .filter(({ direction }) => direction === "renderer-to-main")
      .map(({ channel }) => channel),
  ]));
  assert.deepEqual(sorted(ELECTRON_OUTBOUND_EVENT_CHANNELS), sorted([
    ...ELECTRON_PRODUCTION_SURFACES
      .filter(({ direction }) => direction === "main-to-renderer")
      .map(({ channel }) => channel),
  ]));
});

test("main handlers and preload invokes equal the registry in both directions", () => {
  const mainSource = electronSource("main.ts");
  const preloadSource = electronSource("preload.cts");
  const mainChannels = discoverPropertyCalls(mainSource, "productionIpcHandlers", "handle");
  const preloadChannels = discoverIdentifierCalls(preloadSource, "invokeProductionIpc");

  assert.equal(new Set(mainChannels).size, mainChannels.length, "main registered a duplicate handler");
  assert.equal(new Set(preloadChannels).size, preloadChannels.length, "preload exposes a duplicate invoke");
  assert.deepEqual(sorted(mainChannels), sorted(ELECTRON_INVOKE_CHANNELS));
  assert.deepEqual(sorted(preloadChannels), sorted(ELECTRON_INVOKE_CHANNELS));
});

test("every production main handler starts with the trusted sender boundary", () => {
  const mainSource = electronSource("main.ts");
  const guardedChannels = [];
  visit(mainSource, (node) => {
    if (!isPropertyCall(node, "productionIpcHandlers", "handle")) return;
    const channel = literalChannel(node, "productionIpcHandlers.handle");
    const handler = node.arguments[1];
    assert.ok(
      handler && (ts.isArrowFunction(handler) || ts.isFunctionExpression(handler)),
      `${channel} must register a directly inspectable function`,
    );
    assert.ok(ts.isBlock(handler.body), `${channel} handler must have a block body`);
    const firstStatement = handler.body.statements[0];
    assert.ok(
      firstStatement && ts.isExpressionStatement(firstStatement),
      `${channel} must begin with an authority expression`,
    );
    const boundaryCall = firstStatement.expression;
    assert.ok(
      isIdentifierCall(boundaryCall, "assertTrustedIpcSender"),
      `${channel} must call assertTrustedIpcSender before any handler side effect`,
    );
    assert.equal(boundaryCall.arguments.length, 2, `${channel} authority boundary is incomplete`);
    assert.ok(
      ts.isIdentifier(boundaryCall.arguments[0])
        && boundaryCall.arguments[0].text === "event",
      `${channel} must authorize its invoke event`,
    );
    assert.ok(
      ts.isIdentifier(boundaryCall.arguments[1])
        && boundaryCall.arguments[1].text === "trustedRendererEntries",
      `${channel} must authorize against the BrowserWindow registry`,
    );
    guardedChannels.push(channel);
  });
  assert.deepEqual(sorted(guardedChannels), sorted(ELECTRON_INVOKE_CHANNELS));
});

test("untrusted renderer frames fail before every production handler", () => {
  const entryUrl = "http://127.0.0.1:5173/desktop";
  const trustedRendererEntries = new Map([[41, entryUrl]]);
  const makeEvent = ({ senderId = 41, frameUrl = entryUrl, subframe = false } = {}) => {
    const mainFrame = { url: frameUrl };
    return {
      sender: { id: senderId, mainFrame },
      senderFrame: subframe ? { url: frameUrl } : mainFrame,
    };
  };
  const rejectedEvents = [
    makeEvent({ senderId: 99 }),
    makeEvent({ subframe: true }),
    makeEvent({ frameUrl: "http://127.0.0.1:5173/navigation-drift" }),
    { ...makeEvent(), senderFrame: null },
  ];

  for (const channel of ELECTRON_INVOKE_CHANNELS) {
    let sideEffects = 0;
    const handler = (event) => {
      assertTrustedIpcSender(event, trustedRendererEntries);
      sideEffects += 1;
    };
    for (const event of rejectedEvents) {
      assert.throws(
        () => handler(event),
        /Rejected IPC call from an untrusted renderer frame/,
        channel,
      );
    }
    assert.equal(sideEffects, 0, `${channel} reached a side effect for an untrusted frame`);
    handler(makeEvent());
    assert.equal(sideEffects, 1, `${channel} rejected its exact registered main frame`);
  }
});

test("open-in-codex routes OS dispatch through the network-profile preflight", () => {
  const mainSource = electronSource("main.ts");
  let targetHandler;
  visit(mainSource, (node) => {
    if (
      isPropertyCall(node, "productionIpcHandlers", "handle")
      && literalChannel(node, "productionIpcHandlers.handle") === "memolens:open-in-codex"
    ) {
      targetHandler = node.arguments[1];
    }
  });
  assert.ok(targetHandler && (ts.isArrowFunction(targetHandler) || ts.isFunctionExpression(targetHandler)));

  const preflightCalls = [];
  const externalDispatchCalls = [];
  visit(targetHandler, (node) => {
    if (isIdentifierCall(node, "dispatchExternalNavigation")) preflightCalls.push(node);
    if (isPropertyCall(node, "shell", "openExternal")) externalDispatchCalls.push(node);
  });
  assert.equal(preflightCalls.length, 1);
  assert.equal(externalDispatchCalls.length, 1);
  let current = externalDispatchCalls[0].parent;
  let insidePreflight = false;
  while (current && current !== targetHandler) {
    if (current === preflightCalls[0]) {
      insidePreflight = true;
      break;
    }
    current = current.parent;
  }
  assert.equal(insidePreflight, true, "shell.openExternal must be the guarded dispatcher");
});

test("main outbound sends and preload subscriptions equal the registry", () => {
  const sources = loadElectronSources();
  const outboundChannels = sources.flatMap(({ sourceFile }) => (
    discoverIdentifierCalls(sourceFile, "sendProductionIpcEvent", 1)
  ));
  const preload = sources.find(({ name }) => name === "preload.cts");
  assert.ok(preload);
  const subscriptionChannels = discoverIdentifierCalls(
    preload.sourceFile,
    "subscribeProductionIpcEvent",
  );

  assert.equal(new Set(outboundChannels).size, outboundChannels.length);
  assert.equal(new Set(subscriptionChannels).size, subscriptionChannels.length);
  assert.deepEqual(sorted(outboundChannels), sorted(ELECTRON_OUTBOUND_EVENT_CHANNELS));
  assert.deepEqual(sorted(subscriptionChannels), sorted(ELECTRON_OUTBOUND_EVENT_CHANNELS));
});

test("Electron source cannot bypass the production IPC boundaries", () => {
  const sources = loadElectronSources();
  const violations = [];
  for (const { name, sourceFile } of sources) {
    if (name === "productionSurfaceRegistry.ts") continue;
    visit(sourceFile, (node) => {
      if (
        isPropertyCall(node, "ipcMain", "handle")
        || isPropertyCall(node, "ipcMain", "on")
        || isPropertyCall(node, "ipcMain", "once")
      ) {
        violations.push(`${name}: raw ipcMain.${node.expression.name.text}`);
      }
      if (
        ts.isCallExpression(node)
        && ts.isPropertyAccessExpression(node.expression)
        && node.expression.name.text === "send"
      ) {
        violations.push(`${name}: raw sender.send`);
      }
      if (
        (isPropertyCall(node, "ipcRenderer", "invoke")
          || isPropertyCall(node, "ipcRenderer", "on")
          || isPropertyCall(node, "ipcRenderer", "removeListener"))
        && !isInsideApprovedPreloadBoundary(node)
      ) {
        violations.push(`${name}: raw ipcRenderer.${node.expression.name.text}`);
      }
      if (
        isPropertyCall(node, "ipcRenderer", "send")
        || isPropertyCall(node, "ipcRenderer", "sendSync")
        || isPropertyCall(node, "ipcRenderer", "postMessage")
      ) {
        violations.push(`${name}: unregistered ipcRenderer.${node.expression.name.text}`);
      }
    });
  }
  assert.deepEqual(violations, []);
});

function isInsideApprovedPreloadBoundary(node) {
  const allowed = new Set(["invokeProductionIpc", "subscribeProductionIpcEvent"]);
  let current = node.parent;
  while (current) {
    if (ts.isFunctionDeclaration(current) && current.name && allowed.has(current.name.text)) {
      return true;
    }
    current = current.parent;
  }
  return false;
}

test("unknown, duplicate, and incomplete main registrations fail closed", () => {
  const calls = [];
  const target = {
    handle(channel, listener) {
      calls.push({ channel, listener });
    },
  };
  const registry = new ProductionIpcHandlerRegistry(target);
  assert.throws(
    () => registry.handle("memolens:future-undeclared-action", () => undefined),
    /Unknown Electron invoke channel rejected/,
  );
  registry.handle(ELECTRON_INVOKE_CHANNELS[0], () => undefined);
  assert.throws(
    () => registry.handle(ELECTRON_INVOKE_CHANNELS[0], () => undefined),
    /Duplicate Electron invoke handler rejected/,
  );
  assert.throws(() => registry.assertComplete(), /handlers are incomplete/);
  assert.equal(calls.length, 1, "rejected registrations must not reach ipcMain.handle");

  const complete = new ProductionIpcHandlerRegistry({ handle() {} });
  for (const channel of ELECTRON_INVOKE_CHANNELS) {
    complete.handle(channel, () => undefined);
  }
  assert.doesNotThrow(() => complete.assertComplete());
});

test("unknown outbound events fail before sender.send", () => {
  const calls = [];
  const sender = { send: (...args) => calls.push(args) };
  assert.throws(
    () => sendProductionIpcEvent(sender, "memolens:future-event"),
    /Unknown Electron outbound event channel rejected/,
  );
  assert.deepEqual(calls, []);
  sendProductionIpcEvent(sender, ELECTRON_OUTBOUND_EVENT_CHANNELS[0], { completed: 1 });
  assert.deepEqual(calls, [[ELECTRON_OUTBOUND_EVENT_CHANNELS[0], { completed: 1 }]]);
});

test("frozen inventory declares exactly the dynamically registered Electron actions", async () => {
  const inventory = JSON.parse(
    await readFile(join(ROOT, "core", "production_surface_inventory.v1.json"), "utf8"),
  );
  const declared = inventory.actions
    .filter(({ surface }) => surface === "electron_ipc")
    .map(({ id }) => id);
  assert.deepEqual(sorted(declared), sorted(ELECTRON_PRODUCTION_ACTION_IDS));
});
