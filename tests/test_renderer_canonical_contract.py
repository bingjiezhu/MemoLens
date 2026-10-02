"""Production Core payloads must remain readable by the actual TS renderer."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.media_db import MediaRepository
from scripts.prepare_b2b4_real_host_fixture import prepare_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NORMALIZE_PRODUCTION_PAYLOADS = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { normalizeCoverageWorkspace } from './src/blueprint/coverageModel.ts';
import { normalizeCanonicalTimelineWorkspace } from './src/blueprint/timelineModel.ts';
import { buildTimelinePreviewMediaItems } from './src/blueprint/timelinePreviewModel.ts';

const payload = JSON.parse(readFileSync(0, 'utf8'));
const fields = ['analysis_run_id', 'analysis_revision', 'analysis_content_sha256', 'source_binding_sha256'];
const coverage = normalizeCoverageWorkspace(payload.coverage);
for (const [index, evidence] of coverage.coverage_plan.evidence_manifest.entries()) {
  assert.equal(evidence.proof.kind, 'asset');
  for (const field of fields) {
    assert.equal(evidence.proof[field], payload.coverage.coverage_plan.evidence_manifest[index].proof[field]);
    assert.notEqual(evidence.proof[field], undefined);
  }
}
const timelines = payload.timelines.map(normalizeCanonicalTimelineWorkspace);
assert.deepEqual(timelines.map((value) => value.timeline.schema_version), ['1', '2']);
for (const [index, timeline] of timelines.entries()) {
  assert.deepEqual(timeline.source_bindings, payload.timelines[index].source_bindings);
  for (const [ordinal, clip] of timeline.timeline.tracks[0].clips.entries()) {
    for (const field of fields) {
      assert.equal(clip[field], payload.timelines[index].timeline.tracks[0].clips[ordinal][field]);
      assert.equal(clip[field], timeline.source_bindings[ordinal][field]);
      assert.notEqual(clip[field], undefined);
    }
  }
  assert.equal(buildTimelinePreviewMediaItems('http://127.0.0.1:5519', timeline).length,
    timeline.timeline.tracks[0].clips.length);
}

const invalidFields = [
  ['analysis_run_id', 'not-an-analysis-run'],
  ['analysis_revision', true], ['analysis_revision', 0],
  ['analysis_revision', 1.5], ['analysis_revision', 1_800_001],
  ['analysis_content_sha256', 'UPPERCASE'], ['source_binding_sha256', null],
  ['input_asset_sha256', 'a'.repeat(64)], ['path', '/private/source.jpg'],
];
for (const [field, value] of invalidFields) {
  const wrongCoverage = structuredClone(payload.coverage);
  wrongCoverage.coverage_plan.evidence_manifest[0].proof[field] = value;
  assert.throws(() => normalizeCoverageWorkspace(wrongCoverage), /Invalid canonical Coverage/);
  for (const original of payload.timelines) {
    const wrongTimeline = structuredClone(original);
    wrongTimeline.timeline.tracks[0].clips[0][field] = value;
    assert.throws(() => normalizeCanonicalTimelineWorkspace(wrongTimeline), /Invalid canonical Timeline/);
  }
}
for (const field of fields) {
  const partialCoverage = structuredClone(payload.coverage);
  delete partialCoverage.coverage_plan.evidence_manifest[0].proof[field];
  assert.throws(() => normalizeCoverageWorkspace(partialCoverage), /Invalid canonical Coverage/);
  for (const original of payload.timelines) {
    const partialClip = structuredClone(original);
    delete partialClip.timeline.tracks[0].clips[0][field];
    assert.throws(() => normalizeCanonicalTimelineWorkspace(partialClip), /Invalid canonical Timeline/);
    const partialSource = structuredClone(original);
    delete partialSource.source_bindings[0][field];
    assert.throws(() => normalizeCanonicalTimelineWorkspace(partialSource), /Invalid canonical Timeline/);
    const substitution = structuredClone(original);
    substitution.source_bindings[0][field] = field === 'analysis_revision' ? 2
      : field === 'analysis_run_id' ? 'arun_' + '0'.repeat(32) : '0'.repeat(64);
    assert.throws(() => normalizeCanonicalTimelineWorkspace(substitution), /Invalid canonical Timeline/);
    assert.deepEqual(buildTimelinePreviewMediaItems('http://127.0.0.1:5519', substitution), []);
    assert.deepEqual(buildTimelinePreviewMediaItems('http://127.0.0.1:5519', partialSource), []);
  }
}
for (const original of payload.timelines) {
  const downgraded = structuredClone(original);
  for (const field of fields) delete downgraded.source_bindings[0][field];
  assert.throws(() => normalizeCanonicalTimelineWorkspace(downgraded), /Invalid canonical Timeline/);
  assert.deepEqual(buildTimelinePreviewMediaItems('http://127.0.0.1:5519', downgraded), []);
}
console.log(JSON.stringify({ coverage: 'accepted', timeline_schemas: ['1', '2'], negative_cases: 'rejected' }));
"""


class RendererCanonicalContractTests(unittest.TestCase):
    def test_production_image_coverage_and_v1_v2_timelines_pass_ts_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-renderer-contract-") as directory:
            root = Path(directory) / "b2b4-real-host-20261002T000000Z"
            manifest = prepare_fixture(root)
            project_id = str(manifest["projects"][0]["project_id"])
            repository = MediaRepository(root / "authority" / "media.db")
            try:
                coverage = CoverageService(repository).read(project_id)
                timelines = TimelineLoweringService(repository)
                first = timelines.read(project_id)
                self.assertEqual(first["timeline"]["schema_version"], "1")
                result = timelines.apply_structural_edit(project_id, {
                    "expected_blueprint": first["head"]["blueprint_binding"],
                    "expected_coverage": first["head"]["coverage_binding"],
                    "expected_timeline_head": first["head"],
                    "structural_edit": {
                        "op": "delete_clip",
                        "clip_id": first["timeline"]["tracks"][0]["clips"][0]["clip_id"],
                    },
                }, idempotency_key="renderer-contract-v2", expected_database_uuid=repository.database_uuid)
                self.assertEqual(result.response_status, 201)
                second = timelines.read(project_id)
                self.assertEqual(second["timeline"]["schema_version"], "2")
                result = subprocess.run([
                    "node", "--experimental-strip-types", "--experimental-loader",
                    "./tests/ts-extension-loader.mjs", "--input-type=module", "-e",
                    NORMALIZE_PRODUCTION_PAYLOADS,
                ], cwd=PROJECT_ROOT, input=json.dumps({
                    "coverage": coverage, "timelines": [first, second],
                }), text=True, capture_output=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["timeline_schemas"], ["1", "2"])
            finally:
                repository.close()
