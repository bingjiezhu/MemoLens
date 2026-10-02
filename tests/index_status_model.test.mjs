import assert from "node:assert/strict";
import test from "node:test";

import { resolveIndexReadiness } from "../src/query/types.ts";

const baseStats = {
  total_records: 12,
  fallback_records: 0,
  fallback_ratio: 0,
  needs_reindex: false,
};

test("managed index requires both ready status and the canonical projection gate", () => {
  assert.equal(resolveIndexReadiness({
    ...baseStats,
    projection_status: "ready",
    projection_ready: true,
    projection_reason_code: null,
    projection_generation_id: "generation-17",
  }).ready, true);

  assert.equal(resolveIndexReadiness({
    ...baseStats,
    projection_status: "ready",
    projection_ready: false,
    projection_reason_code: "image_projection_manifest_dirty",
    projection_generation_id: "generation-17",
  }).ready, false);

  assert.equal(resolveIndexReadiness({
    ...baseStats,
    projection_status: "unavailable",
    projection_ready: true,
    projection_reason_code: "image_projection_manifest_dirty",
    projection_generation_id: null,
  }).ready, false);
});

test("old managed-shaped payload fails closed even when it reports records", () => {
  const readiness = resolveIndexReadiness(baseStats);
  assert.equal(readiness.mode, "unknown");
  assert.equal(readiness.ready, false);
  assert.equal(readiness.recordCount, 12);
});

test("explicit legacy mode retains count-based compatibility", () => {
  const readiness = resolveIndexReadiness({
    ...baseStats,
    projection_status: "legacy",
    projection_ready: false,
    projection_reason_code: null,
    projection_generation_id: null,
  });
  assert.equal(readiness.mode, "legacy");
  assert.equal(readiness.ready, true);
  assert.equal(readiness.recordCount, 12);
});

test("an empty canonical projection is not retrieval-ready", () => {
  const readiness = resolveIndexReadiness({
    ...baseStats,
    total_records: 0,
    projection_status: "ready",
    projection_ready: true,
    projection_reason_code: null,
    projection_generation_id: "generation-empty",
  });
  assert.equal(readiness.mode, "ready");
  assert.equal(readiness.ready, false);
  assert.equal(readiness.recordCount, 0);
});
