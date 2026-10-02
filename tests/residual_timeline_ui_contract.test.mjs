import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { normalizeCanonicalTimelineWorkspace } from "../src/blueprint/timelineModel.ts";
import { buildTimelinePreviewMediaItems } from "../src/blueprint/timelinePreviewModel.ts";

const ASSET_SHA = "a".repeat(64);
const ASSET_ID = `asset_${ASSET_SHA.slice(0, 24)}`;
const SOURCE_ID = `src_${"b".repeat(24)}`;
const PARENT_ID = `seg_${ASSET_SHA.slice(0, 24)}_1_0`;
const RESIDUAL_ID = `rseg_${"c".repeat(64)}`;
const ANALYSIS_RUN_ID = `arun_${"d".repeat(32)}`;

function residualBinding() {
  return {
    object: "memolens.residual_binding",
    contract_version: "1",
    residual_id: RESIDUAL_ID,
    parent_segment_id: PARENT_ID,
    asset_id: ASSET_ID,
    asset_sha256: ASSET_SHA,
    asset_source_id: SOURCE_ID,
    source_binding_sha256: "e".repeat(64),
    analysis_run_id: ANALYSIS_RUN_ID,
    analysis_revision: 1,
    input_asset_sha256: ASSET_SHA,
    parent_start_ms: 0,
    parent_end_ms: 60_000,
    source_in_ms: 1_000,
    source_out_ms: 10_000,
    usage_revision: "f".repeat(64),
    parent_usage_projection_sha256: "1".repeat(64),
  };
}

function fixture() {
  const blueprintBinding = {
    revision: 1,
    content_sha256: "2".repeat(64),
    semantic_sha256: "3".repeat(64),
    operation_id: "operation_blueprint",
  };
  const coverageBinding = {
    revision: 1,
    content_sha256: "4".repeat(64),
    evidence_manifest_sha256: "5".repeat(64),
    operation_id: "operation_coverage",
  };
  const clip = {
    clip_id: `clip_${"6".repeat(24)}`,
    ordinal: 0,
    beat_id: "beat_residual",
    assignment_id: "assignment_residual",
    evidence_ref: `memolens://evidence/span/${RESIDUAL_ID}`,
    asset_id: ASSET_ID,
    asset_sha256: ASSET_SHA,
    media_kind: "video",
    start_ms: 0,
    end_ms: 1_000,
    fit: "cover",
    audio_enabled: false,
    span_id: PARENT_ID,
    analysis_run_id: ANALYSIS_RUN_ID,
    analysis_revision: 1,
    input_asset_sha256: ASSET_SHA,
    source_in_ms: 2_000,
    source_out_ms: 3_000,
    residual_id: RESIDUAL_ID,
    parent_segment_id: PARENT_ID,
    residual_binding: residualBinding(),
  };
  const head = {
    revision: 1,
    revision_sha256: "7".repeat(64),
    timeline_id: "timeline_residual",
    timeline_content_sha256: "8".repeat(64),
    blueprint_binding: blueprintBinding,
    coverage_binding: coverageBinding,
    operation_id: "operation_timeline",
  };
  return {
    object: "canonical_timeline.workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    project_id: "project_residual",
    head,
    selected_revision: { ...head, is_head: true },
    freshness: { state: "current", reasons: [] },
    timeline: {
      object: "memolens.canonical_timeline",
      schema_version: "2",
      timeline_id: "timeline_residual",
      project_id: "project_residual",
      revision: 1,
      parent: null,
      blueprint_binding: blueprintBinding,
      coverage_binding: coverageBinding,
      compiler: {
        id: "memolens.timeline-lowerer/v1",
        media: "image_and_video_span",
        transitions: "hard_cut",
        audio: "silent",
        subtitles: "none",
      },
      output: { duration_ms: 1_000, aspect_ratio: "9:16" },
      tracks: [{
        track_id: `track_${"9".repeat(24)}`,
        kind: "primary_visual",
        clips: [clip],
      }],
    },
    source_bindings: [{
      clip_id: clip.clip_id,
      evidence_ref: clip.evidence_ref,
      asset_id: ASSET_ID,
      asset_sha256: ASSET_SHA,
      asset_source_id: SOURCE_ID,
      coverage_proof_sha256: "a".repeat(64),
      media_kind: "video",
      span_id: PARENT_ID,
      analysis_run_id: ANALYSIS_RUN_ID,
      analysis_revision: 1,
      input_asset_sha256: ASSET_SHA,
      source_in_ms: clip.source_in_ms,
      source_out_ms: clip.source_out_ms,
      residual_id: RESIDUAL_ID,
      parent_segment_id: PARENT_ID,
      residual_binding: residualBinding(),
    }],
    lifecycle: { state: "draft", approval: "not_established", exportable: false },
  };
}

test("canonical Timeline preserves a residual proof while allowing a trimmed child interval", () => {
  const normalized = normalizeCanonicalTimelineWorkspace(fixture());
  const clip = normalized.timeline.tracks[0].clips[0];
  const source = normalized.source_bindings[0];

  assert.equal(clip.residual_id, RESIDUAL_ID);
  assert.equal(clip.parent_segment_id, PARENT_ID);
  assert.deepEqual(clip.residual_binding, residualBinding());
  assert.deepEqual(
    [clip.source_in_ms, clip.source_out_ms],
    [2_000, 3_000],
  );
  assert.equal(source.residual_id, RESIDUAL_ID);
  assert.deepEqual(source.residual_binding, residualBinding());

  const items = buildTimelinePreviewMediaItems("http://127.0.0.1:43110", normalized);
  assert.equal(items.length, 1);
  assert.equal(
    items[0].thumbnailUrl,
    `http://127.0.0.1:43110/v1/video-segments/${PARENT_ID}/thumbnail`,
  );
  assert.equal(items[0].thumbnailUrl.includes(RESIDUAL_ID), false);
});

test("canonical Timeline rejects residual downgrade, substitution, or range expansion", () => {
  const mutations = [
    (value) => { delete value.timeline.tracks[0].clips[0].residual_binding; },
    (value) => {
      value.timeline.tracks[0].clips[0].evidence_ref =
        `memolens://evidence/span/${PARENT_ID}`;
    },
    (value) => { value.timeline.tracks[0].clips[0].source_out_ms = 10_001; },
    (value) => { value.source_bindings[0].residual_binding.usage_revision = "0".repeat(64); },
    (value) => { value.source_bindings[0].asset_source_id = `src_${"0".repeat(24)}`; },
  ];
  for (const mutate of mutations) {
    const raw = fixture();
    mutate(raw);
    assert.throws(() => normalizeCanonicalTimelineWorkspace(raw));
  }

  const normalized = normalizeCanonicalTimelineWorkspace(fixture());
  const downgraded = structuredClone(normalized);
  delete downgraded.source_bindings[0].residual_id;
  delete downgraded.source_bindings[0].parent_segment_id;
  delete downgraded.source_bindings[0].residual_binding;
  assert.deepEqual(
    buildTimelinePreviewMediaItems("http://127.0.0.1:43110", downgraded),
    [],
  );
});

test("visible Timeline UI names exact residual lineage beside real edit controls", () => {
  const source = readFileSync(
    new URL("../src/blueprint/BlueprintProjectWorkspace.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /Exact residual/);
  assert.match(source, /parent <code>\{clip\.parent_segment_id\}<\/code>/);
  assert.match(source, /frozen residual bound/);
  assert.match(source, />\s*Split clip\s*<\/button>/);
  assert.match(source, />\s*Delete clip\s*<\/button>/);
});
