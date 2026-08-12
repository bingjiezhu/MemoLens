import assert from "node:assert/strict";
import test from "node:test";

import { createDraftGenerationController } from "../src/generation/controller.ts";
import {
  GENERATION_UNAVAILABLE_ERROR,
  applyDraftCopyUpdate,
  buildDraftGenerationRequestKey,
  classifyDraftGenerationFailure,
  createInitialDraftGenerationState,
  reduceDraftGenerationState,
} from "../src/generation/model.ts";

function draft(id = "draft-1") {
  return {
    id,
    prompt: "quiet coast",
    title: "Quiet Coast",
    caption: "A slow afternoon.",
    candidateCount: 3,
    selectedCount: 1,
    selected: [{ id: "photo-1" }],
    analysis: { focus: "coast" },
    notes: ["note"],
  };
}

function abortError() {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

function baseInput(overrides = {}) {
  return {
    canGenerate: true,
    connectionState: "connected",
    desktopRuntime: false,
    prompt: " quiet coast ",
    fallbackPrompt: "fallback prompt",
    variant: "balanced",
    contextAssetIds: [],
    fetchDraft: async () => draft(),
    createMockDraft: (_prompt, _variant, seed) => draft(`mock-${seed}`),
    restartBackend: async () => null,
    ...overrides,
  };
}

test("request keys preserve variant, normalized prompt, basket order, and the unit separator", () => {
  assert.equal(
    buildDraftGenerationRequestKey("soft", "quiet coast", ["photo-b", "photo-a"]),
    "soft\u001fquiet coast\u001fphoto-b\u001fphoto-a",
  );
  assert.notEqual(
    buildDraftGenerationRequestKey("soft", "quiet coast", ["photo-a", "photo-b"]),
    buildDraftGenerationRequestKey("soft", "quiet coast", ["photo-b", "photo-a"]),
  );
});

test("generation state transitions preserve reconnect, completion, cancellation, and timeout copy", () => {
  const initial = createInitialDraftGenerationState();
  const started = reduceDraftGenerationState(initial, { type: "started", variant: "soft" });
  assert.equal(started.isGenerating, true);
  assert.equal(started.activeVariant, "soft");
  assert.equal(started.progress.title, "Searching and curating");

  const reconnecting = reduceDraftGenerationState(started, { type: "reconnecting" });
  assert.equal(reconnecting.progress.phase, "running");
  assert.equal(reconnecting.progress.title, "Reconnecting to backend");

  const cancelled = reduceDraftGenerationState(reconnecting, {
    type: "aborted",
    reason: "cancelled",
  });
  assert.equal(cancelled.isGenerating, false);
  assert.equal(cancelled.progress.phase, "cancelled");
  assert.equal(
    cancelled.error,
    "Draft generation cancelled. You can adjust the prompt and retry.",
  );

  const timedOut = reduceDraftGenerationState(started, {
    type: "aborted",
    reason: "timed_out",
  });
  assert.equal(timedOut.progress.phase, "timed_out");
  assert.match(timedOut.error, /timed out after 90 seconds/);

  const completed = reduceDraftGenerationState(started, { type: "completed" });
  assert.equal(completed.isGenerating, false);
  assert.equal(completed.progress.percent, 100);
  assert.equal(completed.error, null);
});

test("an unavailable start changes only the error presentation", () => {
  const completed = reduceDraftGenerationState(
    reduceDraftGenerationState(createInitialDraftGenerationState(), {
      type: "started",
      variant: "balanced",
    }),
    { type: "completed" },
  );
  const rejected = reduceDraftGenerationState(completed, {
    type: "rejected",
    message: GENERATION_UNAVAILABLE_ERROR,
  });

  assert.equal(rejected.progress.phase, "completed");
  assert.equal(rejected.error, GENERATION_UNAVAILABLE_ERROR);
});

test("error classification distinguishes user abort, timeout, fetch failures, and backend errors", () => {
  assert.deepEqual(classifyDraftGenerationFailure(abortError(), true, "cancelled"), {
    kind: "aborted",
    reason: "cancelled",
  });
  assert.deepEqual(classifyDraftGenerationFailure(abortError(), true, "timed_out"), {
    kind: "aborted",
    reason: "timed_out",
  });
  assert.deepEqual(classifyDraftGenerationFailure(new TypeError("Failed to fetch"), false, null), {
    kind: "network",
  });
  assert.deepEqual(classifyDraftGenerationFailure(new Error("backend rejected"), false, null), {
    kind: "failed",
    message: "backend rejected",
  });
});

test("copy updates replace only non-empty streamed fields", () => {
  const current = draft();
  const updated = applyDraftCopyUpdate(current, {
    title: "  ",
    caption: "New caption",
    notes: [],
  });

  assert.equal(updated.title, current.title);
  assert.equal(updated.caption, "New caption");
  assert.deepEqual(updated.notes, current.notes);
});

test("the controller suppresses an identical in-flight request", async () => {
  const controller = createDraftGenerationController();
  let fetchCount = 0;
  const fetchDraft = ({ signal }) => {
    fetchCount += 1;
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(abortError()), { once: true });
    });
  };
  const input = baseInput({ fetchDraft, contextAssetIds: ["photo-2", "photo-1"] });

  const first = controller.run(input);
  await controller.run(input);
  assert.equal(fetchCount, 1);
  controller.cancel();
  await first;
  assert.equal(controller.getState().progress.phase, "cancelled");
});

test("a different request supersedes stale work without publishing cancellation", async () => {
  const controller = createDraftGenerationController();
  const results = [];
  const first = controller.run(baseInput({
    prompt: "first",
    fetchDraft: ({ signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(abortError()), { once: true });
    }),
    onResult: (result) => results.push(result.id),
  }));
  const second = controller.run(baseInput({
    prompt: "second",
    fetchDraft: async () => draft("second"),
    onResult: (result) => results.push(result.id),
  }));

  await Promise.all([first, second]);

  assert.deepEqual(results, ["second"]);
  assert.equal(controller.getState().progress.phase, "completed");
  assert.equal(controller.getState().error, null);
});

test("a desktop fetch failure restarts once, retries once, and publishes the result", async () => {
  const controller = createDraftGenerationController();
  const results = [];
  const statuses = [];
  let fetchCount = 0;
  let restartCount = 0;

  await controller.run(baseInput({
    desktopRuntime: true,
    fetchDraft: async () => {
      fetchCount += 1;
      if (fetchCount === 1) {
        throw new TypeError("Failed to fetch");
      }
      return draft("retried");
    },
    restartBackend: async () => {
      restartCount += 1;
      return {
        state: "started",
        message: "backend restarted",
        url: "http://127.0.0.1:5519",
        startedByApp: true,
      };
    },
    onBackendRestarted: (status) => statuses.push(status),
    onResult: (result) => results.push(result),
  }));

  assert.equal(fetchCount, 2);
  assert.equal(restartCount, 1);
  assert.equal(statuses.length, 1);
  assert.equal(results[0].id, "retried");
  assert.equal(controller.getState().progress.phase, "completed");
});

test("the timeout clock aborts work with timeout-specific presentation", async () => {
  let scheduled = null;
  const clock = {
    setTimeout(callback) {
      scheduled = callback;
      return 1;
    },
    clearTimeout() {},
  };
  const controller = createDraftGenerationController(clock, 90_000);
  const pending = controller.run(baseInput({
    fetchDraft: ({ signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(abortError()), { once: true });
    }),
  }));

  scheduled();
  await pending;

  assert.equal(controller.getState().progress.phase, "timed_out");
  assert.match(controller.getState().error, /timed out after 90 seconds/);
});

test("a streamed copy update remains valid after the matching draft completes", async () => {
  const controller = createDraftGenerationController();
  const updates = [];
  let emitCopyUpdate = null;
  let shouldApplyCopyUpdate = null;

  await controller.run(baseInput({
    fetchDraft: async (request) => {
      emitCopyUpdate = request.onCopyUpdate;
      shouldApplyCopyUpdate = request.shouldApplyCopyUpdate;
      return draft();
    },
    onCopyUpdate: (update) => updates.push(update),
  }));

  assert.equal(shouldApplyCopyUpdate(), true);
  emitCopyUpdate({ caption: "Late generated copy" });
  assert.deepEqual(updates, [{ caption: "Late generated copy" }]);
});

test("mock mode keeps the historical first-run seed of two", async () => {
  const controller = createDraftGenerationController();
  const results = [];
  await controller.run(baseInput({
    connectionState: "mock",
    fetchDraft: async () => {
      throw new Error("library fetch must not run in mock mode");
    },
    onResult: (result) => results.push(result),
  }));

  assert.equal(results[0].id, "mock-2");
});

test("reset clears the presentation and suppresses settlement from the previous scope", async () => {
  const controller = createDraftGenerationController();
  const results = [];
  const pending = controller.run(baseInput({
    variant: "soft",
    fetchDraft: ({ signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(abortError()), { once: true });
    }),
    onResult: (result) => results.push(result.id),
  }));

  controller.reset();
  await pending;

  assert.deepEqual(results, []);
  assert.equal(controller.getState().progress.phase, "idle");
  assert.equal(controller.getState().error, null);
  assert.equal(controller.getState().isGenerating, false);
  assert.equal(controller.getState().activeVariant, "soft");
});

test("browser and retry fetch failures preserve the transport error message", async () => {
  const browserController = createDraftGenerationController();
  await browserController.run(baseInput({
    fetchDraft: async () => {
      throw new TypeError("Failed to fetch");
    },
  }));
  assert.equal(browserController.getState().error, "Failed to fetch");

  const desktopController = createDraftGenerationController();
  await desktopController.run(baseInput({
    desktopRuntime: true,
    fetchDraft: async () => {
      throw new TypeError("Failed to fetch after retry");
    },
    restartBackend: async () => ({
      state: "started",
      message: "backend restarted",
      url: "http://127.0.0.1:5519",
      startedByApp: true,
    }),
  }));
  assert.equal(desktopController.getState().error, "Failed to fetch after retry");
});
