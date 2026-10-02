import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalTimelineRestoreIdempotencyKey,
  fetchCanonicalTimelineWorkspace,
  restoreCanonicalTimelineRevision,
} from "../src/blueprint/timelineApi.ts";
import {
  isHistoricalTimelineWorkspaceForCurrentHead,
  isHistoricalTimelineSelectionForCurrentLedger,
  normalizeCanonicalTimelineRestoreCommandResult,
  normalizeCanonicalTimelineWorkspace,
} from "../src/blueprint/timelineModel.ts";
import {
  historicalTimelineRevisionNumbers,
  stageCanonicalTimelineRestore,
} from "../src/blueprint/timelineRestoreModel.ts";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);
const SHA_E = "e".repeat(64);

function bindings() {
  return {
    blueprint: {
      revision: 2,
      content_sha256: SHA_A,
      semantic_sha256: SHA_B,
      operation_id: "blueprint_op_2",
    },
    coverage: {
      revision: 4,
      content_sha256: SHA_C,
      evidence_manifest_sha256: SHA_D,
      operation_id: "coverage_op_4",
    },
  };
}

function head(revision, suffix, binding) {
  return {
    revision,
    revision_sha256: suffix === "3" ? SHA_E : SHA_D,
    timeline_id: `timeline_${"1".repeat(24)}`,
    timeline_content_sha256: suffix === "3" ? SHA_C : SHA_B,
    blueprint_binding: structuredClone(binding.blueprint),
    coverage_binding: structuredClone(binding.coverage),
    operation_id: `timeline_op_${suffix}`,
  };
}

function workspacePayload({ historical = false, schemaVersion = "1" } = {}) {
  const binding = bindings();
  const currentHead = head(3, "3", binding);
  const selected = historical ? head(1, "1", binding) : structuredClone(currentHead);
  const assetId = `asset_${"a".repeat(24)}`;
  const clip = {
    clip_id: `clip_${"2".repeat(24)}`,
    ordinal: 0,
    beat_id: `beat_${"3".repeat(24)}`,
    assignment_id: `assign_${"4".repeat(24)}`,
    evidence_ref: `memolens://evidence/asset/${assetId}`,
    asset_id: assetId,
    asset_sha256: SHA_A,
    media_kind: "image",
    start_ms: 0,
    end_ms: historical ? 1_000 : 1_500,
    fit: "cover",
    audio_enabled: false,
  };
  return {
    object: "canonical_timeline.workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    project_id: "project_restore",
    head: currentHead,
    selected_revision: { ...selected, is_head: !historical },
    freshness: { state: "current", reasons: [] },
    timeline: {
      object: "memolens.canonical_timeline",
      schema_version: schemaVersion,
      timeline_id: currentHead.timeline_id,
      project_id: "project_restore",
      revision: selected.revision,
      parent: historical ? null : { revision: 2, content_sha256: SHA_A },
      blueprint_binding: structuredClone(binding.blueprint),
      coverage_binding: structuredClone(binding.coverage),
      compiler: {
        id: "memolens.timeline-lowerer/v1",
        media: "image_and_video_span",
        transitions: "hard_cut",
        audio: "silent",
        subtitles: "none",
      },
      output: { duration_ms: clip.end_ms, aspect_ratio: "9:16" },
      tracks: [{
        track_id: `track_${"5".repeat(24)}`,
        kind: "primary_visual",
        clips: [clip],
      }],
    },
    source_bindings: [{
      clip_id: clip.clip_id,
      evidence_ref: clip.evidence_ref,
      asset_id: clip.asset_id,
      asset_sha256: clip.asset_sha256,
      asset_source_id: `src_${"6".repeat(24)}`,
      coverage_proof_sha256: SHA_E,
      media_kind: "image",
    }],
    lifecycle: { state: "draft", approval: "not_established", exportable: false },
  };
}

function restoreResult(current, restoreFrom) {
  const resultHead = structuredClone(current.head);
  resultHead.revision = current.head.revision + 1;
  resultHead.revision_sha256 = SHA_A;
  resultHead.timeline_content_sha256 = SHA_D;
  resultHead.operation_id = "timeline_restore_op_4";
  return {
    object: "canonical_timeline.command_result",
    schema_version: "1",
    project_id: current.project_id,
    command_type: "timeline.restore_revision",
    operation_id: resultHead.operation_id,
    result: {
      kind: "revision_restored",
      result_head: resultHead,
      restore_from: restoreFrom,
    },
  };
}

test("historical selection stays separate from current authority and stages a noncanonical restore", () => {
  const current = normalizeCanonicalTimelineWorkspace(workspacePayload());
  const historical = normalizeCanonicalTimelineWorkspace(
    workspacePayload({ historical: true }),
  );
  const frozenCurrent = structuredClone(current);
  const frozenHistorical = structuredClone(historical);

  assert.equal(isHistoricalTimelineWorkspaceForCurrentHead(current, historical), true);
  assert.deepEqual(historicalTimelineRevisionNumbers(current.head), [2, 1]);
  const pending = stageCanonicalTimelineRestore({ current, historical });
  assert.equal(pending.canonical, false);
  assert.equal(pending.persisted, false);
  assert.equal(pending.based_on_head.revision, 3);
  assert.equal(pending.restore_from.revision, 1);
  assert.equal("timeline" in pending, false);
  assert.deepEqual(current, frozenCurrent);
  assert.deepEqual(historical, frozenHistorical);

  const wrongHead = structuredClone(historical);
  wrongHead.head.timeline_content_sha256 = SHA_A;
  assert.equal(isHistoricalTimelineWorkspaceForCurrentHead(current, wrongHead), false);
  assert.throws(
    () => stageCanonicalTimelineRestore({ current, historical: wrongHead }),
    /does not belong to the exact current head/,
  );
});

test("mixed v2 current and v1 history remain readable and restore-staging compatible", () => {
  const current = normalizeCanonicalTimelineWorkspace(
    workspacePayload({ schemaVersion: "2" }),
  );
  const historical = normalizeCanonicalTimelineWorkspace(
    workspacePayload({ historical: true, schemaVersion: "1" }),
  );

  assert.equal(current.timeline.schema_version, "2");
  assert.equal(historical.timeline.schema_version, "1");
  assert.equal(isHistoricalTimelineSelectionForCurrentLedger(current, historical), true);
  assert.equal(isHistoricalTimelineWorkspaceForCurrentHead(current, historical), true);
  const pending = stageCanonicalTimelineRestore({ current, historical });
  assert.equal(pending.restore_from.revision, 1);
  assert.equal("schema_version" in pending.restore_from, false);
});

test("read-only history inspection is broader than restore eligibility", () => {
  const current = normalizeCanonicalTimelineWorkspace(workspacePayload());
  const historicalPayload = workspacePayload({ historical: true });
  historicalPayload.freshness = {
    state: "stale_blueprint",
    reasons: ["blueprint_head_changed"],
  };
  historicalPayload.selected_revision.blueprint_binding.revision = 1;
  historicalPayload.timeline.blueprint_binding.revision = 1;
  const historical = normalizeCanonicalTimelineWorkspace(historicalPayload);

  assert.equal(
    isHistoricalTimelineSelectionForCurrentLedger(current, historical),
    true,
  );
  assert.equal(
    isHistoricalTimelineWorkspaceForCurrentHead(current, historical),
    false,
  );
  assert.throws(
    () => stageCanonicalTimelineRestore({ current, historical }),
    /does not belong to the exact current head/,
  );
});

test("restore result normalizer is closed and binds K to the N+1 result", () => {
  const current = normalizeCanonicalTimelineWorkspace(workspacePayload());
  const historical = normalizeCanonicalTimelineWorkspace(
    workspacePayload({ historical: true }),
  );
  const pending = stageCanonicalTimelineRestore({ current, historical });
  const payload = restoreResult(current, pending.restore_from);
  const normalized = normalizeCanonicalTimelineRestoreCommandResult(payload);
  assert.equal(normalized.result.kind, "revision_restored");
  assert.equal(normalized.result.result_head.revision, 4);
  assert.equal(normalized.result.restore_from.revision, 1);

  for (const mutate of [
    (value) => { value.result.extra = true; },
    (value) => { value.result.kind = "revision_created"; },
    (value) => { value.result.restore_from.timeline_id = "timeline_other"; },
    (value) => { value.result.restore_from.revision = 3; },
    (value) => { value.result.restore_from.revision = 4; },
  ]) {
    const rejected = structuredClone(payload);
    mutate(rejected);
    assert.throws(
      () => normalizeCanonicalTimelineRestoreCommandResult(rejected),
      /timeline_restore_command/,
    );
  }
});

test("restore API sends only exact N and K identities with a stable bounded key", async () => {
  const currentPayload = workspacePayload();
  const historicalPayload = workspacePayload({ historical: true });
  const current = normalizeCanonicalTimelineWorkspace(currentPayload);
  const historical = normalizeCanonicalTimelineWorkspace(historicalPayload);
  const pending = stageCanonicalTimelineRestore({ current, historical });
  const command = restoreResult(current, pending.restore_from);
  const requests = [];
  const responses = [historicalPayload, command];
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  globalThis.window = globalThis;
  globalThis.setTimeout = () => 1;
  globalThis.clearTimeout = () => {};
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url: String(url), init });
    return new Response(JSON.stringify(responses.shift()), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const selected = await fetchCanonicalTimelineWorkspace({
      apiBase: "http://127.0.0.1:5519",
      projectId: current.project_id,
      dbPath: "/db/library.sqlite",
      revision: 1,
    });
    assert.equal(selected.selected_revision.is_head, false);
    const result = await restoreCanonicalTimelineRevision({
      apiBase: "http://127.0.0.1:5519",
      projectId: current.project_id,
      dbPath: "/db/library.sqlite",
      expectedDatabaseUuid: current.database_uuid,
      expectedBlueprint: current.head.blueprint_binding,
      expectedCoverage: current.head.coverage_binding,
      expectedTimelineHead: current.head,
      restoreFrom: pending.restore_from,
    });
    assert.equal(result.command_type, "timeline.restore_revision");
    assert.equal(
      requests[0].url,
      "http://127.0.0.1:5519/v1/creative/projects/project_restore/timeline?db_path=%2Fdb%2Flibrary.sqlite&revision=1",
    );
    assert.equal(
      requests[1].url,
      `http://127.0.0.1:5519/v1/creative/projects/project_restore/timeline/restore?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${current.database_uuid}`,
    );
    assert.deepEqual(JSON.parse(requests[1].init.body), {
      expected_blueprint: current.head.blueprint_binding,
      expected_coverage: current.head.coverage_binding,
      expected_timeline_head: current.head,
      restore_from: pending.restore_from,
    });
    const expectedKey = await canonicalTimelineRestoreIdempotencyKey(
      current.database_uuid,
      current.head,
      pending.restore_from,
    );
    assert.equal(requests[1].init.headers["Idempotency-Key"], expectedKey);
    assert.match(expectedKey, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/);
    const reorderedHead = {
      operation_id: current.head.operation_id,
      coverage_binding: {
        operation_id: current.head.coverage_binding.operation_id,
        evidence_manifest_sha256: current.head.coverage_binding.evidence_manifest_sha256,
        content_sha256: current.head.coverage_binding.content_sha256,
        revision: current.head.coverage_binding.revision,
      },
      blueprint_binding: {
        operation_id: current.head.blueprint_binding.operation_id,
        semantic_sha256: current.head.blueprint_binding.semantic_sha256,
        content_sha256: current.head.blueprint_binding.content_sha256,
        revision: current.head.blueprint_binding.revision,
      },
      timeline_content_sha256: current.head.timeline_content_sha256,
      timeline_id: current.head.timeline_id,
      revision_sha256: current.head.revision_sha256,
      revision: current.head.revision,
    };
    assert.equal(
      await canonicalTimelineRestoreIdempotencyKey(
        current.database_uuid,
        reorderedHead,
        pending.restore_from,
      ),
      expectedKey,
    );
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.window = originalWindow;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});
