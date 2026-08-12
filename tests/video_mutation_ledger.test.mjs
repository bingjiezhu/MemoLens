import assert from "node:assert/strict";
import test from "node:test";

import {
  VIDEO_MUTATION_LEDGER_STORAGE_KEY,
  VideoMutationLedger,
  canonicalizeMutationIdentity,
  isAmbiguousVideoMutationOutcome,
  shouldRetainVideoMutation,
  shouldReconcileTimelineMutation,
  videoMutationOutcomeFromError,
} from "../src/video/mutationLedger.ts";


class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

function keySequence(prefix = "idem") {
  let current = 0;
  const factory = () => `${prefix}-${++current}`;
  factory.count = () => current;
  return factory;
}

function identity(payload, overrides = {}) {
  return {
    scope: "desktop:POST:/v1/timelines/:id/revise",
    action: "timeline.revise",
    payload,
    ...overrides,
  };
}

test("canonical identity and repeated payload reuse exactly one persisted key", async () => {
  const storage = new MemoryStorage();
  const keys = keySequence();
  let timestamp = 1_000;
  const ledger = new VideoMutationLedger(storage, {
    keyFactory: keys,
    now: () => timestamp++,
  });

  const first = await ledger.acquire(identity({ apply: true, operations: [{ volume_db: -3, op: "set_volume" }] }));
  const second = await ledger.acquire(identity({ operations: [{ op: "set_volume", volume_db: -3 }], apply: true }));

  assert.equal(first.idempotencyKey, "idem-1");
  assert.equal(second.idempotencyKey, first.idempotencyKey);
  assert.equal(second.identityHash, first.identityHash);
  assert.equal(second.attemptCount, 2);
  assert.equal(second.createdAtMs, first.createdAtMs);
  assert.ok(second.lastAttemptAtMs > first.lastAttemptAtMs);
  assert.equal(keys.count(), 1);
  assert.equal(
    canonicalizeMutationIdentity(identity({ z: 1, a: { y: 2, x: 3 } })),
    canonicalizeMutationIdentity(identity({ a: { x: 3, y: 2 }, z: 1 })),
  );
});

test("payload, scope, or action changes create a different mutation identity", async () => {
  const storage = new MemoryStorage();
  const ledger = new VideoMutationLedger(storage, { keyFactory: keySequence("changed") });

  const base = await ledger.acquire(identity({ base_revision: 1 }));
  const payloadChanged = await ledger.acquire(identity({ base_revision: 2 }));
  const scopeChanged = await ledger.acquire(identity({ base_revision: 1 }, { scope: "desktop:POST:/v1/renders" }));
  const actionChanged = await ledger.acquire(identity({ base_revision: 1 }, { action: "render.start" }));

  assert.equal(new Set([
    base.identityHash,
    payloadChanged.identityHash,
    scopeChanged.identityHash,
    actionChanged.identityHash,
  ]).size, 4);
  assert.equal(new Set([
    base.idempotencyKey,
    payloadChanged.idempotencyKey,
    scopeChanged.idempotencyKey,
    actionChanged.idempotencyKey,
  ]).size, 4);
});

test("a new ledger instance recovers the in-flight key after reload", async () => {
  const storage = new MemoryStorage();
  const firstLedger = new VideoMutationLedger(storage, { keyFactory: keySequence("before-reload") });
  const first = await firstLedger.acquire(identity({ timeline_revision: 7 }));

  const reloadedLedger = new VideoMutationLedger(storage, {
    keyFactory: () => {
      throw new Error("reload must not create another key");
    },
  });
  const peeked = await reloadedLedger.peek(identity({ timeline_revision: 7 }));
  const retried = await reloadedLedger.acquire(identity({ timeline_revision: 7 }));

  assert.equal(peeked?.idempotencyKey, first.idempotencyKey);
  assert.equal(peeked?.attemptCount, 1);
  assert.equal(retried.idempotencyKey, first.idempotencyKey);
  assert.equal(retried.attemptCount, 2);
});

test("only success and explicit nonretryable 4xx clear a mutation", async (t) => {
  const retainedOutcomes = [
    { kind: "timeout" },
    { kind: "abort" },
    { kind: "network_error" },
    { kind: "request_in_progress" },
    { kind: "http_error", status: 503, code: "backend_busy", retryable: false },
    { kind: "http_error", status: 409, code: "revision_conflict", retryable: false },
    { kind: "http_error", status: 202, code: "request_in_progress", retryable: false },
    { kind: "http_error", status: 400, code: "unknown_retryability" },
    { kind: "http_error", status: 429, code: "rate_limited", retryable: true },
  ];
  for (const [index, outcome] of retainedOutcomes.entries()) {
    await t.test(`retains ${outcome.kind}-${index}`, async () => {
      const storage = new MemoryStorage();
      const ledger = new VideoMutationLedger(storage, { keyFactory: keySequence(`retain-${index}`) });
      const mutationIdentity = identity({ case: index });
      const lease = await ledger.acquire(mutationIdentity);
      assert.equal(ledger.settle(lease, outcome), "retained");
      assert.equal((await ledger.peek(mutationIdentity))?.idempotencyKey, lease.idempotencyKey);
    });
  }

  const clearedOutcomes = [
    { kind: "success" },
    { kind: "http_error", status: 400, code: "invalid_request", retryable: false },
    { kind: "http_error", status: 404, code: "timeline_not_found", retryable: false },
  ];
  for (const [index, outcome] of clearedOutcomes.entries()) {
    await t.test(`clears ${outcome.kind}-${index}`, async () => {
      const storage = new MemoryStorage();
      const ledger = new VideoMutationLedger(storage, { keyFactory: keySequence(`clear-${index}`) });
      const mutationIdentity = identity({ case: index });
      const lease = await ledger.acquire(mutationIdentity);
      assert.equal(ledger.settle(lease, outcome), "cleared");
      assert.equal(await ledger.peek(mutationIdentity), null);
      assert.equal(storage.getItem(VIDEO_MUTATION_LEDGER_STORAGE_KEY), null);
    });
  }
});

test("late settlement from an old lease cannot clear a newer request", async () => {
  const storage = new MemoryStorage();
  const ledger = new VideoMutationLedger(storage, { keyFactory: keySequence("generation") });
  const mutationIdentity = identity({ timeline_revision: 3 });
  const oldLease = await ledger.acquire(mutationIdentity);
  assert.equal(ledger.settle(oldLease, { kind: "success" }), "cleared");
  const newLease = await ledger.acquire(mutationIdentity);

  assert.notEqual(newLease.idempotencyKey, oldLease.idempotencyKey);
  assert.equal(ledger.settle(oldLease, { kind: "success" }), "stale");
  assert.equal((await ledger.peek(mutationIdentity))?.idempotencyKey, newLease.idempotencyKey);
});

test("persistent storage contains no payload, path, scope, or action text", async () => {
  const storage = new MemoryStorage();
  const ledger = new VideoMutationLedger(storage, { keyFactory: () => "opaque-idempotency-key" });
  const sensitive = {
    absolute_path: "/Users/alice/Secret Videos/private-client-cut.mp4",
    export_token: "do-not-persist-this-token",
    instruction: "Use the confidential acquisition footage",
  };
  await ledger.acquire(identity(sensitive, {
    scope: "desktop:POST:/private/project/revise",
    action: "secret.timeline.revise",
  }));

  const raw = storage.getItem(VIDEO_MUTATION_LEDGER_STORAGE_KEY);
  assert.ok(raw);
  for (const secret of [
    sensitive.absolute_path,
    sensitive.export_token,
    sensitive.instruction,
    "absolute_path",
    "export_token",
    "instruction",
    "private/project",
    "secret.timeline.revise",
  ]) {
    assert.equal(raw.includes(secret), false, `ledger leaked ${secret}`);
  }
  const parsed = JSON.parse(raw);
  assert.deepEqual(Object.keys(parsed), ["version", "entries"]);
  const [identityHash] = Object.keys(parsed.entries);
  assert.match(identityHash, /^[0-9a-f]{64}$/);
  assert.deepEqual(Object.keys(parsed.entries[identityHash]).sort(), [
    "attempt_count",
    "created_at_ms",
    "idempotency_key",
    "last_attempt_at_ms",
  ]);
});

test("retention classifier preserves revision reconciliation and rejects malformed payloads", () => {
  assert.equal(
    shouldRetainVideoMutation({
      kind: "http_error",
      status: 409,
      code: "revision_conflict",
      retryable: false,
    }),
    true,
  );
  assert.equal(
    shouldRetainVideoMutation({
      kind: "http_error",
      status: 422,
      code: "invalid_timeline",
      retryable: false,
    }),
    false,
  );
  assert.throws(
    () => canonicalizeMutationIdentity(identity({ invalid: Number.NaN })),
    /finite/,
  );
  const cyclic = {};
  cyclic.self = cyclic;
  assert.throws(
    () => canonicalizeMutationIdentity(identity(cyclic)),
    /cycles/,
  );
});

test("transport errors classify conservatively and only targeted timeline failures reconcile", () => {
  assert.deepEqual(
    videoMutationOutcomeFromError({ status: 422, code: "invalid_timeline" }),
    { kind: "http_error", status: 422, code: "invalid_timeline" },
  );
  assert.deepEqual(
    videoMutationOutcomeFromError({ status: 409, code: "request_in_progress", retryable: true }),
    { kind: "request_in_progress" },
  );
  assert.deepEqual(
    videoMutationOutcomeFromError(new DOMException("deadline", "TimeoutError")),
    { kind: "timeout" },
  );
  assert.deepEqual(
    videoMutationOutcomeFromError(new TypeError("fetch failed")),
    { kind: "network_error" },
  );

  assert.equal(isAmbiguousVideoMutationOutcome({ kind: "http_error", status: 503 }), true);
  assert.equal(isAmbiguousVideoMutationOutcome({ kind: "http_error", status: 422, retryable: false }), false);
  assert.equal(shouldReconcileTimelineMutation({ kind: "timeout" }), true);
  assert.equal(shouldReconcileTimelineMutation({
    kind: "http_error",
    status: 409,
    code: "revision_conflict",
    retryable: false,
  }), true);
  assert.equal(shouldReconcileTimelineMutation({ kind: "network_error" }), false);
});
