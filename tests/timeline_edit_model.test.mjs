import assert from "node:assert/strict";
import test from "node:test";

import {
  canonicalTimelinePendingEditKey,
  canonicalTimelineReplacementCandidates,
  stageCanonicalTimelineEdit,
} from "../src/blueprint/timelineEditModel.ts";
import { normalizeCanonicalTimelineWorkspace } from "../src/blueprint/timelineModel.ts";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);

function fixture() {
  const blueprintBinding = {
    revision: 2,
    content_sha256: SHA_A,
    semantic_sha256: SHA_B,
    operation_id: "operation_blueprint",
  };
  const coverageBinding = {
    revision: 4,
    content_sha256: SHA_B,
    evidence_manifest_sha256: SHA_C,
    operation_id: "operation_coverage",
  };
  const imageClip = {
    clip_id: "clip_image",
    ordinal: 0,
    beat_id: "beat_image",
    assignment_id: "assign_image",
    evidence_ref: "memolens://evidence/asset/img_current",
    asset_id: "img_current",
    asset_sha256: SHA_A,
    media_kind: "image",
    start_ms: 0,
    end_ms: 1_000,
    fit: "cover",
    audio_enabled: false,
  };
  const videoClip = {
    clip_id: "clip_video",
    ordinal: 1,
    beat_id: "beat_video",
    assignment_id: "assign_video",
    evidence_ref: "memolens://evidence/span/span_current",
    asset_id: "asset_current",
    asset_sha256: SHA_B,
    media_kind: "video",
    start_ms: 1_000,
    end_ms: 2_500,
    fit: "cover",
    audio_enabled: false,
    span_id: "span_current",
    analysis_run_id: "analysis_current",
    analysis_revision: 1,
    input_asset_sha256: SHA_B,
    source_in_ms: 5_000,
    source_out_ms: 6_500,
  };
  const head = {
    revision: 3,
    revision_sha256: SHA_A,
    timeline_id: "timeline_1",
    timeline_content_sha256: SHA_C,
    blueprint_binding: blueprintBinding,
    coverage_binding: coverageBinding,
    operation_id: "operation_timeline",
  };
  const workspace = {
    object: "canonical_timeline.workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    project_id: "project_1",
    head,
    selected_revision: { ...head, is_head: true },
    freshness: { state: "current", reasons: [] },
    timeline: {
      object: "memolens.canonical_timeline",
      schema_version: "1",
      timeline_id: "timeline_1",
      project_id: "project_1",
      revision: 3,
      parent: { revision: 2, content_sha256: SHA_B },
      blueprint_binding: blueprintBinding,
      coverage_binding: coverageBinding,
      compiler: {},
      output: { duration_ms: 2_500, aspect_ratio: "9:16" },
      tracks: [{ track_id: "track_1", kind: "primary_visual", clips: [imageClip, videoClip] }],
    },
    source_bindings: [
      {
        ...imageClip,
        asset_source_id: "source_image",
        coverage_proof_sha256: SHA_A,
      },
      {
        ...videoClip,
        asset_source_id: "source_video",
        coverage_proof_sha256: SHA_B,
      },
    ],
    lifecycle: { state: "draft", approval: "not_established", exportable: false },
  };
  const coveragePlan = {
    object: "memolens.coverage_plan",
    schema_version: "1",
    project_id: "project_1",
    revision: 4,
    parent: null,
    blueprint_binding: {
      revision: 2,
      content_sha256: SHA_A,
      semantic_sha256: SHA_B,
    },
    compiler: {},
    output_binding: { duration_target_ms: 2_500, aspect_ratio: "9:16" },
    beats: [
      {
        beat_id: "beat_image",
        script_block_id: "block_image",
        ordinal: 0,
        text_sha256: SHA_A,
        timing: { basis: "estimated_text", start_ms: 0, end_ms: 1_000 },
        needs: [],
        selected_assignments: [],
        alternatives: [],
        gap: null,
      },
      {
        beat_id: "beat_video",
        script_block_id: "block_video",
        ordinal: 1,
        text_sha256: SHA_B,
        timing: { basis: "estimated_text", start_ms: 1_000, end_ms: 2_500 },
        needs: [],
        selected_assignments: [],
        alternatives: [
          {
            assignment_id: "assign_replacement",
            need_id: "need_video",
            hint_id: "hint_replacement",
            evidence_ref: "memolens://evidence/span/span_replacement",
            match_type: "depiction_unspecified",
            timeline_duration_ms: 2_000,
            reason_sha256: SHA_C,
            locked: false,
            not_selected_reason: "baseline_first_verified_selected",
          },
          {
            assignment_id: "assign_short",
            need_id: "need_video",
            hint_id: "hint_short",
            evidence_ref: "memolens://evidence/span/span_short",
            match_type: "depiction_unspecified",
            timeline_duration_ms: 1_500,
            reason_sha256: SHA_A,
            locked: false,
            not_selected_reason: "evidence_span_too_short",
          },
        ],
        gap: null,
      },
    ],
    evidence_manifest: [
      {
        evidence_ref: imageClip.evidence_ref,
        status: "verified",
        proof_sha256: SHA_A,
        proof: { kind: "asset", asset_id: imageClip.asset_id, asset_sha256: SHA_A },
      },
      {
        evidence_ref: videoClip.evidence_ref,
        status: "verified",
        proof_sha256: SHA_B,
        proof: {
          kind: "span",
          span_id: "span_current",
          asset_id: videoClip.asset_id,
          asset_sha256: SHA_B,
          analysis_run_id: "analysis_current",
          analysis_revision: 1,
          input_asset_sha256: SHA_B,
          start_ms: 4_000,
          end_ms: 8_000,
        },
      },
      {
        evidence_ref: "memolens://evidence/span/span_replacement",
        status: "verified",
        proof_sha256: SHA_C,
        proof: {
          kind: "span",
          span_id: "span_replacement",
          asset_id: "asset_replacement",
          asset_sha256: SHA_C,
          analysis_run_id: "analysis_replacement",
          analysis_revision: 1,
          input_asset_sha256: SHA_C,
          start_ms: 10_000,
          end_ms: 12_500,
        },
      },
      {
        evidence_ref: "memolens://evidence/span/span_short",
        status: "verified",
        proof_sha256: SHA_A,
        proof: {
          kind: "span",
          span_id: "span_short",
          asset_id: "asset_short",
          asset_sha256: SHA_A,
          analysis_run_id: "analysis_short",
          analysis_revision: 1,
          input_asset_sha256: SHA_A,
          start_ms: 0,
          end_ms: 500,
        },
      },
    ],
  };
  const coverage = {
    object: "coverage_plan.workspace",
    schema_version: "1",
    database_uuid: workspace.database_uuid,
    project_id: "project_1",
    head: { revision: 4, content_sha256: SHA_B },
    selected_revision: {
      revision: 4,
      content_sha256: SHA_B,
      evidence_manifest_sha256: SHA_C,
      operation_id: "operation_coverage",
      is_head: true,
    },
    freshness: { state: "current", reasons: [] },
    coverage_plan: coveragePlan,
    operations: [],
  };
  return { workspace, coverage };
}

test("move staging reorders clips and fixed bindings, then deterministically reflows time", () => {
  const { workspace, coverage } = fixture();
  const pending = stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "move_clip", clip_id: "clip_video", to_index: 0 },
  });
  assert.equal(pending.previewKind, "local_effect_preview");
  assert.equal(pending.database_uuid, workspace.database_uuid);
  assert.equal(pending.project_id, workspace.project_id);
  assert.deepEqual(pending.based_on_head, workspace.head);
  assert.deepEqual(
    pending.preview.timeline.tracks[0].clips.map((clip) => [clip.clip_id, clip.ordinal, clip.start_ms, clip.end_ms]),
    [["clip_video", 0, 0, 1_500], ["clip_image", 1, 1_500, 2_500]],
  );
  assert.deepEqual(
    pending.preview.source_bindings.map((binding) => binding.clip_id),
    ["clip_video", "clip_image"],
  );
  assert.equal(pending.preview.object, "canonical_timeline.pending_preview");
  assert.equal("head" in pending.preview, false);
  assert.equal("selected_revision" in pending.preview, false);
  assert.equal("freshness" in pending.preview, false);
  assert.equal("lifecycle" in pending.preview, false);
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(pending.preview),
    /Invalid canonical Timeline workspace/,
  );
});

test("ordinary apply_edit remains usable on v2 without acquiring structural operations", () => {
  const { workspace, coverage } = fixture();
  const left = workspace.timeline.tracks[0].clips[1];
  left.end_ms = 1_750;
  left.source_out_ms = 5_750;
  const right = {
    ...structuredClone(left),
    clip_id: "clip_video_right",
    ordinal: 2,
    start_ms: 1_750,
    end_ms: 2_500,
    source_in_ms: 5_750,
    source_out_ms: 6_500,
  };
  workspace.timeline.schema_version = "2";
  workspace.timeline.tracks[0].clips.push(right);
  workspace.source_bindings[1].source_out_ms = 5_750;
  workspace.source_bindings.push({
    ...structuredClone(workspace.source_bindings[1]),
    clip_id: right.clip_id,
    source_in_ms: right.source_in_ms,
    source_out_ms: right.source_out_ms,
  });

  const pending = stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "move_clip", clip_id: right.clip_id, to_index: 1 },
  });
  assert.equal(pending.preview.timeline.schema_version, "2");
  const videoOccurrences = pending.preview.timeline.tracks[0].clips
    .filter((clip) => clip.media_kind === "video");
  assert.equal(new Set(videoOccurrences.map((clip) => clip.clip_id)).size, 2);
  assert.equal(new Set(videoOccurrences.map((clip) => clip.beat_id)).size, 1);
  assert.equal(new Set(videoOccurrences.map((clip) => clip.assignment_id)).size, 1);
  assert.equal(new Set(videoOccurrences.map((clip) => clip.evidence_ref)).size, 1);

  assert.throws(
    () => stageCanonicalTimelineEdit({
      workspace,
      coverage,
      edit: { op: "delete_clip", clip_id: right.clip_id },
    }),
    /outside timeline\.apply_edit/,
  );
});

test("trim staging stays inside proof bounds and reflows the following clip", () => {
  const { workspace, coverage } = fixture();
  const pending = stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: {
      op: "trim_clip",
      clip_id: "clip_video",
      source_in_ms: 5_250,
      source_out_ms: 6_000,
    },
  });
  const clip = pending.preview.timeline.tracks[0].clips[1];
  assert.deepEqual([clip.start_ms, clip.end_ms, clip.source_in_ms, clip.source_out_ms], [1_000, 1_750, 5_250, 6_000]);
  assert.equal(pending.preview.timeline.output.duration_ms, 1_750);
  assert.equal(pending.preview.source_bindings[1].source_in_ms, 5_250);
  assert.throws(() => stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "trim_clip", clip_id: "clip_video", source_in_ms: 7_950, source_out_ms: 8_100 },
  }), /outside the pinned verified span/);
});

test("image duration staging never mutates the canonical input snapshot", () => {
  const { workspace, coverage } = fixture();
  const pending = stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "set_clip_duration", clip_id: "clip_image", duration_ms: 2_000 },
  });
  assert.equal(workspace.timeline.output.duration_ms, 2_500);
  assert.equal(pending.preview.timeline.output.duration_ms, 3_500);
  assert.deepEqual(
    pending.preview.timeline.tracks[0].clips.map((clip) => [clip.start_ms, clip.end_ms]),
    [[0, 2_000], [2_000, 3_500]],
  );
  assert.throws(() => stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "set_clip_duration", clip_id: "clip_image", duration_ms: 1_800_000 },
  }), /exceeds the canonical duration limit/);
});

test("replacement candidates are same-Beat, verified, long enough, and save-then-reread", () => {
  const { workspace, coverage } = fixture();
  assert.deepEqual(
    canonicalTimelineReplacementCandidates(workspace, coverage, "clip_video")
      .map((candidate) => candidate.assignment_id),
    ["assign_replacement"],
  );
  const pending = stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "replace_clip", clip_id: "clip_video", assignment_id: "assign_replacement" },
  });
  assert.equal(pending.previewKind, "save_then_reread");
  assert.equal(pending.preview, null);
  assert.throws(() => stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "replace_clip", clip_id: "clip_video", assignment_id: "assign_short" },
  }), /not an eligible/);
});

test("staging fails closed for stale heads and produces stable pending keys", () => {
  const { workspace, coverage } = fixture();
  workspace.freshness = { state: "stale_coverage", reasons: ["coverage_head_changed"] };
  assert.throws(() => stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "move_clip", clip_id: "clip_video", to_index: 0 },
  }), /not the exact current head/);
  assert.equal(
    canonicalTimelinePendingEditKey({
      op: "trim_clip",
      clip_id: "clip_video",
      source_in_ms: 5_000,
      source_out_ms: 6_000,
    }),
    "trim_clip:clip_video:5000:6000",
  );
});

test("staging rejects a Coverage resource whose exact selected proof is not pinned", () => {
  const { workspace, coverage } = fixture();
  coverage.selected_revision.evidence_manifest_sha256 = SHA_A;
  assert.throws(() => stageCanonicalTimelineEdit({
    workspace,
    coverage,
    edit: { op: "move_clip", clip_id: "clip_video", to_index: 0 },
  }), /not the Timeline's exact pinned current input/);
});
