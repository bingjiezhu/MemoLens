import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { normalizeMatch } from "../src/video/api/normalizers.ts";
import {
  buildCanonicalUsageSelection,
  usageMatchSelectableInBrief,
} from "../src/video/usage.ts";
import {
  ResidualBindingIntegrityError,
  residualParentSegmentId,
} from "../src/video/residual.ts";

const ASSET_SHA = "a".repeat(64);
const ASSET_ID = `asset_${ASSET_SHA.slice(0, 24)}`;
const SOURCE_ID = `src_${"b".repeat(24)}`;
const RUN_ID = `arun_${"c".repeat(32)}`;
const PARENT_ID = `seg_${ASSET_SHA.slice(0, 24)}_1_0`;
const RESIDUAL_ID = "rseg_4dc2f2577b0008f345a4c5269fa36c8e663c2d57e1915d286b5e37faf97599e1";
const USAGE_REVISION = "d".repeat(64);
const DERIVATIVE_REVISION = "e".repeat(64);

function residualBinding() {
  return {
    object: "memolens.residual_binding",
    contract_version: "1",
    residual_id: RESIDUAL_ID,
    parent_segment_id: PARENT_ID,
    asset_id: ASSET_ID,
    asset_sha256: ASSET_SHA,
    asset_source_id: SOURCE_ID,
    source_binding_sha256: "f9c22c3de3cc956c06d72b6f632bbc6c369b781534ef592545d6a75b0e2d7dcf",
    analysis_run_id: RUN_ID,
    analysis_revision: 1,
    input_asset_sha256: ASSET_SHA,
    parent_start_ms: 0,
    parent_end_ms: 60_000,
    source_in_ms: 0,
    source_out_ms: 10_000,
    usage_revision: USAGE_REVISION,
    parent_usage_projection_sha256: "06c88d34519a2aa95a51d9a4239d3a6904a05aa51d5f42b9968efa73ca1f7a24",
  };
}

function residualMatch() {
  return {
    object: "creative_asset_match",
    schema_version: "1",
    result_type: "video_segment",
    id: RESIDUAL_ID,
    parent_segment_id: PARENT_ID,
    residual_binding: residualBinding(),
    asset_id: ASSET_ID,
    asset_sha256: ASSET_SHA,
    asset_source_id: SOURCE_ID,
    filename: "renamed-source.mp4",
    start_ms: 0,
    end_ms: 10_000,
    duration_ms: 60_000,
    thumbnail_url: `/v1/video-segments/${PARENT_ID}/thumbnail`,
    media_url: `/v1/assets/${ASSET_ID}/media`,
    summary: "exact remainder",
    matched_terms: ["remainder"],
    score: 1,
    confidence: null,
    analysis_status: "current",
    analysis_run_id: RUN_ID,
    analysis_revision: 1,
    provenance: ["local_keyframe"],
    usage: {
      asset_id: ASSET_ID,
      media_kind: "video",
      occurrence_count: 1,
      used: true,
      used_in: [{ project_id: "project_alpha", export_revision: 1 }],
      source_domain: [0, 60_000],
      candidate_domain: [0, 10_000],
      used_intervals: [],
      residual_intervals: [[0, 10_000]],
      fully_used: false,
      has_residual: true,
    },
  };
}

test("residual match stays parent-resolved and freezes its full binding in Brief selection", () => {
  const match = normalizeMatch(residualMatch(), { requireCanonicalUsage: true });

  assert.equal(match.id, RESIDUAL_ID);
  assert.equal(match.parent_segment_id, PARENT_ID);
  assert.equal(match.residual_binding.parent_usage_projection_sha256, residualBinding().parent_usage_projection_sha256);
  assert.equal(residualParentSegmentId(match), PARENT_ID);
  assert.equal(usageMatchSelectableInBrief(match, "unused_only"), true);

  const selection = buildCanonicalUsageSelection(
    [match.id],
    [match],
    "unused_only",
    USAGE_REVISION,
    DERIVATIVE_REVISION,
  );
  assert.equal(selection.derivative_revision, DERIVATIVE_REVISION);
  assert.deepEqual(selection.candidates[0].residual_binding, residualBinding());
});

test("residual normalization rejects open, substituted, and synthetic-resolver bindings", () => {
  const cases = [
    (value) => { value.residual_binding.relative_path = "/tmp/final.mp4"; },
    (value) => { value.residual_binding.source_out_ms = 9_999; },
    (value) => { value.parent_segment_id = `seg_${"0".repeat(24)}_1_0`; },
    (value) => { value.thumbnail_url = `/v1/video-segments/${RESIDUAL_ID}/thumbnail`; },
  ];
  for (const mutate of cases) {
    const raw = structuredClone(residualMatch());
    mutate(raw);
    assert.throws(
      () => normalizeMatch(raw, { requireCanonicalUsage: true }),
      (error) => error instanceof Error
        && (
          error instanceof ResidualBindingIntegrityError
          || /residual candidate/.test(error.message)
        ),
    );
  }
});

test("ordinary segment cannot acquire residual-only authority fields", () => {
  const raw = residualMatch();
  raw.id = PARENT_ID;
  assert.throws(
    () => normalizeMatch(raw, { requireCanonicalUsage: true }),
    /ordinary candidate carries residual-only authority fields/,
  );
});

test("visible raw search explains exact-SHA derivative exclusion without claiming deletion", () => {
  const source = readFileSync(
    new URL("../src/VideoWorkbench.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    source,
    /Raw search excludes bytes proven to be successful canonical video outputs by exact SHA-256/,
  );
  assert.match(source, /including renamed or copied imports/);
  assert.match(source, /does not delete or move the original file/);
});
