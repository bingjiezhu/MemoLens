import assert from "node:assert/strict";
import test from "node:test";

import {
  buildTimelinePreviewMediaItems,
  buildPendingTimelinePreviewMediaItems,
  canonicalTimelinePreviewIdentity,
  clampTimelinePreviewPlayhead,
  isPendingTimelinePreviewForWorkspace,
  timelinePreviewClipAtPlayhead,
  timelinePreviewPlayheadFromFraction,
  timelinePreviewResourceUrl,
  timelinePreviewVideoSourceTimeMs,
} from "../src/blueprint/timelinePreviewModel.ts";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);

const imageClip = {
  clip_id: "clip_image",
  ordinal: 0,
  beat_id: "beat_image",
  assignment_id: "assignment_image",
  evidence_ref: "memolens://evidence/asset/img_aaaaaaaaaaaaaaaaaaaaaaaa",
  asset_id: "img_aaaaaaaaaaaaaaaaaaaaaaaa",
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
  assignment_id: "assignment_video",
  evidence_ref: "memolens://evidence/span/seg_bbbbbbbbbbbbbbbbbbbbbbbb_1_5000",
  asset_id: "asset_bbbbbbbbbbbbbbbbbbbbbbbb",
  asset_sha256: SHA_B,
  media_kind: "video",
  start_ms: 1_000,
  end_ms: 2_500,
  fit: "cover",
  audio_enabled: false,
  span_id: "seg_bbbbbbbbbbbbbbbbbbbbbbbb_1_5000",
  analysis_run_id: "arun_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  analysis_revision: 1,
  input_asset_sha256: SHA_B,
  source_in_ms: 5_000,
  source_out_ms: 6_500,
};

function sourceBinding(clip, sourceId) {
  return {
    ...clip,
    asset_source_id: sourceId,
    coverage_proof_sha256: SHA_A,
  };
}

function workspace() {
  return {
    object: "canonical_timeline.workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    project_id: "project-1",
    head: null,
    selected_revision: {
      revision: 3,
      revision_sha256: SHA_A,
      timeline_id: "timeline-1",
      timeline_content_sha256: SHA_B,
      blueprint_binding: {},
      coverage_binding: {},
      operation_id: "operation-3",
      is_head: true,
    },
    freshness: { state: "current", reasons: [] },
    timeline: {
      object: "memolens.canonical_timeline",
      schema_version: "1",
      timeline_id: "timeline-1",
      project_id: "project-1",
      revision: 3,
      parent: { revision: 2, content_sha256: SHA_A },
      blueprint_binding: {},
      coverage_binding: {},
      compiler: {},
      output: { duration_ms: 2_500, aspect_ratio: "9:16" },
      tracks: [{ track_id: "track-1", kind: "primary_visual", clips: [imageClip, videoClip] }],
    },
    source_bindings: [
      sourceBinding(imageClip, "src_image"),
      sourceBinding(videoClip, "src_video"),
    ],
    lifecycle: { state: "draft", approval: "not_established", exportable: false },
  };
}

test("inspection playhead clamps and locates exact half-open hard cuts", () => {
  const clips = [imageClip, videoClip];
  assert.equal(clampTimelinePreviewPlayhead(-1, 2_500), 0);
  assert.equal(clampTimelinePreviewPlayhead(9_000, 2_500), 2_500);
  assert.equal(clampTimelinePreviewPlayhead(Number.NaN, 2_500), 0);
  assert.equal(timelinePreviewPlayheadFromFraction(0.5, 2_500), 1_250);
  assert.equal(timelinePreviewPlayheadFromFraction(2, 2_500), 2_500);

  assert.equal(timelinePreviewClipAtPlayhead(clips, 999).clip.clip_id, "clip_image");
  assert.equal(timelinePreviewClipAtPlayhead(clips, 1_000).clip.clip_id, "clip_video");
  assert.equal(timelinePreviewClipAtPlayhead(clips, 1_000).offsetMs, 0);
  assert.equal(timelinePreviewClipAtPlayhead(clips, 2_500).clip.clip_id, "clip_video");
  assert.equal(timelinePreviewClipAtPlayhead(clips, 2_500).offsetMs, 1_500);
  assert.equal(timelinePreviewClipAtPlayhead([], 0), null);
});

test("video playhead maps into the exact source span without crossing its end", () => {
  assert.equal(timelinePreviewVideoSourceTimeMs(videoClip, 1_000), 5_000);
  assert.equal(timelinePreviewVideoSourceTimeMs(videoClip, 1_750), 5_750);
  assert.equal(timelinePreviewVideoSourceTimeMs(videoClip, 2_500), 6_499);
  assert.equal(timelinePreviewVideoSourceTimeMs(videoClip, 99_000), 6_499);
});

test("inspection URLs remain on the configured backend origin", () => {
  assert.equal(
    timelinePreviewResourceUrl("http://127.0.0.1:43110", "/v1/assets/asset_1/media"),
    "http://127.0.0.1:43110/v1/assets/asset_1/media",
  );
  assert.equal(
    timelinePreviewResourceUrl("http://127.0.0.1:43110", "https://example.com/private.mp4"),
    null,
  );
  assert.equal(timelinePreviewResourceUrl("file:///tmp", "/v1/assets/asset_1/media"), null);
});

test("media plans use only exact clip bindings and path-free backend routes", () => {
  const exact = workspace();
  const items = buildTimelinePreviewMediaItems("http://127.0.0.1:43110", exact);
  assert.equal(items.length, 2);
  assert.equal(items[0].mediaUrl, null);
  assert.equal(
    items[0].thumbnailUrl,
    "http://127.0.0.1:43110/v1/assets/img_aaaaaaaaaaaaaaaaaaaaaaaa/thumbnail",
  );
  assert.equal(
    items[1].mediaUrl,
    "http://127.0.0.1:43110/v1/assets/asset_bbbbbbbbbbbbbbbbbbbbbbbb/media",
  );
  assert.equal(
    items[1].thumbnailUrl,
    "http://127.0.0.1:43110/v1/video-segments/seg_bbbbbbbbbbbbbbbbbbbbbbbb_1_5000/thumbnail",
  );
  assert.equal(JSON.stringify(items).includes("/private/"), false);

  const mismatched = structuredClone(exact);
  mismatched.source_bindings[1].clip_id = "clip_other";
  assert.equal(buildTimelinePreviewMediaItems("http://127.0.0.1:43110", mismatched).length, 0);

  const staleSource = structuredClone(exact);
  staleSource.freshness = {
    state: "stale_source_binding",
    reasons: ["fixed_source_binding_changed"],
  };
  assert.equal(buildTimelinePreviewMediaItems("http://127.0.0.1:43110", staleSource).length, 0);
});

test("v2 split occurrences remain independently viewable by unique clip identity", () => {
  const v2 = workspace();
  const left = v2.timeline.tracks[0].clips[1];
  left.end_ms = 1_750;
  left.source_out_ms = 5_750;
  v2.source_bindings[1].end_ms = 1_750;
  v2.source_bindings[1].source_out_ms = 5_750;
  const right = {
    ...structuredClone(left),
    clip_id: "clip_video_right",
    ordinal: 2,
    start_ms: 1_750,
    end_ms: 2_500,
    source_in_ms: 5_750,
    source_out_ms: 6_500,
  };
  v2.timeline.schema_version = "2";
  v2.timeline.tracks[0].clips.push(right);
  v2.source_bindings.push(sourceBinding(right, "src_video"));

  const items = buildTimelinePreviewMediaItems("http://127.0.0.1:43110", v2);
  assert.deepEqual(
    items.map((item) => item.clip.clip_id),
    ["clip_image", "clip_video", "clip_video_right"],
  );
  assert.equal(items[1].clip.beat_id, items[2].clip.beat_id);
  assert.equal(items[1].clip.assignment_id, items[2].clip.assignment_id);
  assert.equal(items[1].clip.evidence_ref, items[2].clip.evidence_ref);
  assert.equal(items[1].mediaUrl, items[2].mediaUrl);
});

test("preview identity changes on project, revision, or content identity", () => {
  const original = workspace();
  const identity = canonicalTimelinePreviewIdentity(original);
  for (const mutate of [
    (value) => { value.project_id = "project-2"; },
    (value) => { value.selected_revision.revision = 4; },
    (value) => { value.selected_revision.timeline_content_sha256 = SHA_A; },
  ]) {
    const changed = structuredClone(original);
    mutate(changed);
    assert.notEqual(canonicalTimelinePreviewIdentity(changed), identity);
  }
});

test("pending preview is a separate view model admitted only against its exact current head", () => {
  const exact = workspace();
  exact.head = { ...exact.selected_revision };
  delete exact.head.is_head;
  const pending = {
    object: "canonical_timeline.pending_preview",
    schema_version: "1",
    database_uuid: exact.database_uuid,
    project_id: exact.project_id,
    based_on_head: structuredClone(exact.head),
    timeline: structuredClone(exact.timeline),
    source_bindings: structuredClone(exact.source_bindings),
  };
  pending.timeline.output.duration_ms = 3_000;
  pending.timeline.tracks[0].clips[0].end_ms = 1_500;
  pending.timeline.tracks[0].clips[1].start_ms = 1_500;
  pending.timeline.tracks[0].clips[1].end_ms = 3_000;

  assert.equal(isPendingTimelinePreviewForWorkspace(pending, exact), true);
  assert.equal(
    buildPendingTimelinePreviewMediaItems("http://127.0.0.1:43110", pending).length,
    2,
  );

  const stale = structuredClone(exact);
  stale.freshness = {
    state: "stale_source_binding",
    reasons: ["fixed_source_binding_changed"],
  };
  assert.equal(isPendingTimelinePreviewForWorkspace(pending, stale), false);

  const wrongHead = structuredClone(pending);
  wrongHead.based_on_head.operation_id = "different-operation";
  assert.equal(isPendingTimelinePreviewForWorkspace(wrongHead, exact), false);
});
