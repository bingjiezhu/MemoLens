import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";

import {
  canAdoptOpenedVideoProject,
  canApplyVideoProjectResponse,
  loadExistingVideoProject,
  persistOpenedVideoProject,
} from "../src/video/projectOpen.ts";
import { initialVideoWorkbenchState, videoWorkbenchReducer } from "../src/video/projectReducer.ts";
import {
  canResumePersistedVideoSession,
  createVideoScopeKey,
  persistVideoSession,
  readPersistedVideoSession,
} from "../src/video/session.ts";

const DATABASE_UUID = "11111111-2222-4333-8444-555555555555";
const scope = {
  scopeKey: createVideoScopeKey("/photos", "/library/a.db"),
  scopeEpoch: 1,
  apiBase: "http://127.0.0.1:43110",
  dbPath: "/library/a.db",
  requestId: 1,
};

function canonical(projectId = "proj_from_plugin") {
  return {
    object: "creative.project_workspace",
    canonical_source: "creative_blueprint",
    database_uuid: DATABASE_UUID,
    id: projectId,
    project_id: projectId,
    current_blueprint: { database_uuid: DATABASE_UUID, project_id: projectId },
  };
}

function legacy(projectId = "proj_legacy", timelineId = null) {
  return { id: projectId, brief: { revision: 1 }, latest_timeline_id: timelineId, candidates: [] };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness(readers) {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) ?? null; },
    setItem(key, value) { this.values.set(key, value); },
  };
  let current = { ...scope };
  let workspace = null;
  let state = initialVideoWorkbenchState(scope.scopeKey);
  async function open(projectId, options = {}) {
    const request = { ...current, projectId };
    const opened = await loadExistingVideoProject({
      request,
      signal: new AbortController().signal,
      isCurrent: () => canAdoptOpenedVideoProject(request, current),
      ...options,
    }, readers);
    if (!opened || !persistOpenedVideoProject(storage, opened, current)) return false;
    if (opened.kind === "canonical") {
      workspace = opened.workspace;
      state = videoWorkbenchReducer(state, { type: "canonical_workspace_opened" });
    } else {
      workspace = null;
      state = videoWorkbenchReducer(state, { type: "project_ready", project: opened.project, resetWorkspace: true });
      if (opened.timeline) state = videoWorkbenchReducer(state, { type: "timeline_ready", timeline: opened.timeline });
    }
    return true;
  }
  return {
    storage, open,
    get workspace() { return workspace; },
    get state() { return state; },
    setScope(next) { current = { ...current, ...next, scopeEpoch: current.scopeEpoch + 1 }; },
    nextRequest() { current = { ...current, requestId: current.requestId + 1 }; },
  };
}

test("an empty renderer session opens an exact plugin-created canonical project and persists it", async () => {
  const calls = [];
  const app = harness({
    async fetchCreativeProject(...args) { calls.push(args); return canonical(); },
    async fetchTimeline() { assert.fail("a canonical project must open the Blueprint workspace"); },
  });
  assert.equal(readPersistedVideoSession(app.storage, scope.scopeKey), null);
  assert.equal(await app.open("proj_from_plugin"), true);
  assert.deepEqual(calls[0].slice(0, 3), [scope.apiBase, "proj_from_plugin", scope.dbPath]);
  assert.equal(app.workspace.project_id, "proj_from_plugin");
  assert.equal(app.state.project, null);
  assert.equal(app.state.projectPhase, "ready");
  assert.deepEqual(readPersistedVideoSession(app.storage, scope.scopeKey), {
    projectId: "proj_from_plugin", timelineId: null, timelineRevision: null,
  });
});

test("a wrong Project ID is recoverable and preserves the visible project and saved session", async () => {
  const app = harness({
    async fetchCreativeProject(_base, id) {
      if (id === "missing") throw new Error("Project not found");
      return canonical(id);
    },
  });
  await app.open("proj_original");
  const saved = readPersistedVideoSession(app.storage, scope.scopeKey);
  await assert.rejects(app.open("missing"), /Project not found/);
  assert.equal(app.workspace.project_id, "proj_original");
  assert.deepEqual(readPersistedVideoSession(app.storage, scope.scopeKey), saved);
  await app.open("proj_recovered");
  assert.equal(app.workspace.project_id, "proj_recovered");
});

test("project, database UUID, and Blueprint identity substitutions are rejected before adoption", async () => {
  for (const mutate of [
    (value) => { value.id = "wrong-project"; },
    (value) => { value.project_id = "wrong-project"; },
    (value) => { value.current_blueprint.project_id = "wrong-project"; },
    (value) => { value.current_blueprint.database_uuid = "other-database"; },
    (value) => { value.database_uuid = "other-database"; value.current_blueprint.database_uuid = "other-database"; },
  ]) {
    const project = canonical();
    mutate(project);
    const app = harness({ async fetchCreativeProject() { return project; } });
    await assert.rejects(app.open("proj_from_plugin", { expectedDatabaseUuid: DATABASE_UUID }), /does not match/);
    assert.equal(app.workspace, null);
    assert.equal(readPersistedVideoSession(app.storage, scope.scopeKey), null);
  }
});

test("switching libraries, resetting the same scope, and A -> B -> A all suppress delayed project reads", async () => {
  for (const change of [
    (app) => app.setScope({ dbPath: "/library/b.db", scopeKey: createVideoScopeKey("/photos", "/library/b.db") }),
    (app) => app.setScope({}),
    (app) => { app.setScope({ dbPath: "/library/b.db" }); app.setScope({ dbPath: scope.dbPath }); },
    (app) => app.setScope({ apiBase: "http://127.0.0.1:43111" }),
    (app) => app.nextRequest(),
  ]) {
    const pending = deferred();
    const app = harness({ fetchCreativeProject() { return pending.promise; } });
    const opening = app.open("proj_from_plugin");
    change(app);
    pending.resolve(canonical()); // Deliberately ignores AbortSignal.
    assert.equal(await opening, false);
    assert.equal(app.workspace, null);
    assert.equal(readPersistedVideoSession(app.storage, scope.scopeKey), null);
  }
});

test("legacy session restoration still reads its exact saved timeline and revision", async () => {
  const calls = [];
  const app = harness({
    async fetchCreativeProject() { return legacy(); },
    async fetchTimeline(...args) {
      calls.push(args);
      return { id: "timeline_saved", project_id: "proj_legacy", revision: 4 };
    },
  });
  const saved = { projectId: "proj_legacy", timelineId: "timeline_saved", timelineRevision: 4 };
  persistVideoSession(app.storage, scope.scopeKey, saved);
  assert.equal(canResumePersistedVideoSession({
    canUseBackend: true, dbPath: scope.dbPath, requestedScopeKey: scope.scopeKey,
    stateScopeKey: app.state.scopeKey, hasProject: false, projectPhase: app.state.projectPhase,
  }), true);
  await app.open(saved.projectId, { savedSession: readPersistedVideoSession(app.storage, scope.scopeKey) });
  assert.deepEqual(calls[0].slice(0, 4), [scope.apiBase, "timeline_saved", 4, scope.dbPath]);
  assert.equal(app.state.project.id, "proj_legacy");
  assert.equal(app.state.timeline.revision, 4);
  assert.deepEqual(readPersistedVideoSession(app.storage, scope.scopeKey), saved);
});

test("a persisted canonical session resumes through the same exact project path", async () => {
  const app = harness({ async fetchCreativeProject() { return canonical(); } });
  const saved = { projectId: "proj_from_plugin", timelineId: "obsolete_legacy_timeline", timelineRevision: 1 };
  persistVideoSession(app.storage, scope.scopeKey, saved);
  await app.open(saved.projectId, { savedSession: saved });
  assert.equal(app.workspace.project_id, saved.projectId);
  assert.equal(readPersistedVideoSession(app.storage, scope.scopeKey).timelineId, null);
});

test("legacy loading is atomic when a timeline read fails or the scope resets mid-read", async () => {
  for (const reset of [false, true]) {
    const pending = deferred();
    const app = harness({
      async fetchCreativeProject(_base, id) { return id === "proj_original" ? canonical(id) : legacy(id, "timeline_new"); },
      fetchTimeline() { return pending.promise; },
    });
    await app.open("proj_original");
    const saved = readPersistedVideoSession(app.storage, scope.scopeKey);
    const opening = app.open("proj_new");
    await Promise.resolve();
    if (reset) {
      app.setScope({});
      pending.resolve({ id: "timeline_new", project_id: "proj_new", revision: 1 });
      assert.equal(await opening, false);
    } else {
      pending.reject(new Error("Timeline unavailable"));
      await assert.rejects(opening, /Timeline unavailable/);
    }
    assert.equal(app.workspace.project_id, "proj_original");
    assert.deepEqual(readPersistedVideoSession(app.storage, scope.scopeKey), saved);
  }
});

test("legacy timeline identity and revision must match the requested project", async () => {
  for (const timeline of [
    { id: "wrong", project_id: "proj_legacy", revision: 2 },
    { id: "timeline_expected", project_id: "wrong", revision: 2 },
    { id: "timeline_expected", project_id: "proj_legacy", revision: 3 },
  ]) {
    const app = harness({
      async fetchCreativeProject() { return { ...legacy("proj_legacy", "timeline_expected"), latest_timeline_revision: 2 }; },
      async fetchTimeline() { return timeline; },
    });
    await assert.rejects(app.open("proj_legacy"), /timeline does not match/);
    assert.equal(app.state.project, null);
    assert.equal(app.storage.values.size, 0);
  }
});

test("canonical retry cannot silently adopt a legacy project", async () => {
  const app = harness({ async fetchCreativeProject() { return legacy(); } });
  await assert.rejects(app.open("proj_legacy", { canonicalOnly: true }), /will not fall back/);
  assert.equal(app.state.project, null);
  assert.equal(app.storage.values.size, 0);
});

test("reopening a legacy project clears a timeline no longer present in its saved head", async () => {
  let hasTimeline = true;
  const app = harness({
    async fetchCreativeProject() { return legacy("proj_legacy", hasTimeline ? "timeline_old" : null); },
    async fetchTimeline() { return { id: "timeline_old", project_id: "proj_legacy", revision: 1 }; },
  });
  await app.open("proj_legacy");
  assert.equal(app.state.timeline.id, "timeline_old");
  hasTimeline = false;
  await app.open("proj_legacy");
  assert.equal(app.state.timeline, null);
  assert.equal(readPersistedVideoSession(app.storage, scope.scopeKey).timelineId, null);
});

test("empty IDs fail before a read and cancelled reads cannot be adopted", async () => {
  const request = { ...scope, projectId: "" };
  await assert.rejects(loadExistingVideoProject({
    request, signal: new AbortController().signal, isCurrent: () => true,
  }, { fetchCreativeProject() { assert.fail("empty ID must not be fetched"); } }), /exact Project ID/);

  const pending = deferred();
  const controller = new AbortController();
  const opening = loadExistingVideoProject({
    request: { ...request, projectId: "proj_from_plugin" },
    signal: controller.signal, isCurrent: () => true,
  }, { fetchCreativeProject() { return pending.promise; } });
  controller.abort();
  pending.resolve(canonical());
  assert.equal(await opening, null);
});

test("a response invalidated after loading cannot persist a session", async () => {
  const request = { ...scope, projectId: "proj_from_plugin" };
  const result = await loadExistingVideoProject({ request, signal: new AbortController().signal, isCurrent: () => true }, {
    async fetchCreativeProject() { return canonical(); },
  });
  const storage = { setItem() { assert.fail("stale session write"); } };
  assert.equal(persistOpenedVideoProject(storage, result, { ...scope, scopeEpoch: 2 }), false);
});

test("visible project entry is wired in the initial, canonical, and integrity-error workspaces", () => {
  const source = readFileSync(new URL("../src/VideoWorkbench.tsx", import.meta.url), "utf8");
  assert.match(source, /<h3 id="video-open-project-title">Open existing project<\/h3>/);
  assert.match(source, /openExistingProject\(projectIdInput\)/);
  assert.match(source, /openExistingProject\(saved\.projectId/);
  assert.equal((source.match(/\{projectOpenPanel\}/g) ?? []).length, 3);
});

// Run the actual disclosure hook and its component inputs with dependency-aware
// hook scheduling. Native toggles use the real handler, including mid-request.
function projectDisclosureHarness() {
  const source = readFileSync(new URL("../src/VideoWorkbench.tsx", import.meta.url), "utf8");
  assert.match(source, /open=\{projectDisclosure\.open\}/);
  assert.match(source, /onToggle=\{projectDisclosure\.onToggle\}/);
  const hookStart = source.indexOf("function useProjectOpenDisclosure(");
  const hookEnd = source.indexOf("\nfunction VideoWorkbench(", hookStart);
  const inputsStart = source.indexOf("  const openedProjectId =");
  const inputsEnd = source.indexOf("  const projectOpenRequestIdRef =", inputsStart);
  assert.ok(hookStart >= 0 && hookEnd > hookStart && inputsStart >= 0 && inputsEnd > inputsStart);
  let open;
  let initialized = false;
  let changed = false;
  let effectIndex = 0;
  let pending = [];
  let disclosure;
  const dependencies = [];
  const context = {
    scopeKey: scope.scopeKey,
    state: { project: null },
    blueprintWorkspace: null,
    isOpeningProject: false,
    projectOpenError: null,
    canonicalWorkspaceFailure: null,
    useState(initial) {
      if (!initialized) { open = initial; initialized = true; }
      return [open, (next) => {
        if (!Object.is(open, next)) { open = next; changed = true; }
      }];
    },
    useEffect(effect, next) {
      const previous = dependencies[effectIndex];
      if (!previous || next.some((value, index) => !Object.is(value, previous[index]))) pending.push(effect);
      dependencies[effectIndex++] = next;
    },
  };
  runInNewContext(stripTypeScriptTypes(source.slice(hookStart, hookEnd)), context);
  const renderSource = stripTypeScriptTypes(`(() => {${source.slice(inputsStart, inputsEnd)}return projectDisclosure;})()`);
  function render(patch = {}) {
    Object.assign(context, patch);
    for (let pass = 0; pass < 5; pass += 1) {
      changed = false;
      effectIndex = 0;
      pending = [];
      disclosure = runInNewContext(renderSource, context);
      for (const effect of pending) effect();
      if (!changed) return disclosure.open;
    }
    assert.fail("project disclosure did not settle");
  }
  return {
    render,
    nativeToggle(value) {
      disclosure.onToggle({ currentTarget: { open: value } });
      return render();
    },
  };
}

test("project disclosure reopens on failure after a native collapse during the pending request", () => {
  const app = projectDisclosureHarness();
  assert.equal(app.render(), false);
  assert.equal(app.nativeToggle(true), true);
  assert.equal(app.render({ isOpeningProject: true }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ isOpeningProject: false, projectOpenError: "Project read failed. Retry." }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render(), false, "an unchanged error must not prevent manual collapse");
  assert.equal(app.render({ isOpeningProject: true, projectOpenError: null }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ isOpeningProject: false, projectOpenError: "Project read failed. Retry." }), true);
});

test("project disclosure preserves manual collapse across polling but opens a changed project or library", () => {
  const app = projectDisclosureHarness();
  assert.equal(app.render({ state: { project: legacy("proj_A") } }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ state: { project: { ...legacy("proj_A"), title: "Updated title" } } }), false);
  assert.equal(app.render({ state: { project: legacy("proj_B") } }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ scopeKey: createVideoScopeKey("/other", "/library/b.db") }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ blueprintWorkspace: canonical("proj_canonical") }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ blueprintWorkspace: { ...canonical("proj_canonical"), title: "Polled title" } }), false);
});

test("project disclosure reveals a new canonical failure without reopening for unchanged failure objects", () => {
  const app = projectDisclosureHarness();
  assert.equal(app.render({ isOpeningProject: true }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({
    isOpeningProject: false,
    canonicalWorkspaceFailure: { projectId: "proj_A", message: "Canonical read failed" },
  }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ canonicalWorkspaceFailure: { projectId: "proj_A", message: "Canonical read failed" } }), false);
  assert.equal(app.render({ canonicalWorkspaceFailure: { projectId: "proj_A", message: "Integrity check failed" } }), true);
  assert.equal(app.nativeToggle(false), false);
  assert.equal(app.render({ canonicalWorkspaceFailure: { projectId: "proj_B", message: "Integrity check failed" } }), true);
});

// Execute the actual component handlers with a delayed transport. Keeping the
// rendered `state` snapshot old models a response arriving before React commits.
function handlerHarness(overrides = {}) {
  const source = readFileSync(new URL("../src/VideoWorkbench.tsx", import.meta.url), "utf8");
  function extract(name) {
    const match = new RegExp(`^  (?:async )?function ${name}\\(`, "m").exec(source);
    assert.ok(match, `missing handler ${name}`);
    const remainder = source.slice(match.index + 1);
    const next = /^  (?:async )?function /m.exec(remainder);
    assert.ok(next, `missing boundary after ${name}`);
    return source.slice(match.index, match.index + 1 + next.index);
  }
  const initial = videoWorkbenchReducer(initialVideoWorkbenchState(scope.scopeKey), {
    type: "project_ready", project: legacy("proj_A"),
  });
  initial.timeline = { id: "timeline_A", project_id: "proj_A", revision: 1, content_sha256: "a".repeat(64) };
  initial.capabilities = { preview_root_id: "preview-root" };
  const actions = [];
  const savedFlags = [];
  const instructionPreviews = [];
  const stateRef = { current: initial };
  const context = {
    state: initial,
    currentStateRef: stateRef,
    projectOpenScopeRef: { current: { ...scope } },
    projectOpenRequestIdRef: { current: scope.requestId },
    blueprintWorkspaceRef: { current: null },
    projectDatabaseUuidRef: { current: null },
    canAdoptOpenedVideoProject, canApplyVideoProjectResponse, videoWorkbenchReducer,
    dispatchState(action) { actions.push(action); },
    createTrackedController: () => new AbortController(), releaseController() {},
    apiBase: scope.apiBase, dbPath: scope.dbPath, scopeKey: scope.scopeKey,
    AbortController, DOMException, Error, Set,
    canWrite: true, canPreviewRender: true, canVerifiedPreviewSaveAs: true,
    renderActive: false, renderCompleted: true, isWriting: false, desktopRuntime: true,
    recoveredRenderJobs: [], commandText: "Trim the opening", pendingInstruction: null,
    setIsWriting() {}, setIsSavingArtifact() {}, setCommandText() {},
    setArtifactSaved(value) { savedFlags.push(value); },
    setPendingInstruction(value) { instructionPreviews.push(value); },
    setRecoveredRenderJobs() {},
    humanError: (error) => error.message,
    mutationLedger: { async acquire() { return { idempotencyKey: "fixture-key" }; }, settle() {} },
    settleMutationError: () => ({ kind: "network_error" }),
    shouldReconcileTimelineMutation: () => true,
    isAmbiguousVideoMutationOutcome: () => true,
    renderDownloadUrl: () => "http://127.0.0.1:43110/fixture.mp4",
    defaultPreviewFilename: () => "preview.mp4",
    persistTimelineHead() { assert.fail("a stale response must not persist a timeline head"); },
    fetchTimeline() { assert.fail("a stale mutation must not begin a reconciliation read"); },
    fetchCreativeProject() { assert.fail("a stale mutation must not reload its previous project"); },
    fetchRecentRenderJobs() { assert.fail("a stale render must not reconcile into a new project"); },
    ...overrides,
  };
  const functions = [
    "dispatch", "currentProjectOpenScope", "captureProjectRequest", "isCurrentProjectRequest",
    "isCurrentProjectScope", "assertCurrentProjectRequest", "restoreLatestTimeline",
    "restoreProjectLatestTimeline", "restoreRenderFromJobList", "handleCreateTimeline",
    "applyTimelineOperations", "prepareInstruction", "confirmInstruction", "handleValidate",
    "handleRender", "handleCancelRender", "handleSaveArtifact",
  ];
  runInNewContext(stripTypeScriptTypes(functions.map(extract).join("\n")), context);
  return {
    context, actions, savedFlags, instructionPreviews,
    get current() { return stateRef.current; },
    switchProject(projectId = "proj_B") {
      context.projectOpenRequestIdRef.current += 1;
      stateRef.current = videoWorkbenchReducer(stateRef.current, {
        type: "project_ready", project: legacy(projectId), resetWorkspace: true,
      });
      stateRef.current = videoWorkbenchReducer(stateRef.current, {
        type: "timeline_ready", timeline: { id: `timeline_${projectId}`, project_id: projectId, revision: 1 },
      });
    },
  };
}

test("actual Validate handler discards A's delayed success and error after opening B in the same library", async () => {
  for (const fail of [false, true]) {
    const pending = deferred();
    const app = handlerHarness({ validateTimeline: () => pending.promise });
    const validation = app.context.handleValidate();
    app.switchProject();
    if (fail) pending.reject(new Error("A validation failed"));
    else pending.resolve({ id: "timeline_A", valid: true });
    assert.equal(await validation, false);
    assert.equal(app.current.project.id, "proj_B");
    assert.equal(app.current.validation, null);
    assert.equal(app.current.validationError, null);
    assert.equal(app.actions.some((action) => ["validation_ready", "validation_error"].includes(action.type)), false);
  }
});

test("actual Validate handler rejects a changed timeline revision and an A -> B -> A scope epoch", async () => {
  for (const change of [
    (app) => { app.context.currentStateRef.current = { ...app.current, timeline: { ...app.current.timeline, revision: 2 } }; },
    (app) => { app.context.projectOpenScopeRef.current.scopeEpoch += 2; },
  ]) {
    const pending = deferred();
    const app = handlerHarness({ validateTimeline: () => pending.promise });
    const validation = app.context.handleValidate();
    change(app);
    pending.resolve({ valid: true });
    assert.equal(await validation, false);
    assert.equal(app.current.validation, null);
  }
});

test("actual Render handler cannot start a render from a superseded validation", async () => {
  const pending = deferred();
  const app = handlerHarness({
    validateTimeline: () => pending.promise,
    startRender() { assert.fail("stale validation started a render"); },
  });
  const rendering = app.context.handleRender("preview");
  app.switchProject();
  pending.resolve({ valid: true });
  await rendering;
  assert.equal(app.current.renderJob, null);
  assert.equal(app.current.validation, null);
});

test("actual timeline mutation discards a late success or reconciliation after changing projects", async () => {
  for (const fail of [false, true]) {
    const pending = deferred();
    const sent = deferred();
    const app = handlerHarness({ reviseTimeline() { sent.resolve(); return pending.promise; } });
    const editing = app.context.applyTimelineOperations([{ op: "set_duration", clip_id: "clip_A", timeline_duration_ms: 1000 }]);
    await sent.promise;
    app.switchProject();
    if (fail) pending.reject(new Error("A connection lost"));
    else pending.resolve({ timeline: { id: "timeline_A", project_id: "proj_A", revision: 2 }, diff: [] });
    await editing;
    assert.equal(app.current.timeline.project_id, "proj_B");
    assert.equal(app.current.timeline.revision, 1);
    assert.equal(app.current.timelineError, null);
  }
});

test("actual mutation rechecks project identity after waiting for its local lease", async () => {
  const lease = deferred();
  const app = handlerHarness({
    mutationLedger: { acquire: () => lease.promise, settle() {} },
    reviseTimeline() { assert.fail("stale mutation reached the backend"); },
  });
  const editing = app.context.applyTimelineOperations([{ op: "set_duration", clip_id: "clip_A", timeline_duration_ms: 1000 }]);
  app.switchProject();
  lease.resolve({ idempotencyKey: "old-lease" });
  await editing;
  assert.equal(app.current.timeline.project_id, "proj_B");
});

test("actual instruction preview and completed artifact callback cannot change a new project", async () => {
  const preview = deferred();
  const previewApp = handlerHarness({ previewTimelineInstruction: () => preview.promise });
  const previewing = previewApp.context.prepareInstruction();
  previewApp.switchProject();
  preview.resolve({ operations: [], diff: [] });
  await previewing;
  assert.deepEqual(previewApp.instructionPreviews, [null]);

  const saved = deferred();
  const saveApp = handlerHarness({ saveVideoArtifactOnDesktop: () => saved.promise });
  saveApp.context.state.renderJob = {
    id: "render_A", timeline_id: "timeline_A", timeline_revision: 1,
    output_sha256: "a".repeat(64), size_bytes: 123,
  };
  const saving = saveApp.context.handleSaveArtifact();
  saveApp.switchProject();
  saved.resolve({ status: "saved", message: "A was saved" });
  await saving;
  assert.deepEqual(saveApp.savedFlags, []);
  assert.equal(saveApp.current.saveMessage, null);
});
