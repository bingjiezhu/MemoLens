import assert from "node:assert/strict";
import test from "node:test";

import {
  createVideoScopeKey,
  persistVideoSession,
  readPersistedVideoSession,
  videoSessionStorageKey,
} from "../src/video/session.ts";


class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }
}

test("video scope keys are canonical and library-specific", () => {
  const first = createVideoScopeKey(" /photos ", " /state/a.db ");
  const equivalent = createVideoScopeKey("/photos", "/state/a.db");
  const other = createVideoScopeKey("/photos", "/state/b.db");

  assert.equal(first, equivalent);
  assert.equal(videoSessionStorageKey(first), videoSessionStorageKey(equivalent));
  assert.notEqual(videoSessionStorageKey(first), videoSessionStorageKey(other));
});

test("persisted video sessions round-trip only supported fields", () => {
  const storage = new MemoryStorage();
  const scope = createVideoScopeKey("/photos", "/state/index.db");
  const session = {
    projectId: "project-1",
    timelineId: "timeline-1",
    timelineRevision: 3,
  };

  persistVideoSession(storage, scope, session);

  assert.deepEqual(readPersistedVideoSession(storage, scope), session);
});

test("empty, corrupt, and malformed sessions fail closed", () => {
  const storage = new MemoryStorage();
  const emptyScope = createVideoScopeKey(null, null);
  assert.equal(readPersistedVideoSession(storage, emptyScope), null);

  const scope = createVideoScopeKey("/photos", "/state/index.db");
  storage.setItem(videoSessionStorageKey(scope), "not-json");
  assert.equal(readPersistedVideoSession(storage, scope), null);

  storage.setItem(videoSessionStorageKey(scope), JSON.stringify({ timelineId: "missing-project" }));
  assert.equal(readPersistedVideoSession(storage, scope), null);
});

test("storage failures never interrupt the SQLite-backed workflow", () => {
  const storage = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("blocked");
    },
  };
  const scope = createVideoScopeKey("/photos", "/state/index.db");

  assert.doesNotThrow(() => persistVideoSession(storage, scope, {
    projectId: "project-1",
    timelineId: null,
    timelineRevision: null,
  }));
  assert.equal(readPersistedVideoSession(storage, scope), null);
});
