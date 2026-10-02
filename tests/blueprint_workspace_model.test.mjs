import assert from "node:assert/strict";
import test from "node:test";

import {
  canApplyBlueprintWorkspaceRequest,
  classifyBlueprintWorkspaceInput,
  compareBlueprintSemantics,
  canAdoptBlueprintWorkspaceResponse,
  hasCanonicalWorkspaceMarker,
  isBlueprintProjectWorkspace,
  normalizeBlueprintRevisionResource,
  normalizeBlueprintWorkspace,
} from "../src/blueprint/workspaceModel.ts";
import {
  blueprintRestoreIdempotencyKey,
  fetchBlueprintRevision,
  fetchBlueprintWorkspace,
  restoreBlueprintRevision,
} from "../src/blueprint/workspaceApi.ts";
import {
  coverageMaterializeIdempotencyKey,
  fetchCoverageWorkspace,
  materializeCoverageBaseline,
} from "../src/blueprint/coverageApi.ts";
import {
  isCoverageWorkspaceProjectionForBlueprint,
  normalizeCoverageMaterializeCommandResult,
  normalizeCoverageWorkspace,
} from "../src/blueprint/coverageModel.ts";
import {
  applyCanonicalTimelineEdit,
  applyCanonicalTimelineStructuralEdit,
  canonicalTimelineEditIdempotencyKey,
  canonicalTimelineMaterializeIdempotencyKey,
  canonicalTimelineReconcileIdempotencyKey,
  canonicalTimelineStructuralEditIdempotencyKey,
  fetchCanonicalTimelineWorkspace,
  materializeCanonicalTimelineFirstCut,
  reconcileCanonicalTimelineFromCoverage,
} from "../src/blueprint/timelineApi.ts";
import {
  isTimelineWorkspaceProjectionForBlueprint,
  isSameCanonicalTimelineHead,
  normalizeCanonicalTimelineEditCommandResult,
  normalizeCanonicalTimelineMaterializeCommandResult,
  normalizeCanonicalTimelineReconcileCommandResult,
  normalizeCanonicalTimelineStructuralEdit,
  normalizeCanonicalTimelineStructuralEditCommandResult,
  normalizeCanonicalTimelineWorkspace,
  timelineCoverageBindingFromWorkspace,
} from "../src/blueprint/timelineModel.ts";
import { fetchCreativeProject } from "../src/video/api/creative.ts";
import { VideoApiError } from "../src/video/api/transport.ts";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);
const SHA_E = "e".repeat(64);
const DECISION_UNITS = [
  "intent_goal",
  "intent_stance",
  "script",
  "creative_direction",
  "output",
  "material_constraints",
  "references",
  "techniques",
];

function semantic(goal = "Turn forgotten clips into a short film") {
  return {
    intent: { goal, stance: "Ordinary days deserve to be remembered", audience: "friends", platform: "Douyin" },
    script: { blocks: [{ block_id: "opening", text: "These clips waited on my drive." }] },
    direction: { theme: "memory", narrative_arc: "fragments to story", emotion: "warm", tone: "natural", pace: "calm" },
    output: { duration_target_ms: 30_000, aspect_ratio: "9:16" },
    constraints: { must_include: [], must_exclude: [] },
    material_hints: [],
    reference_refs: [],
    technique_refs: [],
    bindings: { creator_context: null, wiki_generation: null },
    assumptions: [],
    missing_evidence: [],
    open_decisions: [],
  };
}

function authorityProjection(confirmed = []) {
  const selected = new Set(confirmed);
  return {
    state: confirmed.length === 0 ? "unverified" : confirmed.length === 8 ? "confirmed" : "partially_confirmed",
    confirmed_decision_unit_count: confirmed.length,
    decision_unit_count: 8,
    as_of_revision: 2,
    as_of_content_sha256: SHA_B,
    decision_units: Object.fromEntries(DECISION_UNITS.map((name) => [name, {
      semantic_sha256: SHA_A,
      state: selected.has(name) ? "confirmed" : "unverified",
      confirmed: selected.has(name),
      active_confirmation: selected.has(name) ? {
        event_id: "auth_1",
        confirmed_at: "2026-08-23T00:01:00+00:00",
        observed_revision: 2,
        presentation_sha256: SHA_B,
      } : null,
    }])),
  };
}

function workspacePayload() {
  const projection = authorityProjection(["script"]);
  return {
    object: "creative.project_workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    id: "proj_1",
    project_id: "proj_1",
    title: "A grounded short film",
    status: "draft",
    canonical_source: "creative_blueprint",
    workspace_mode: "canonical_blueprint",
    coverage: {
      state: "missing",
      head: null,
      blueprint_binding: null,
      beat_count: 0,
      selected_assignment_count: 0,
      gap_count: 0,
    },
    timeline: {
      state: "missing",
      head: null,
      blueprint_binding: null,
      coverage_binding: null,
      clip_count: 0,
      source_binding_count: 0,
      lifecycle: {
        state: "not_materialized",
        approval: "not_established",
        exportable: false,
      },
    },
    canonical_export: {
      state: "blocked",
      reason_code: "timeline_missing",
      latest_job: null,
    },
    executable: {
      state: "not_compiled",
      current_timeline: null,
      reason_code: "coverage_plan_missing",
    },
    current_blueprint: {
      object: "creative_blueprint.revision",
      schema_version: "1",
      database_uuid: "11111111-2222-4333-8444-555555555555",
      project_id: "proj_1",
      revision: 2,
      content_sha256: SHA_B,
      semantic_sha256: SHA_A,
      operation_id: "op_2",
      current: true,
      blueprint: {
        object: "memolens.creative_blueprint",
        schema_version: "1",
        schema_sha256: SHA_A,
        project_id: "proj_1",
        revision: 2,
        parent: { revision: 1, content_sha256: SHA_A },
        created_by_operation_id: "op_2",
        status: "proposal",
        semantic_sha256: SHA_A,
        semantic: semantic(),
        lineage: { compiler: "memolens.blueprint-proposal-compiler/v1", source_candidate_sha256: null, initial_legacy_brief: { revision: 1, content_sha256: SHA_A } },
        evidence_manifest: [],
        authority: {
          state: "unverified",
          decision_units: Object.fromEntries(DECISION_UNITS.map((name) => [name, {
            semantic_sha256: SHA_A,
            claim: "agent_proposal",
            verified: false,
          }])),
        },
      },
      authority: { state: "unverified", verified: false },
      creation_authority: { state: "unverified", verified: false },
      authority_projection: projection,
    },
    history: {
      complete_project_history: false,
      global_total_order: false,
      replay_scope: "per_lane_only",
      blueprint: {
        lane: "semantic_changes",
        operations: [{
          operation_id: "op_2",
          sequence: 2,
          parent_operation_id: "op_1",
          command_type: "blueprint.commit_proposal",
          intent_code: "propose_blueprint",
          result_kind: "revision_created",
          result_revision: 2,
          result_content_sha256: SHA_B,
          changed_sections: ["intent"],
          restore_from: null,
          input_sha256: SHA_A,
          output_sha256: SHA_B,
          created_at: "2026-08-23T00:00:00+00:00",
        }],
        available_revision_count: 2,
        available_operation_count: 2,
        operations_truncated: true,
        ledger: { coverage_scope: "creative_blueprint", complete_project_history: false },
        order: "sequence_desc",
      },
      decision_authority: {
        lane: "decision_authority",
        events: [{ event_id: "auth_1", sequence: 1, authority_operation: "confirm", observed_revision: 2, decision_units: ["script"], created_at: "2026-08-23T00:01:00+00:00" }],
        available_event_count: 1,
        events_truncated: false,
        projection,
        coverage_scope: "blueprint_decision_authority",
        complete_project_history: false,
        order: "sequence_asc",
      },
      legacy_artifacts: {
        lane: "legacy_artifacts",
        status: "available",
        reason_code: null,
        role: "migration_context_only",
        coverage_scope: "legacy_briefs_and_timelines",
        order: "brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc",
        authoritative_timeline_head: false,
        briefs: [{ revision: 1, content_sha256: SHA_A, created_at: "2026-08-22T00:00:00+00:00", role: "migration_context_only" }],
        brief_count: 1,
        briefs_truncated: false,
        timelines: [],
        timeline_revision_count: 0,
        timelines_truncated: false,
      },
    },
    capabilities: {
      complete_project_history: false,
      authoritative_timeline_head: false,
      coverage_plan_materialization: true,
      blueprint_timeline_compiler: true,
      timeline_materialization: false,
      timeline_reconciliation: false,
      timeline_mutation: false,
      preview: false,
      render: false,
      export: false,
      canonical_export: false,
    },
    created_at: "2026-08-22T00:00:00+00:00",
    updated_at: "2026-08-23T00:00:00+00:00",
  };
}

function setCoverageState(payload, state) {
  const reasonCode = {
    missing: "coverage_plan_missing",
    current: "coverage_not_lowerable",
    stale_blueprint: "coverage_plan_stale_blueprint",
    stale_evidence: "coverage_plan_stale_evidence",
  }[state];
  payload.executable.reason_code = reasonCode;
  payload.capabilities.timeline_materialization = false;
  if (state === "missing") {
    payload.coverage = {
      state,
      head: null,
      blueprint_binding: null,
      beat_count: 0,
      selected_assignment_count: 0,
      gap_count: 0,
    };
    return payload;
  }
  payload.coverage = {
    state,
    head: { revision: 1, content_sha256: SHA_A },
    blueprint_binding: {
      revision: payload.current_blueprint.revision,
      content_sha256: payload.current_blueprint.content_sha256,
      semantic_sha256: payload.current_blueprint.semantic_sha256,
    },
    beat_count: 3,
    selected_assignment_count: 2,
    gap_count: 1,
  };
  if (state === "stale_blueprint") {
    payload.coverage.blueprint_binding.content_sha256 = SHA_A;
  }
  return payload;
}

function coverageWorkspacePayload() {
  const assetOne = `asset_${"d".repeat(24)}`;
  const assetTwo = `asset_${"e".repeat(24)}`;
  const unresolvedAsset = `asset_${"3".repeat(24)}`;
  const spanOne = `seg_${"e".repeat(24)}_1_0`;
  const beatOne = `beat_${"6".repeat(24)}`;
  const beatTwo = `beat_${"7".repeat(24)}`;
  const needOne = `need_${"8".repeat(24)}`;
  const needTwo = `need_${"9".repeat(24)}`;
  const assignmentOne = `assign_${"a".repeat(24)}`;
  const assignmentTwo = `assign_${"b".repeat(24)}`;
  const assignmentThree = `assign_${"c".repeat(24)}`;
  const assetRef = `memolens://evidence/asset/${assetOne}`;
  const unresolvedRef = `memolens://evidence/asset/${unresolvedAsset}`;
  const spanRef = `memolens://evidence/span/${spanOne}`;
  const resultHead = {
    revision: 1,
    content_sha256: SHA_C,
    blueprint_revision: 2,
    blueprint_content_sha256: SHA_B,
    blueprint_semantic_sha256: SHA_A,
    operation_id: "coverage_op_1",
  };
  return {
    object: "coverage_plan.workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    project_id: "proj_1",
    head: { revision: 1, content_sha256: SHA_C },
    selected_revision: {
      revision: 1,
      content_sha256: SHA_C,
      evidence_manifest_sha256: SHA_D,
      operation_id: "coverage_op_1",
      is_head: true,
    },
    freshness: { state: "current", reasons: [] },
    coverage_plan: {
      object: "memolens.coverage_plan",
      schema_version: "1",
      project_id: "proj_1",
      revision: 1,
      parent: null,
      blueprint_binding: {
        revision: 2,
        content_sha256: SHA_B,
        semantic_sha256: SHA_A,
      },
      compiler: {
        id: "memolens.coverage-baseline/v1",
        timing_capability: "estimated_text",
        semantic_matching: false,
        global_optimization: false,
      },
      output_binding: { duration_target_ms: 20_000, aspect_ratio: "9:16" },
      beats: [
        {
          beat_id: beatOne,
          script_block_id: "opening",
          ordinal: 0,
          text_sha256: SHA_D,
          timing: { basis: "estimated_text", start_ms: 0, end_ms: 10_000 },
          needs: [{
            need_id: needOne,
            kind: "depiction_unspecified",
            priority: "should",
            allow_empty: true,
          }],
          selected_assignments: [{
            assignment_id: assignmentOne,
            need_id: needOne,
            hint_id: "hint_1",
            evidence_ref: assetRef,
            match_type: "depiction_unspecified",
            timeline_duration_ms: 10_000,
            reason_sha256: SHA_A,
            locked: false,
          }],
          alternatives: [{
            assignment_id: assignmentTwo,
            need_id: needOne,
            hint_id: "hint_2",
            evidence_ref: spanRef,
            match_type: "depiction_unspecified",
            timeline_duration_ms: 10_000,
            reason_sha256: SHA_B,
            locked: false,
            not_selected_reason: "baseline_first_verified_selected",
          }],
          gap: null,
        },
        {
          beat_id: beatTwo,
          script_block_id: "ending",
          ordinal: 1,
          text_sha256: SHA_E,
          timing: { basis: "estimated_text", start_ms: 10_000, end_ms: 20_000 },
          needs: [{
            need_id: needTwo,
            kind: "depiction_unspecified",
            priority: "should",
            allow_empty: true,
          }],
          selected_assignments: [],
          alternatives: [{
            assignment_id: assignmentThree,
            need_id: needTwo,
            hint_id: "hint_3",
            evidence_ref: unresolvedRef,
            match_type: "depiction_unspecified",
            timeline_duration_ms: 10_000,
            reason_sha256: SHA_C,
            locked: false,
            not_selected_reason: "evidence_unresolved",
          }],
          gap: { code: "evidence_unresolved", blocking: false },
        },
      ],
      evidence_manifest: [
        {
          evidence_ref: unresolvedRef,
          status: "unresolved",
          proof_sha256: null,
          proof: null,
        },
        {
          evidence_ref: assetRef,
          status: "verified",
          proof_sha256: SHA_A,
          proof: { kind: "asset", asset_id: assetOne, asset_sha256: SHA_D },
        },
        {
          evidence_ref: spanRef,
          status: "verified",
          proof_sha256: SHA_B,
          proof: {
            kind: "span",
            span_id: spanOne,
            asset_id: assetTwo,
            asset_sha256: SHA_E,
            analysis_run_id: `arun_${"5".repeat(32)}`,
            analysis_revision: 1,
            input_asset_sha256: SHA_E,
            start_ms: 1_000,
            end_ms: 11_000,
          },
        },
      ],
    },
    operations: [{
      id: "coverage_op_1",
      project_id: "proj_1",
      sequence: 1,
      parent_operation_id: null,
      command_type: "coverage.materialize_baseline",
      command_version: "1",
      effect_class: "reversible_project_write",
      expected_blueprint_revision: 2,
      expected_blueprint_content_sha256: SHA_B,
      expected_plan_head: null,
      request_sha256: SHA_D,
      result_revision: 1,
      result_content_sha256: SHA_C,
      result: { kind: "revision_created", result_head: resultHead },
      created_at: "2026-08-23T00:03:00+00:00",
    }],
  };
}

function coverageCommandPayload() {
  const workspace = coverageWorkspacePayload();
  return {
    object: "coverage_plan.command_result",
    schema_version: "1",
    project_id: workspace.project_id,
    command_type: "coverage.materialize_baseline",
    operation_id: workspace.operations[0].id,
    result: structuredClone(workspace.operations[0].result),
  };
}

function projectWorkspaceWithCoverage() {
  const payload = setCoverageState(workspacePayload(), "current");
  payload.current_blueprint.blueprint.semantic.script.blocks.push({
    block_id: "ending",
    text: "The fragments now have a home.",
  });
  payload.coverage.head = { revision: 1, content_sha256: SHA_C };
  payload.coverage.beat_count = 2;
  payload.coverage.selected_assignment_count = 1;
  payload.coverage.gap_count = 1;
  return payload;
}

function timelineHeadPayload() {
  return {
    revision: 1,
    revision_sha256: SHA_E,
    timeline_id: `timeline_${"1".repeat(24)}`,
    timeline_content_sha256: SHA_A,
    blueprint_binding: {
      revision: 2,
      content_sha256: SHA_B,
      semantic_sha256: SHA_A,
      operation_id: "op_2",
    },
    coverage_binding: {
      revision: 1,
      content_sha256: SHA_C,
      evidence_manifest_sha256: SHA_D,
      operation_id: "coverage_op_1",
    },
    operation_id: "timeline_op_1",
  };
}

function timelineWorkspacePayload() {
  const head = timelineHeadPayload();
  const assetId = `asset_${"d".repeat(24)}`;
  const clip = {
    clip_id: `clip_${"2".repeat(24)}`,
    ordinal: 0,
    beat_id: `beat_${"6".repeat(24)}`,
    assignment_id: `assign_${"a".repeat(24)}`,
    evidence_ref: `memolens://evidence/asset/${assetId}`,
    asset_id: assetId,
    asset_sha256: SHA_D,
    media_kind: "image",
    start_ms: 0,
    end_ms: 10_000,
    fit: "cover",
    audio_enabled: false,
  };
  return {
    object: "canonical_timeline.workspace",
    schema_version: "1",
    database_uuid: "11111111-2222-4333-8444-555555555555",
    project_id: "proj_1",
    head,
    selected_revision: { ...structuredClone(head), is_head: true },
    freshness: { state: "current", reasons: [] },
    timeline: {
      object: "memolens.canonical_timeline",
      schema_version: "1",
      timeline_id: head.timeline_id,
      project_id: "proj_1",
      revision: 1,
      parent: null,
      blueprint_binding: structuredClone(head.blueprint_binding),
      coverage_binding: structuredClone(head.coverage_binding),
      compiler: {
        id: "memolens.timeline-lowerer/v1",
        media: "image_and_video_span",
        transitions: "hard_cut",
        audio: "silent",
        subtitles: "none",
      },
      output: { duration_ms: 10_000, aspect_ratio: "9:16" },
      tracks: [{
        track_id: `track_${"3".repeat(24)}`,
        kind: "primary_visual",
        clips: [clip],
      }],
    },
    source_bindings: [{
      clip_id: clip.clip_id,
      evidence_ref: clip.evidence_ref,
      asset_id: clip.asset_id,
      asset_sha256: clip.asset_sha256,
      asset_source_id: `src_${"4".repeat(24)}`,
      coverage_proof_sha256: SHA_A,
      media_kind: "image",
    }],
    lifecycle: {
      state: "draft",
      approval: "not_established",
      exportable: false,
    },
  };
}

function timelineWorkspaceWithSharedVideoSource() {
  const payload = timelineWorkspacePayload();
  const assetId = `asset_${"e".repeat(24)}`;
  const assetSourceId = `src_${"4".repeat(24)}`;
  const analysisRunId = `arun_${"5".repeat(32)}`;
  const clips = [0, 1].map((ordinal) => {
    const spanId = `seg_${"e".repeat(24)}_1_${ordinal}`;
    const startMs = ordinal * 10_000;
    const sourceInMs = 1_000 + ordinal * 11_000;
    return {
      clip_id: `clip_${String(ordinal + 2).repeat(24)}`,
      ordinal,
      beat_id: `beat_${String(ordinal + 6).repeat(24)}`,
      assignment_id: `assign_${(ordinal === 0 ? "a" : "b").repeat(24)}`,
      evidence_ref: `memolens://evidence/span/${spanId}`,
      asset_id: assetId,
      asset_sha256: SHA_E,
      media_kind: "video",
      span_id: spanId,
      analysis_run_id: analysisRunId,
      analysis_revision: 1,
      input_asset_sha256: SHA_E,
      source_in_ms: sourceInMs,
      source_out_ms: sourceInMs + 10_000,
      start_ms: startMs,
      end_ms: startMs + 10_000,
      fit: "cover",
      audio_enabled: false,
    };
  });
  payload.timeline.output.duration_ms = 20_000;
  payload.timeline.tracks[0].clips = clips;
  payload.source_bindings = clips.map((clip) => ({
    clip_id: clip.clip_id,
    evidence_ref: clip.evidence_ref,
    asset_id: clip.asset_id,
    asset_sha256: clip.asset_sha256,
    asset_source_id: assetSourceId,
    coverage_proof_sha256: SHA_A,
    media_kind: clip.media_kind,
    span_id: clip.span_id,
    analysis_run_id: clip.analysis_run_id,
    analysis_revision: clip.analysis_revision,
    input_asset_sha256: clip.input_asset_sha256,
    source_in_ms: clip.source_in_ms,
    source_out_ms: clip.source_out_ms,
  }));
  return payload;
}

function timelineV2WorkspaceWithRepeatedOccurrence({ singleOccurrence = false } = {}) {
  const payload = timelineWorkspacePayload();
  const assetId = `asset_${"e".repeat(24)}`;
  const spanId = `seg_${"e".repeat(24)}_1_0`;
  const analysisRunId = `arun_${"5".repeat(32)}`;
  const repeated = {
    beat_id: `beat_${"6".repeat(24)}`,
    assignment_id: `assign_${"a".repeat(24)}`,
    evidence_ref: `memolens://evidence/span/${spanId}`,
    asset_id: assetId,
    asset_sha256: SHA_E,
    media_kind: "video",
    span_id: spanId,
    analysis_run_id: analysisRunId,
    analysis_revision: 1,
    input_asset_sha256: SHA_E,
    fit: "cover",
    audio_enabled: false,
  };
  const clips = [
    {
      ...repeated,
      clip_id: `clip_${"2".repeat(24)}`,
      ordinal: 0,
      source_in_ms: 1_000,
      source_out_ms: 2_000,
      start_ms: 0,
      end_ms: 1_000,
    },
    {
      ...repeated,
      clip_id: `clip_${"3".repeat(24)}`,
      ordinal: 1,
      source_in_ms: 2_000,
      source_out_ms: 3_000,
      start_ms: 1_000,
      end_ms: 2_000,
    },
  ].slice(0, singleOccurrence ? 1 : 2);
  payload.head.revision = 2;
  payload.head.operation_id = "timeline_structural_op_2";
  payload.selected_revision = { ...structuredClone(payload.head), is_head: true };
  payload.timeline.schema_version = "2";
  payload.timeline.revision = 2;
  payload.timeline.parent = { revision: 1, content_sha256: SHA_B };
  payload.timeline.output.duration_ms = clips.at(-1).end_ms;
  payload.timeline.tracks[0].clips = clips;
  payload.source_bindings = clips.map((clip) => ({
    clip_id: clip.clip_id,
    evidence_ref: clip.evidence_ref,
    asset_id: clip.asset_id,
    asset_sha256: clip.asset_sha256,
    asset_source_id: `src_${"4".repeat(24)}`,
    coverage_proof_sha256: SHA_A,
    media_kind: clip.media_kind,
    span_id: clip.span_id,
    analysis_run_id: clip.analysis_run_id,
    analysis_revision: clip.analysis_revision,
    input_asset_sha256: clip.input_asset_sha256,
    source_in_ms: clip.source_in_ms,
    source_out_ms: clip.source_out_ms,
  }));
  return payload;
}

function coverageWorkspaceWithHistoricalSelection(headRevision) {
  const payload = coverageWorkspacePayload();
  const digestForRevision = (revision) => (
    revision === 1 ? SHA_C : revision.toString(16).padStart(64, "0")
  );
  payload.head = {
    revision: headRevision,
    content_sha256: digestForRevision(headRevision),
  };
  payload.selected_revision.is_head = headRevision === 1;
  payload.operations = Array.from(
    { length: Math.min(headRevision, 50) },
    (_, index) => headRevision - index,
  ).map((revision) => {
    const operationId = `coverage_op_${revision}`;
    const contentSha256 = digestForRevision(revision);
    return {
      id: operationId,
      project_id: "proj_1",
      sequence: revision,
      parent_operation_id: revision === 1 ? null : `coverage_op_${revision - 1}`,
      command_type: "coverage.materialize_baseline",
      command_version: "1",
      effect_class: "reversible_project_write",
      expected_blueprint_revision: 2,
      expected_blueprint_content_sha256: SHA_B,
      expected_plan_head: revision === 1
        ? null
        : {
          revision: revision - 1,
          content_sha256: digestForRevision(revision - 1),
        },
      request_sha256: (revision + 256).toString(16).padStart(64, "0"),
      result_revision: revision,
      result_content_sha256: contentSha256,
      result: {
        kind: "revision_created",
        result_head: {
          revision,
          content_sha256: contentSha256,
          blueprint_revision: 2,
          blueprint_content_sha256: SHA_B,
          blueprint_semantic_sha256: SHA_A,
          operation_id: operationId,
        },
      },
      created_at: new Date(revision * 1_000).toISOString(),
    };
  });
  return payload;
}

function projectWorkspaceWithTimeline() {
  const payload = setCoverageState(workspacePayload(), "current");
  payload.coverage.head = { revision: 1, content_sha256: SHA_C };
  payload.coverage.beat_count = 1;
  payload.coverage.selected_assignment_count = 1;
  payload.coverage.gap_count = 0;
  payload.timeline = {
    state: "current",
    head: timelineHeadPayload(),
    blueprint_binding: structuredClone(timelineHeadPayload().blueprint_binding),
    coverage_binding: structuredClone(timelineHeadPayload().coverage_binding),
    clip_count: 1,
    source_binding_count: 1,
    lifecycle: { state: "draft", approval: "not_established", exportable: false },
  };
  payload.executable = {
    state: "compiled_draft",
    current_timeline: structuredClone(payload.timeline.head),
    reason_code: "canonical_timeline_current",
  };
  payload.capabilities.authoritative_timeline_head = true;
  payload.capabilities.timeline_materialization = false;
  payload.capabilities.timeline_mutation = true;
  payload.canonical_export = {
    state: "available",
    reason_code: "requires_native_confirmation",
    latest_job: null,
  };
  payload.capabilities.canonical_export = true;
  return payload;
}

function timelineCommandPayload() {
  const head = timelineHeadPayload();
  return {
    object: "canonical_timeline.command_result",
    schema_version: "1",
    project_id: "proj_1",
    command_type: "timeline.materialize_first_cut",
    operation_id: head.operation_id,
    result: { kind: "revision_created", result_head: head },
  };
}

function timelineReconcileCommandPayload() {
  const head = timelineHeadPayload();
  head.revision = 2;
  head.operation_id = "timeline_op_2";
  return {
    object: "canonical_timeline.command_result",
    schema_version: "1",
    project_id: "proj_1",
    command_type: "timeline.reconcile_from_coverage",
    operation_id: head.operation_id,
    result: { kind: "revision_created", result_head: head },
  };
}

function timelineEditCommandPayload(edit = {
  op: "trim_clip",
  clip_id: "clip_video",
  source_in_ms: 5_000,
  source_out_ms: 6_000,
}) {
  const head = timelineHeadPayload();
  head.revision = 2;
  head.operation_id = "timeline_edit_op_2";
  return {
    object: "canonical_timeline.command_result",
    schema_version: "1",
    project_id: "proj_1",
    command_type: "timeline.apply_edit",
    operation_id: head.operation_id,
    result: { kind: "revision_created", result_head: head, edit },
  };
}

function timelineStructuralEditCommandPayload(edit = {
  op: "delete_clip",
  clip_id: `clip_${"2".repeat(24)}`,
}) {
  const head = timelineHeadPayload();
  head.revision = 2;
  head.operation_id = "timeline_structural_op_2";
  return {
    object: "canonical_timeline.command_result",
    schema_version: "1",
    project_id: "proj_1",
    command_type: "timeline.apply_structural_edit",
    operation_id: head.operation_id,
    result: { kind: "revision_created", result_head: head, structural_edit: edit },
  };
}

async function withMockTransport(responses, callback) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const requests = [];
  let responseIndex = 0;
  globalThis.window = globalThis;
  globalThis.setTimeout = () => 1;
  globalThis.clearTimeout = () => {};
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url: String(url), init });
    const payload = responses[responseIndex++];
    if (!payload) throw new Error(`Unexpected fetch ${responseIndex}`);
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    return await callback(requests);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
}

test("canonical workspace normalization keeps exact head, authority, and honest lanes", () => {
  const workspace = normalizeBlueprintWorkspace(workspacePayload());
  assert.equal(isBlueprintProjectWorkspace(workspace), true);
  assert.equal(workspace.current_blueprint.revision, 2);
  assert.equal(workspace.current_blueprint.content_sha256, SHA_B);
  assert.equal(workspace.current_blueprint.blueprint.semantic.intent.stance, "Ordinary days deserve to be remembered");
  assert.equal(workspace.history.decision_authority.projection.confirmed_decision_unit_count, 1);
  assert.deepEqual(workspace.history.decision_authority.events[0].decision_units, ["script"]);
  assert.equal(workspace.history.legacy_artifacts.authoritative_timeline_head, false);
  assert.equal(workspace.coverage.state, "missing");
  assert.equal(workspace.executable.reason_code, "coverage_plan_missing");
  assert.equal(workspace.capabilities.coverage_plan_materialization, true);
  assert.equal(workspace.capabilities.blueprint_timeline_compiler, true);
  assert.equal(workspace.capabilities.timeline_materialization, false);
  assert.equal(workspace.capabilities.render, false);
});

test("coverage projection binds current plans and refuses to lower honest gaps", () => {
  const workspace = normalizeBlueprintWorkspace(setCoverageState(workspacePayload(), "current"));
  assert.deepEqual(workspace.coverage, {
    state: "current",
    head: { revision: 1, content_sha256: SHA_A },
    blueprint_binding: {
      revision: 2,
      content_sha256: SHA_B,
      semantic_sha256: SHA_A,
    },
    beat_count: 3,
    selected_assignment_count: 2,
    gap_count: 1,
  });
  assert.equal(workspace.executable.state, "not_compiled");
  assert.equal(workspace.executable.current_timeline, null);
  assert.equal(workspace.executable.reason_code, "coverage_not_lowerable");
  assert.equal(workspace.capabilities.coverage_plan_materialization, true);
  assert.equal(workspace.capabilities.blueprint_timeline_compiler, true);
  assert.equal(workspace.capabilities.timeline_materialization, false);
});

test("coverage projection rejects state, binding, count, and executable contradictions", () => {
  const missingWithHead = workspacePayload();
  missingWithHead.coverage.head = { revision: 1, content_sha256: SHA_A };
  assert.throws(
    () => normalizeBlueprintWorkspace(missingWithHead),
    /coverage\.consistency/,
  );

  const currentWithoutHead = setCoverageState(workspacePayload(), "current");
  currentWithoutHead.coverage.head = null;
  assert.throws(
    () => normalizeBlueprintWorkspace(currentWithoutHead),
    /coverage\.consistency/,
  );

  const currentWithStaleBinding = setCoverageState(workspacePayload(), "current");
  currentWithStaleBinding.coverage.blueprint_binding.content_sha256 = SHA_A;
  assert.throws(
    () => normalizeBlueprintWorkspace(currentWithStaleBinding),
    /coverage\.consistency/,
  );

  const staleBlueprintWithCurrentBinding = setCoverageState(workspacePayload(), "stale_blueprint");
  staleBlueprintWithCurrentBinding.coverage.blueprint_binding.content_sha256 = SHA_B;
  assert.throws(
    () => normalizeBlueprintWorkspace(staleBlueprintWithCurrentBinding),
    /coverage\.consistency/,
  );

  const staleEvidenceWithStaleBlueprint = setCoverageState(workspacePayload(), "stale_evidence");
  staleEvidenceWithStaleBlueprint.coverage.blueprint_binding.revision = 1;
  assert.throws(
    () => normalizeBlueprintWorkspace(staleEvidenceWithStaleBlueprint),
    /coverage\.consistency/,
  );

  const impossibleCounts = setCoverageState(workspacePayload(), "current");
  impossibleCounts.coverage.gap_count = 2;
  assert.throws(
    () => normalizeBlueprintWorkspace(impossibleCounts),
    /coverage\.consistency/,
  );

  const falseExecutableReason = setCoverageState(workspacePayload(), "current");
  falseExecutableReason.executable.reason_code = "coverage_plan_missing";
  assert.throws(
    () => normalizeBlueprintWorkspace(falseExecutableReason),
    /timeline\.consistency/,
  );
});

test("coverage projection is closed and rejects locator leakage or capability inflation", () => {
  const leakedBinding = setCoverageState(workspacePayload(), "current");
  leakedBinding.coverage.blueprint_binding.path = "/private/library.sqlite";
  assert.throws(
    () => normalizeBlueprintWorkspace(leakedBinding),
    /coverage\.blueprint_binding/,
  );

  const unknownCoverageField = workspacePayload();
  unknownCoverageField.coverage.operation_id = "coverage_op_1";
  assert.throws(
    () => normalizeBlueprintWorkspace(unknownCoverageField),
    /workspace\.coverage/,
  );

  const compilerHidden = workspacePayload();
  compilerHidden.capabilities.blueprint_timeline_compiler = false;
  assert.throws(
    () => normalizeBlueprintWorkspace(compilerHidden),
    /blueprint_timeline_compiler/,
  );

  const materializationHidden = workspacePayload();
  materializationHidden.capabilities.coverage_plan_materialization = false;
  assert.throws(
    () => normalizeBlueprintWorkspace(materializationHidden),
    /coverage_plan_materialization/,
  );
});

test("Coverage resource normalization keeps exact Beats, selected material, alternatives, and gaps", () => {
  const coverage = normalizeCoverageWorkspace(coverageWorkspacePayload());
  const project = normalizeBlueprintWorkspace(projectWorkspaceWithCoverage());

  assert.equal(coverage.coverage_plan.beats.length, 2);
  assert.equal(coverage.coverage_plan.beats[0].selected_assignments.length, 1);
  assert.equal(
    coverage.coverage_plan.beats[0].alternatives[0].not_selected_reason,
    "baseline_first_verified_selected",
  );
  assert.deepEqual(
    coverage.coverage_plan.beats[1].gap,
    { code: "evidence_unresolved", blocking: false },
  );
  assert.equal(
    coverage.coverage_plan.evidence_manifest[2].proof.kind,
    "span",
  );
  assert.equal(isCoverageWorkspaceProjectionForBlueprint(project, coverage), true);
  assert.equal(project.capabilities.blueprint_timeline_compiler, true);
  assert.equal(project.capabilities.timeline_materialization, false);
  assert.equal(project.executable.current_timeline, null);
});

test("Coverage resource boundary rejects locator leakage and cross-projection contradictions", () => {
  const leaked = coverageWorkspacePayload();
  leaked.coverage_plan.evidence_manifest[1].proof.path = "/private/secret.jpg";
  assert.throws(
    () => normalizeCoverageWorkspace(leaked),
    /evidence_manifest\[1\]\.proof/,
  );

  const unresolvedSelection = coverageWorkspacePayload();
  unresolvedSelection.coverage_plan.evidence_manifest[1].status = "unresolved";
  unresolvedSelection.coverage_plan.evidence_manifest[1].proof_sha256 = null;
  unresolvedSelection.coverage_plan.evidence_manifest[1].proof = null;
  assert.throws(
    () => normalizeCoverageWorkspace(unresolvedSelection),
    /selected_assignments\[0\]\.evidence_ref/,
  );

  const hiddenGap = coverageWorkspacePayload();
  hiddenGap.coverage_plan.beats[1].gap = null;
  assert.throws(
    () => normalizeCoverageWorkspace(hiddenGap),
    /beats\[1\]\.gap/,
  );

  const falseFreshness = coverageWorkspacePayload();
  falseFreshness.freshness = { state: "current", reasons: ["evidence_head_changed"] };
  assert.throws(
    () => normalizeCoverageWorkspace(falseFreshness),
    /coverage\.freshness/,
  );

  const mismatchedOperation = coverageWorkspacePayload();
  mismatchedOperation.operations[0].result.result_head.content_sha256 = SHA_D;
  assert.throws(
    () => normalizeCoverageWorkspace(mismatchedOperation),
    /operations\[0\]\.result/,
  );

  const coverage = normalizeCoverageWorkspace(coverageWorkspacePayload());
  const wrongSummary = normalizeBlueprintWorkspace(projectWorkspaceWithCoverage());
  wrongSummary.coverage.gap_count = 2;
  assert.equal(isCoverageWorkspaceProjectionForBlueprint(wrongSummary, coverage), false);

  const wrongScript = normalizeBlueprintWorkspace(projectWorkspaceWithCoverage());
  wrongScript.current_blueprint.blueprint.semantic.script.blocks[1].block_id = "different";
  assert.equal(isCoverageWorkspaceProjectionForBlueprint(wrongScript, coverage), false);
});

test("Coverage command result is closed and bound to one operation", () => {
  const result = normalizeCoverageMaterializeCommandResult(coverageCommandPayload());
  assert.equal(result.operation_id, "coverage_op_1");
  assert.equal(result.result.result_head.revision, 1);

  const inflated = coverageCommandPayload();
  inflated.result.result_head.timeline_id = "timeline_not_created";
  assert.throws(
    () => normalizeCoverageMaterializeCommandResult(inflated),
    /coverage_command\.result\.result_head/,
  );
});

test("Coverage exact historical selection survives a bounded operation window", () => {
  const deepHistory = coverageWorkspaceWithHistoricalSelection(51);
  const normalized = normalizeCoverageWorkspace(deepHistory);
  assert.equal(normalized.head.revision, 51);
  assert.equal(normalized.selected_revision.revision, 1);
  assert.equal(normalized.selected_revision.is_head, false);
  assert.equal(normalized.operations.length, 50);
  assert.equal(normalized.operations.at(-1).result_revision, 2);

  const shallowMismatch = coverageWorkspaceWithHistoricalSelection(2);
  shallowMismatch.operations[1].result_content_sha256 = SHA_D;
  shallowMismatch.operations[1].result.result_head.content_sha256 = SHA_D;
  assert.throws(
    () => normalizeCoverageWorkspace(shallowMismatch),
    /coverage\.consistency/,
  );
});

test("canonical Timeline resource adopts only closed path-free Beat to clip identities", () => {
  const timeline = normalizeCanonicalTimelineWorkspace(timelineWorkspacePayload());
  const project = normalizeBlueprintWorkspace(projectWorkspaceWithTimeline());

  assert.equal(timeline.timeline.tracks[0].clips[0].beat_id, `beat_${"6".repeat(24)}`);
  assert.equal(timeline.timeline.compiler.transitions, "hard_cut");
  assert.equal(timeline.timeline.compiler.audio, "silent");
  assert.equal(timeline.timeline.schema_version, "1");
  assert.equal(timeline.lifecycle.approval, "not_established");
  assert.equal(timeline.lifecycle.exportable, false);
  assert.equal(isTimelineWorkspaceProjectionForBlueprint(project, timeline), true);
  assert.equal(project.executable.state, "compiled_draft");
  assert.equal(project.capabilities.preview, false);
  assert.equal(project.capabilities.export, false);
  assert.equal(project.capabilities.canonical_export, true);
  assert.equal(project.canonical_export.reason_code, "requires_native_confirmation");

  const staleBlueprintClaimedCurrent = projectWorkspaceWithTimeline();
  const staleBlueprintBinding = {
    revision: 1,
    content_sha256: SHA_A,
    semantic_sha256: SHA_C,
    operation_id: "op_1",
  };
  staleBlueprintClaimedCurrent.timeline.blueprint_binding = structuredClone(staleBlueprintBinding);
  staleBlueprintClaimedCurrent.timeline.head.blueprint_binding = structuredClone(staleBlueprintBinding);
  staleBlueprintClaimedCurrent.executable.current_timeline.blueprint_binding = structuredClone(staleBlueprintBinding);
  assert.throws(
    () => normalizeBlueprintWorkspace(staleBlueprintClaimedCurrent),
    /workspace\.timeline\.consistency/,
  );

  const staleCoverageClaimedCurrent = projectWorkspaceWithTimeline();
  const staleCoverageBinding = {
    ...structuredClone(staleCoverageClaimedCurrent.timeline.coverage_binding),
    content_sha256: SHA_A,
  };
  staleCoverageClaimedCurrent.timeline.coverage_binding = structuredClone(staleCoverageBinding);
  staleCoverageClaimedCurrent.timeline.head.coverage_binding = structuredClone(staleCoverageBinding);
  staleCoverageClaimedCurrent.executable.current_timeline.coverage_binding = structuredClone(staleCoverageBinding);
  assert.throws(
    () => normalizeBlueprintWorkspace(staleCoverageClaimedCurrent),
    /workspace\.timeline\.consistency/,
  );

  for (const reason of [
    "coverage_head_changed",
    "coverage_plan_stale_blueprint",
    "coverage_plan_stale_evidence",
  ]) {
    const stale = timelineWorkspacePayload();
    stale.freshness = { state: "stale_coverage", reasons: [reason] };
    assert.equal(
      normalizeCanonicalTimelineWorkspace(stale).freshness.reasons[0],
      reason,
    );
  }
});

test("canonical Timeline reader preserves v1 uniqueness and reads v2 occurrence cardinality", () => {
  const repeatedPayload = timelineV2WorkspaceWithRepeatedOccurrence();
  const repeated = normalizeCanonicalTimelineWorkspace(repeatedPayload);
  const clips = repeated.timeline.tracks[0].clips;
  assert.equal(repeated.schema_version, "1");
  assert.equal(repeated.timeline.schema_version, "2");
  assert.equal(new Set(clips.map((clip) => clip.clip_id)).size, 2);
  assert.equal(new Set(clips.map((clip) => clip.beat_id)).size, 1);
  assert.equal(new Set(clips.map((clip) => clip.assignment_id)).size, 1);
  assert.equal(new Set(clips.map((clip) => clip.evidence_ref)).size, 1);
  assert.deepEqual(
    repeated.source_bindings.map((binding) => [binding.source_in_ms, binding.source_out_ms]),
    [[1_000, 2_000], [2_000, 3_000]],
  );

  const v1CannotClaimRepeatedOccurrences = structuredClone(repeatedPayload);
  v1CannotClaimRepeatedOccurrences.timeline.schema_version = "1";
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(v1CannotClaimRepeatedOccurrences),
    /clips\.identities/,
  );

  const duplicateClipId = structuredClone(repeatedPayload);
  duplicateClipId.timeline.tracks[0].clips[1].clip_id = clips[0].clip_id;
  duplicateClipId.source_bindings[1].clip_id = clips[0].clip_id;
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(duplicateClipId),
    /clips\.identities/,
  );

  const substitutedProof = structuredClone(repeatedPayload);
  substitutedProof.source_bindings[1].coverage_proof_sha256 = SHA_B;
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(substitutedProof),
    /timeline\.source_bindings/,
  );
});

test("canonical Timeline projection does not mistake a deleted Beat occurrence for corruption", () => {
  const timeline = normalizeCanonicalTimelineWorkspace(
    timelineV2WorkspaceWithRepeatedOccurrence({ singleOccurrence: true }),
  );
  const projectPayload = projectWorkspaceWithTimeline();
  projectPayload.coverage.beat_count = 2;
  projectPayload.coverage.selected_assignment_count = 2;
  projectPayload.timeline.head = structuredClone(timeline.head);
  projectPayload.timeline.blueprint_binding = structuredClone(timeline.head.blueprint_binding);
  projectPayload.timeline.coverage_binding = structuredClone(timeline.head.coverage_binding);
  projectPayload.timeline.clip_count = 1;
  projectPayload.timeline.source_binding_count = 1;
  projectPayload.executable.current_timeline = structuredClone(timeline.head);
  const project = normalizeBlueprintWorkspace(projectPayload);

  assert.equal(project.coverage.beat_count, 2);
  assert.equal(project.timeline.clip_count, 1);
  assert.equal(isTimelineWorkspaceProjectionForBlueprint(project, timeline), true);
});

test("canonical export workspace projection is closed and capability-consistent", () => {
  const inProgress = projectWorkspaceWithTimeline();
  inProgress.canonical_export = {
    state: "in_progress",
    reason_code: "export_in_progress",
    latest_job: {
      job_id: "export_job_1",
      status: "packaging",
      package_basename: "grounded-short-film",
    },
  };
  inProgress.capabilities.canonical_export = false;
  const normalized = normalizeBlueprintWorkspace(inProgress);
  assert.equal(normalized.canonical_export.latest_job.status, "packaging");

  const recoveryRequired = projectWorkspaceWithTimeline();
  recoveryRequired.canonical_export = {
    state: "blocked",
    reason_code: "export_recovery_required",
    latest_job: {
      job_id: "export_job_interrupted",
      status: "interrupted",
      package_basename: "grounded-short-film",
    },
  };
  recoveryRequired.capabilities.canonical_export = false;
  assert.equal(
    normalizeBlueprintWorkspace(recoveryRequired).canonical_export.reason_code,
    "export_recovery_required",
  );

  const staleRecoveryRequired = structuredClone(recoveryRequired);
  staleRecoveryRequired.timeline.state = "stale_source_binding";
  staleRecoveryRequired.executable.reason_code = "canonical_timeline_stale_source_binding";
  staleRecoveryRequired.capabilities.timeline_reconciliation = true;
  staleRecoveryRequired.capabilities.timeline_mutation = false;
  assert.equal(
    normalizeBlueprintWorkspace(staleRecoveryRequired).canonical_export.reason_code,
    "export_recovery_required",
  );

  const falseAvailable = structuredClone(recoveryRequired);
  falseAvailable.canonical_export.state = "available";
  falseAvailable.canonical_export.reason_code = "requires_native_confirmation";
  falseAvailable.capabilities.canonical_export = true;
  assert.throws(
    () => normalizeBlueprintWorkspace(falseAvailable),
    /workspace\.canonical_export\.consistency/,
  );

  const leaked = projectWorkspaceWithTimeline();
  leaked.canonical_export.latest_job = {
    job_id: "export_job_1",
    status: "succeeded",
    package_basename: "grounded-short-film",
    canonical_path: "/private/export",
  };
  assert.throws(
    () => normalizeBlueprintWorkspace(leaked),
    /workspace\.canonical_export\.latest_job/,
  );

  const falseCapability = projectWorkspaceWithTimeline();
  falseCapability.capabilities.canonical_export = false;
  assert.throws(
    () => normalizeBlueprintWorkspace(falseCapability),
    /workspace\.canonical_export\.consistency/,
  );

  const staleWithExport = projectWorkspaceWithTimeline();
  staleWithExport.timeline.state = "stale_source_binding";
  staleWithExport.executable.reason_code = "canonical_timeline_stale_source_binding";
  staleWithExport.capabilities.timeline_reconciliation = true;
  staleWithExport.capabilities.timeline_mutation = false;
  assert.throws(
    () => normalizeBlueprintWorkspace(staleWithExport),
    /workspace\.canonical_export\.consistency/,
  );
});

test("stale current-Coverage Timeline exposes only explicit reconciliation", () => {
  const stale = projectWorkspaceWithTimeline();
  stale.timeline.state = "stale_source_binding";
  stale.executable.reason_code = "canonical_timeline_stale_source_binding";
  stale.capabilities.timeline_reconciliation = true;
  stale.capabilities.timeline_mutation = false;
  stale.capabilities.canonical_export = false;
  stale.canonical_export = {
    state: "blocked",
    reason_code: "timeline_stale",
    latest_job: null,
  };
  const normalized = normalizeBlueprintWorkspace(stale);
  assert.equal(normalized.capabilities.timeline_reconciliation, true);
  assert.equal(normalized.capabilities.timeline_mutation, false);

  stale.coverage.state = "stale_evidence";
  stale.capabilities.timeline_reconciliation = false;
  assert.equal(
    normalizeBlueprintWorkspace(stale).capabilities.timeline_reconciliation,
    false,
  );
});

test("canonical Timeline resource rejects locator leakage and clip/source contradictions", () => {
  const leaked = timelineWorkspacePayload();
  leaked.source_bindings[0].path = "/private/library/movie.mp4";
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(leaked),
    /source_bindings\[0\]/,
  );

  const mismatched = timelineWorkspacePayload();
  mismatched.source_bindings[0].clip_id = `clip_${"9".repeat(24)}`;
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(mismatched),
    /source_bindings\[0\]/,
  );

  const inflated = timelineWorkspacePayload();
  inflated.lifecycle.exportable = true;
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(inflated),
    /timeline\.consistency/,
  );
});

test("canonical Timeline source identity may repeat only for the same asset content", () => {
  const shared = timelineWorkspaceWithSharedVideoSource();
  const normalized = normalizeCanonicalTimelineWorkspace(shared);
  assert.deepEqual(
    normalized.source_bindings.map((binding) => binding.asset_source_id),
    [`src_${"4".repeat(24)}`, `src_${"4".repeat(24)}`],
  );

  const conflicting = timelineWorkspaceWithSharedVideoSource();
  const conflictingAssetId = `asset_${"d".repeat(24)}`;
  const conflictingSpanId = `seg_${"d".repeat(24)}_1_1`;
  Object.assign(conflicting.timeline.tracks[0].clips[1], {
    evidence_ref: `memolens://evidence/span/${conflictingSpanId}`,
    asset_id: conflictingAssetId,
    asset_sha256: SHA_D,
    span_id: conflictingSpanId,
    input_asset_sha256: SHA_D,
  });
  Object.assign(conflicting.source_bindings[1], {
    evidence_ref: `memolens://evidence/span/${conflictingSpanId}`,
    asset_id: conflictingAssetId,
    asset_sha256: SHA_D,
    span_id: conflictingSpanId,
    input_asset_sha256: SHA_D,
  });
  assert.throws(
    () => normalizeCanonicalTimelineWorkspace(conflicting),
    /timeline\.source_bindings/,
  );
});

test("Timeline command result is closed and bound to one operation", () => {
  const result = normalizeCanonicalTimelineMaterializeCommandResult(timelineCommandPayload());
  assert.equal(result.operation_id, "timeline_op_1");
  assert.equal(result.result.result_head.timeline_content_sha256, SHA_A);

  const inflated = timelineCommandPayload();
  inflated.result.result_head.path = "/private/timeline.json";
  assert.throws(
    () => normalizeCanonicalTimelineMaterializeCommandResult(inflated),
    /result_head/,
  );
});

test("Timeline reconcile command result is closed and uses the explicit reconcile intent", () => {
  const result = normalizeCanonicalTimelineReconcileCommandResult(
    timelineReconcileCommandPayload(),
  );
  assert.equal(result.command_type, "timeline.reconcile_from_coverage");
  assert.equal(result.result.result_head.revision, 2);

  const inflated = timelineReconcileCommandPayload();
  inflated.result.result_head.destination = "/private/export";
  assert.throws(
    () => normalizeCanonicalTimelineReconcileCommandResult(inflated),
    /result_head/,
  );
});

test("canonical workspace normalization fails closed instead of inventing a brief", () => {
  const payload = workspacePayload();
  payload.current_blueprint.content_sha256 = "not-a-digest";
  assert.throws(
    () => normalizeBlueprintWorkspace(payload),
    /current_blueprint\.content_sha256/,
  );

  const missingSemantic = workspacePayload();
  delete missingSemantic.current_blueprint.blueprint.semantic.script;
  assert.throws(
    () => normalizeBlueprintWorkspace(missingSemantic),
    /blueprint\.semantic/,
  );
});

test("any canonical marker forces strict normalization and never falls back to legacy", async () => {
  const markerPayloads = [
    { canonical_source: "creative_blueprint", id: "proj_1" },
    { workspace_mode: "canonical_blueprint", id: "proj_1" },
    { current_blueprint: null, id: "proj_1" },
    { object: "creative.project_workspace", id: "proj_1" },
    {
      canonical_source: "creative_blueprint",
      project: { id: "proj_1", title: "Legacy-looking nested project" },
    },
  ];
  for (const payload of markerPayloads) {
    assert.equal(hasCanonicalWorkspaceMarker(payload), true);
  }
  await withMockTransport(markerPayloads, async () => {
    for (let index = 0; index < markerPayloads.length; index += 1) {
      await assert.rejects(
        fetchCreativeProject("http://127.0.0.1:5519", "proj_1"),
        (error) => (
          error instanceof VideoApiError
          && error.code === "blueprint_integrity_error"
          && error.status === 502
          && error.retryable === false
        ),
      );
    }
  });
});

test("legacy_brief remains compatible only with the exact legacy object discriminator", async () => {
  const legacy = {
    object: "creative.project",
    schema_version: "1",
    canonical_source: "legacy_brief",
    project_id: "legacy_1",
    title: "Legacy project",
    brief_revision: 1,
    brief: {
      brief_id: "brief_1",
      goal: "Make a legacy film",
      duration_ms: 15_000,
    },
  };
  assert.equal(hasCanonicalWorkspaceMarker(legacy), false);
  await withMockTransport([{ project: legacy, ...legacy }], async () => {
    const project = await fetchCreativeProject(
      "http://127.0.0.1:5519",
      "legacy_1",
    );
    assert.equal(isBlueprintProjectWorkspace(project), false);
    assert.equal(project.id, "legacy_1");
    assert.equal(project.object, "creative.project");
  });

  await withMockTransport(
    [
      { object: "creative.project", canonical_source: "unknown_source", id: "legacy_1" },
      { canonical_source: "legacy_brief", id: "legacy_1" },
      { object: "creative.project", id: "legacy_1", brief: { goal: "marker absent" } },
    ],
    async () => {
      for (let index = 0; index < 3; index += 1) {
        await assert.rejects(
          fetchCreativeProject("http://127.0.0.1:5519", "legacy_1"),
          (error) => error instanceof VideoApiError && error.code === "blueprint_integrity_error",
        );
      }
    },
  );
});

test("closed canonical contract rejects unknown envelope, path, and secret fields", () => {
  const envelope = workspacePayload();
  envelope.secret = "must-not-cross-the-boundary";
  assert.throws(() => normalizeBlueprintWorkspace(envelope), /workspace/);

  const semanticItem = workspacePayload();
  semanticItem.current_blueprint.blueprint.semantic.script.blocks[0].path = "/private/media.mov";
  assert.throws(
    () => normalizeBlueprintWorkspace(semanticItem),
    /semantic\.script\.blocks\[0\]/,
  );

  const authorityItem = workspacePayload();
  authorityItem.history.decision_authority.events[0].secret = "gesture-nonce";
  assert.throws(
    () => normalizeBlueprintWorkspace(authorityItem),
    /decision_authority\.events\[0\]/,
  );

  const binding = workspacePayload();
  binding.current_blueprint.blueprint.semantic.bindings.creator_context = {
    profile_id: "default",
    revision: 1,
    content_sha256: SHA_A,
    path: "/private/profile.json",
  };
  assert.throws(
    () => normalizeBlueprintWorkspace(binding),
    /bindings\.creator_context/,
  );
});

test("authority projection rejects active-confirmation and count contradictions", () => {
  const missingActive = workspacePayload();
  missingActive.current_blueprint.authority_projection.decision_units.script.active_confirmation = null;
  missingActive.history.decision_authority.projection.decision_units.script.active_confirmation = null;
  assert.throws(
    () => normalizeBlueprintWorkspace(missingActive),
    /active_confirmation/,
  );

  const unexpectedActive = workspacePayload();
  const unit = unexpectedActive.current_blueprint.authority_projection.decision_units.intent_goal;
  unit.active_confirmation = {
    event_id: "auth_2",
    confirmed_at: "2026-08-23T00:02:00+00:00",
    observed_revision: 2,
    presentation_sha256: SHA_A,
  };
  unexpectedActive.history.decision_authority.projection.decision_units.intent_goal.active_confirmation = unit.active_confirmation;
  assert.throws(
    () => normalizeBlueprintWorkspace(unexpectedActive),
    /active_confirmation/,
  );

  const wrongCount = workspacePayload();
  wrongCount.current_blueprint.authority_projection.confirmed_decision_unit_count = 2;
  wrongCount.history.decision_authority.projection.confirmed_decision_unit_count = 2;
  assert.throws(
    () => normalizeBlueprintWorkspace(wrongCount),
    /confirmed_decision_unit_count/,
  );
});

test("historical revision authority is bound to its exact revision and content", () => {
  const wrongRevision = structuredClone(workspacePayload().current_blueprint);
  wrongRevision.authority_projection.as_of_revision = wrongRevision.revision + 1;
  assert.throws(
    () => normalizeBlueprintRevisionResource(wrongRevision),
    /blueprint_revision/,
  );

  const wrongContent = structuredClone(workspacePayload().current_blueprint);
  wrongContent.authority_projection.as_of_content_sha256 = SHA_A;
  assert.throws(
    () => normalizeBlueprintRevisionResource(wrongContent),
    /blueprint_revision/,
  );

  const futureConfirmation = structuredClone(workspacePayload().current_blueprint);
  futureConfirmation.authority_projection.decision_units.script.active_confirmation.observed_revision = 3;
  assert.throws(
    () => normalizeBlueprintRevisionResource(futureConfirmation),
    /blueprint_revision/,
  );
});

test("history rejects duplicate members, broken lane order, and over-wide legacy pages", () => {
  const duplicateSection = workspacePayload();
  duplicateSection.history.blueprint.operations[0].changed_sections = ["intent", "intent"];
  assert.throws(
    () => normalizeBlueprintWorkspace(duplicateSection),
    /changed_sections/,
  );

  const wrongOperationOrder = workspacePayload();
  wrongOperationOrder.history.blueprint.operations = [
    wrongOperationOrder.history.blueprint.operations[0],
    {
      ...structuredClone(wrongOperationOrder.history.blueprint.operations[0]),
      operation_id: "op_3",
      sequence: 3,
    },
  ];
  wrongOperationOrder.history.blueprint.available_operation_count = 2;
  wrongOperationOrder.history.blueprint.operations_truncated = false;
  assert.throws(
    () => normalizeBlueprintWorkspace(wrongOperationOrder),
    /blueprint\.operations/,
  );

  const duplicateDecisionUnit = workspacePayload();
  duplicateDecisionUnit.history.decision_authority.events[0].decision_units = ["script", "script"];
  assert.throws(
    () => normalizeBlueprintWorkspace(duplicateDecisionUnit),
    /decision_units/,
  );

  const overWideLegacy = workspacePayload();
  overWideLegacy.history.legacy_artifacts.briefs = Array.from({ length: 51 }, (_, index) => ({
    revision: 100 - index,
    content_sha256: SHA_A,
    created_at: `2026-08-${String(22 - Math.floor(index / 24)).padStart(2, "0")}T${String(index % 24).padStart(2, "0")}:00:00+00:00`,
    role: "migration_context_only",
  }));
  overWideLegacy.history.legacy_artifacts.brief_count = 51;
  overWideLegacy.history.legacy_artifacts.timelines = Array.from({ length: 50 }, (_, index) => ({
    timeline_id: `timeline_${String(index).padStart(3, "0")}`,
    revision: 1,
    parent_revision: null,
    brief_revision: 1,
    schema_version: "1.0",
    content_sha256: SHA_B,
    validation_status: "valid",
    created_at: "2026-08-20T00:00:00+00:00",
    role: "historical_observed_context_only",
  }));
  overWideLegacy.history.legacy_artifacts.timeline_revision_count = 50;
  assert.throws(
    () => normalizeBlueprintWorkspace(overWideLegacy),
    /history\.coverage/,
  );
});

test("latest history windows are anchored to their totals and have no hidden gaps", () => {
  const noChangeHead = workspacePayload();
  noChangeHead.history.blueprint.operations[0] = {
    ...noChangeHead.history.blueprint.operations[0],
    operation_id: "op_3",
    sequence: 3,
    parent_operation_id: "op_2",
    result_kind: "no_change",
    result_revision: 2,
    result_content_sha256: SHA_B,
  };
  noChangeHead.history.blueprint.available_operation_count = 3;
  assert.doesNotThrow(() => normalizeBlueprintWorkspace(noChangeHead));

  const wrongRevisionTotal = workspacePayload();
  wrongRevisionTotal.history.blueprint.available_revision_count = 1;
  assert.throws(
    () => normalizeBlueprintWorkspace(wrongRevisionTotal),
    /blueprint\.coverage/,
  );

  const missingLatestOperation = workspacePayload();
  missingLatestOperation.history.blueprint.operations[0].sequence = 1;
  assert.throws(
    () => normalizeBlueprintWorkspace(missingLatestOperation),
    /blueprint\.coverage/,
  );

  const wrongLatestHead = workspacePayload();
  wrongLatestHead.history.blueprint.operations[0].result_revision = 1;
  assert.throws(
    () => normalizeBlueprintWorkspace(wrongLatestHead),
    /blueprint\.coverage/,
  );

  const splicedOperationParents = workspacePayload();
  splicedOperationParents.history.blueprint.operations = [
    {
      ...structuredClone(splicedOperationParents.history.blueprint.operations[0]),
      parent_operation_id: "op_wrong",
    },
    {
      ...structuredClone(splicedOperationParents.history.blueprint.operations[0]),
      operation_id: "op_1",
      sequence: 1,
      parent_operation_id: null,
      result_revision: 1,
      result_content_sha256: SHA_A,
    },
  ];
  splicedOperationParents.history.blueprint.operations_truncated = false;
  assert.throws(
    () => normalizeBlueprintWorkspace(splicedOperationParents),
    /blueprint\.operations/,
  );

  const missingMiddleOperation = workspacePayload();
  missingMiddleOperation.current_blueprint.revision = 3;
  missingMiddleOperation.current_blueprint.blueprint.revision = 3;
  missingMiddleOperation.current_blueprint.blueprint.parent.revision = 2;
  missingMiddleOperation.current_blueprint.authority_projection.as_of_revision = 3;
  missingMiddleOperation.history.decision_authority.projection.as_of_revision = 3;
  missingMiddleOperation.history.blueprint.available_revision_count = 3;
  missingMiddleOperation.history.blueprint.available_operation_count = 3;
  missingMiddleOperation.history.blueprint.operations = [
    {
      ...structuredClone(missingMiddleOperation.history.blueprint.operations[0]),
      operation_id: "op_3",
      sequence: 3,
      parent_operation_id: "op_2",
      result_revision: 3,
    },
    {
      ...structuredClone(missingMiddleOperation.history.blueprint.operations[0]),
      operation_id: "op_1",
      sequence: 1,
      parent_operation_id: null,
      result_revision: 1,
    },
  ];
  assert.throws(
    () => normalizeBlueprintWorkspace(missingMiddleOperation),
    /blueprint\.operations/,
  );

  const missingLatestAuthority = workspacePayload();
  missingLatestAuthority.history.decision_authority.available_event_count = 2;
  missingLatestAuthority.history.decision_authority.events_truncated = true;
  assert.throws(
    () => normalizeBlueprintWorkspace(missingLatestAuthority),
    /decision_authority\.coverage/,
  );

  const missingMiddleAuthority = workspacePayload();
  missingMiddleAuthority.history.decision_authority.events = [
    structuredClone(missingMiddleAuthority.history.decision_authority.events[0]),
    {
      ...structuredClone(missingMiddleAuthority.history.decision_authority.events[0]),
      event_id: "auth_3",
      sequence: 3,
    },
  ];
  missingMiddleAuthority.history.decision_authority.available_event_count = 3;
  missingMiddleAuthority.history.decision_authority.events_truncated = true;
  assert.throws(
    () => normalizeBlueprintWorkspace(missingMiddleAuthority),
    /decision_authority\.events/,
  );

  const zeroAuthorityWithVisibleEvent = workspacePayload();
  zeroAuthorityWithVisibleEvent.history.decision_authority.available_event_count = 0;
  assert.throws(
    () => normalizeBlueprintWorkspace(zeroAuthorityWithVisibleEvent),
    /decision_authority\.coverage/,
  );
});

test("section comparison is typed and does not treat authority as semantic content", () => {
  const before = semantic();
  const after = semantic("Make an opinionated short film");
  after.script.blocks.push({ block_id: "ending", text: "Now the fragments have a home." });
  const diff = compareBlueprintSemantics(before, after);
  assert.deepEqual(
    diff.filter((entry) => entry.changed).map((entry) => entry.section),
    ["intent", "script"],
  );
  assert.match(diff.find((entry) => entry.section === "intent").after_summary, /Make an opinionated/);
  assert.match(diff.find((entry) => entry.section === "script").after_summary, /2 blocks/);
  assert.equal(diff.some((entry) => entry.section === "authority"), false);
});

test("stale child workspace responses cannot cross scope, database, or project identity", () => {
  const expected = workspacePayload();
  const same = structuredClone(expected);
  assert.equal(canAdoptBlueprintWorkspaceResponse({
    expected,
    candidate: same,
    capturedScopeKey: "library-a",
    currentScopeKey: "library-a",
  }), true);

  const replacedDatabase = structuredClone(expected);
  replacedDatabase.database_uuid = "22222222-3333-4444-8555-666666666666";
  const replacedProject = structuredClone(expected);
  replacedProject.project_id = "proj_2";
  replacedProject.id = "proj_2";
  for (const candidate of [replacedDatabase, replacedProject]) {
    assert.equal(canAdoptBlueprintWorkspaceResponse({
      expected,
      candidate,
      capturedScopeKey: "library-a",
      currentScopeKey: "library-a",
    }), false);
  }
  assert.equal(canAdoptBlueprintWorkspaceResponse({
    expected,
    candidate: same,
    capturedScopeKey: "library-a",
    currentScopeKey: "library-b",
  }), false);
});

test("child request generation rejects a late result after the canonical head changes", () => {
  const captured = normalizeBlueprintWorkspace(workspacePayload());
  const current = structuredClone(captured);
  current.current_blueprint.revision = 3;
  current.current_blueprint.content_sha256 = SHA_A;

  assert.equal(canApplyBlueprintWorkspaceRequest({
    captured,
    current: captured,
    capturedGeneration: 4,
    currentGeneration: 4,
  }), true);
  assert.equal(canApplyBlueprintWorkspaceRequest({
    captured,
    current,
    capturedGeneration: 4,
    currentGeneration: 4,
  }), false);
  assert.equal(canApplyBlueprintWorkspaceRequest({
    captured,
    current: captured,
    capturedGeneration: 4,
    currentGeneration: 5,
  }), false);
});

test("a parent echo of the child's accepted workspace is not an external reset", () => {
  const previousIncoming = normalizeBlueprintWorkspace(workspacePayload());
  const emitted = structuredClone(previousIncoming);
  emitted.current_blueprint.revision = 3;

  assert.equal(classifyBlueprintWorkspaceInput({
    previousIncoming,
    incoming: previousIncoming,
    lastEmitted: emitted,
  }), "unchanged");
  assert.equal(classifyBlueprintWorkspaceInput({
    previousIncoming,
    incoming: emitted,
    lastEmitted: emitted,
  }), "self_echo");
  assert.equal(classifyBlueprintWorkspaceInput({
    previousIncoming,
    incoming: structuredClone(emitted),
    lastEmitted: emitted,
  }), "external");
});

test("workspace API pins database, exact revision, and deterministic restore identity", async () => {
  const payload = workspacePayload();
  await withMockTransport(
    [
      { project: payload },
      { project: payload },
      payload.current_blueprint,
      {
        object: "creative_blueprint.command_result",
        schema_version: "1",
        project_id: "proj_1",
        command_type: "blueprint.restore_revision",
        operation_id: "op_3",
        result: { kind: "revision_restored" },
      },
    ],
    async (requests) => {
      const workspace = await fetchBlueprintWorkspace({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        dbPath: "/db/library.sqlite",
        historyLimit: 25,
      });
      const restoredProject = await fetchCreativeProject(
        "http://127.0.0.1:5519",
        "proj_1",
        "/db/library.sqlite",
      );
      const revision = await fetchBlueprintRevision({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        revision: 2,
        dbPath: "/db/library.sqlite",
      });
      const expectedHead = { revision: 2, content_sha256: SHA_B };
      const restoreFrom = { revision: 1, content_sha256: SHA_A };
      await restoreBlueprintRevision({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        expectedHead,
        restoreFrom,
        expectedDatabaseUuid: payload.database_uuid,
        dbPath: "/db/library.sqlite",
      });

      assert.equal(workspace.current_blueprint.revision, 2);
      assert.equal(isBlueprintProjectWorkspace(restoredProject), true);
      assert.equal(revision.content_sha256, SHA_B);
      assert.equal(
        requests[0].url,
        "http://127.0.0.1:5519/v1/creative/projects/proj_1?db_path=%2Fdb%2Flibrary.sqlite&history_limit=25",
      );
      assert.equal(
        requests[2].url,
        "http://127.0.0.1:5519/v1/creative/projects/proj_1/blueprint?db_path=%2Fdb%2Flibrary.sqlite&revision=2",
      );
      assert.equal(
        requests[3].url,
        `http://127.0.0.1:5519/v1/creative/projects/proj_1/blueprint/restore?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${payload.database_uuid}`,
      );
      assert.deepEqual(JSON.parse(requests[3].init.body), {
        expected_head: expectedHead,
        restore_from: restoreFrom,
      });
      assert.equal(
        requests[3].init.headers["Idempotency-Key"],
        blueprintRestoreIdempotencyKey(payload.database_uuid, expectedHead, restoreFrom),
      );
    },
  );
});

test("Coverage API reads the canonical resource and materializes with exact CAS identity", async () => {
  const coveragePayload = coverageWorkspacePayload();
  const commandPayload = coverageCommandPayload();
  const expectedBlueprint = {
    revision: 2,
    content_sha256: SHA_B,
    semantic_sha256: SHA_A,
  };
  const databaseUuid = coveragePayload.database_uuid;

  await withMockTransport(
    [coveragePayload, commandPayload],
    async (requests) => {
      const coverage = await fetchCoverageWorkspace({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        dbPath: "/db/library.sqlite",
      });
      const result = await materializeCoverageBaseline({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        dbPath: "/db/library.sqlite",
        expectedDatabaseUuid: databaseUuid,
        expectedBlueprint,
        expectedPlanHead: null,
      });

      assert.equal(coverage.coverage_plan.beats[1].gap.code, "evidence_unresolved");
      assert.equal(result.result.result_head.content_sha256, SHA_C);
      assert.equal(
        requests[0].url,
        "http://127.0.0.1:5519/v1/creative/projects/proj_1/coverage?db_path=%2Fdb%2Flibrary.sqlite",
      );
      assert.equal(
        requests[1].url,
        `http://127.0.0.1:5519/v1/creative/projects/proj_1/coverage/materialize?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${databaseUuid}`,
      );
      assert.deepEqual(JSON.parse(requests[1].init.body), {
        expected_blueprint: expectedBlueprint,
        expected_plan_head: null,
      });
      assert.equal(
        requests[1].init.headers["Idempotency-Key"],
        coverageMaterializeIdempotencyKey(databaseUuid, expectedBlueprint, null),
      );
      assert.match(requests[1].init.headers["Idempotency-Key"], /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/);
    },
  );
});

test("Timeline API reads the canonical draft and materializes with exact four-tuple inputs", async () => {
  const timelinePayload = timelineWorkspacePayload();
  const commandPayload = timelineCommandPayload();
  const coverage = normalizeCoverageWorkspace(coverageWorkspacePayload());
  const expectedCoverage = timelineCoverageBindingFromWorkspace(coverage);
  const expectedBlueprint = timelinePayload.timeline.blueprint_binding;
  const databaseUuid = timelinePayload.database_uuid;

  await withMockTransport(
    [timelinePayload, commandPayload],
    async (requests) => {
      const timeline = await fetchCanonicalTimelineWorkspace({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        dbPath: "/db/library.sqlite",
      });
      const result = await materializeCanonicalTimelineFirstCut({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        dbPath: "/db/library.sqlite",
        expectedDatabaseUuid: databaseUuid,
        expectedBlueprint,
        expectedCoverage,
      });

      assert.equal(timeline.timeline.tracks[0].clips[0].media_kind, "image");
      assert.equal(result.result.result_head.revision_sha256, SHA_E);
      assert.equal(
        requests[0].url,
        "http://127.0.0.1:5519/v1/creative/projects/proj_1/timeline?db_path=%2Fdb%2Flibrary.sqlite",
      );
      assert.equal(
        requests[1].url,
        `http://127.0.0.1:5519/v1/creative/projects/proj_1/timeline/materialize?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${databaseUuid}`,
      );
      assert.deepEqual(JSON.parse(requests[1].init.body), {
        expected_blueprint: expectedBlueprint,
        expected_coverage: expectedCoverage,
        expected_timeline_head: null,
      });
      assert.equal(
        requests[1].init.headers["Idempotency-Key"],
        canonicalTimelineMaterializeIdempotencyKey(
          databaseUuid,
          expectedBlueprint,
          expectedCoverage,
        ),
      );
      assert.match(
        requests[1].init.headers["Idempotency-Key"],
        /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/,
      );
    },
  );
});

test("Timeline reconcile API sends one exact stale head and deterministic desktop idempotency", async () => {
  const timelinePayload = timelineWorkspacePayload();
  const commandPayload = timelineReconcileCommandPayload();
  const expectedTimelineHead = timelinePayload.head;
  const expectedBlueprint = timelinePayload.timeline.blueprint_binding;
  const expectedCoverage = timelinePayload.timeline.coverage_binding;
  const databaseUuid = timelinePayload.database_uuid;

  await withMockTransport([commandPayload], async (requests) => {
    const result = await reconcileCanonicalTimelineFromCoverage({
      apiBase: "http://127.0.0.1:5519",
      projectId: "proj_1",
      dbPath: "/db/library.sqlite",
      expectedDatabaseUuid: databaseUuid,
      expectedBlueprint,
      expectedCoverage,
      expectedTimelineHead,
    });
    assert.equal(result.command_type, "timeline.reconcile_from_coverage");
    assert.equal(
      requests[0].url,
      `http://127.0.0.1:5519/v1/creative/projects/proj_1/timeline/reconcile?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${databaseUuid}`,
    );
    assert.deepEqual(JSON.parse(requests[0].init.body), {
      expected_blueprint: expectedBlueprint,
      expected_coverage: expectedCoverage,
      expected_timeline_head: expectedTimelineHead,
    });
    assert.equal(
      requests[0].init.headers["Idempotency-Key"],
      canonicalTimelineReconcileIdempotencyKey(
        databaseUuid,
        expectedCoverage,
        expectedTimelineHead,
      ),
    );
    assert.match(
      requests[0].init.headers["Idempotency-Key"],
      /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/,
    );
  });
});

test("Timeline edit API sends one closed edit with exact upstream and head preconditions", async () => {
  const timelinePayload = timelineWorkspacePayload();
  const expectedTimelineHead = timelinePayload.head;
  const expectedBlueprint = timelinePayload.timeline.blueprint_binding;
  const expectedCoverage = timelinePayload.timeline.coverage_binding;
  const databaseUuid = timelinePayload.database_uuid;
  const edit = {
    op: "trim_clip",
    clip_id: "clip_video",
    source_in_ms: 5_000,
    source_out_ms: 6_000,
  };
  const commandPayload = timelineEditCommandPayload(edit);
  assert.equal(
    isSameCanonicalTimelineHead(
      commandPayload.result.result_head,
      structuredClone(commandPayload.result.result_head),
    ),
    true,
  );
  for (const mutate of [
    (head) => { head.timeline_id = "timeline_other"; },
    (head) => { head.operation_id = "operation_other"; },
  ]) {
    const tampered = structuredClone(commandPayload.result.result_head);
    mutate(tampered);
    assert.equal(
      isSameCanonicalTimelineHead(commandPayload.result.result_head, tampered),
      false,
    );
  }
  const longClipId = "c".repeat(200);
  const [longMoveZero, longMoveOne] = await Promise.all([
    canonicalTimelineEditIdempotencyKey(databaseUuid, expectedTimelineHead, {
      op: "move_clip",
      clip_id: longClipId,
      to_index: 0,
    }),
    canonicalTimelineEditIdempotencyKey(databaseUuid, expectedTimelineHead, {
      op: "move_clip",
      clip_id: longClipId,
      to_index: 1,
    }),
  ]);
  assert.ok(longMoveZero.length <= 200);
  assert.notEqual(longMoveZero, longMoveOne);
  assert.notEqual(
    await canonicalTimelineEditIdempotencyKey(databaseUuid, expectedTimelineHead, {
      op: "replace_clip",
      clip_id: "a-b",
      assignment_id: "c",
    }),
    await canonicalTimelineEditIdempotencyKey(databaseUuid, expectedTimelineHead, {
      op: "replace_clip",
      clip_id: "a",
      assignment_id: "b-c",
    }),
  );

  assert.equal(
    normalizeCanonicalTimelineEditCommandResult(commandPayload).result.edit.op,
    "trim_clip",
  );
  const leaked = structuredClone(commandPayload);
  leaked.result.edit.path = "/private/movie.mp4";
  assert.throws(
    () => normalizeCanonicalTimelineEditCommandResult(leaked),
    /timeline_edit/,
  );

  await withMockTransport([commandPayload], async (requests) => {
    const result = await applyCanonicalTimelineEdit({
      apiBase: "http://127.0.0.1:5519",
      projectId: "proj_1",
      dbPath: "/db/library.sqlite",
      expectedDatabaseUuid: databaseUuid,
      expectedBlueprint,
      expectedCoverage,
      expectedTimelineHead,
      edit,
    });
    assert.equal(result.command_type, "timeline.apply_edit");
    assert.equal(
      requests[0].url,
      `http://127.0.0.1:5519/v1/creative/projects/proj_1/timeline/edit?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${databaseUuid}`,
    );
    assert.deepEqual(JSON.parse(requests[0].init.body), {
      expected_blueprint: expectedBlueprint,
      expected_coverage: expectedCoverage,
      expected_timeline_head: expectedTimelineHead,
      edit,
    });
    assert.equal(
      requests[0].init.headers["Idempotency-Key"],
      await canonicalTimelineEditIdempotencyKey(databaseUuid, expectedTimelineHead, edit),
    );
    assert.match(
      requests[0].init.headers["Idempotency-Key"],
      /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/,
    );
  });
});

test("Timeline structural API is a separate closed action and never widens apply_edit", async () => {
  const timelinePayload = timelineWorkspacePayload();
  const expectedTimelineHead = timelinePayload.head;
  const expectedBlueprint = timelinePayload.timeline.blueprint_binding;
  const expectedCoverage = timelinePayload.timeline.coverage_binding;
  const databaseUuid = timelinePayload.database_uuid;
  const edit = {
    op: "split_clip",
    clip_id: `clip_${"2".repeat(24)}`,
    source_split_ms: 2_000,
  };
  const commandPayload = timelineStructuralEditCommandPayload(edit);

  assert.deepEqual(normalizeCanonicalTimelineStructuralEdit(edit), edit);
  assert.equal(
    normalizeCanonicalTimelineStructuralEditCommandResult(commandPayload).command_type,
    "timeline.apply_structural_edit",
  );
  assert.throws(
    () => normalizeCanonicalTimelineEditCommandResult(commandPayload),
    /timeline_edit_command/,
  );
  for (const invalid of [
    { op: "delete_clip", clip_id: edit.clip_id, source_split_ms: 2_000 },
    { ...edit, source_split_ms: true },
    { op: "move_clip", clip_id: edit.clip_id, to_index: 0 },
  ]) {
    assert.throws(
      () => normalizeCanonicalTimelineStructuralEdit(invalid),
      /timeline_structural_edit/,
    );
  }

  await withMockTransport([commandPayload], async (requests) => {
    await assert.rejects(
      applyCanonicalTimelineEdit({
        apiBase: "http://127.0.0.1:5519",
        projectId: "proj_1",
        dbPath: "/db/library.sqlite",
        expectedDatabaseUuid: databaseUuid,
        expectedBlueprint,
        expectedCoverage,
        expectedTimelineHead,
        edit: { op: "delete_clip", clip_id: edit.clip_id },
      }),
      /timeline_edit\.op/,
    );
    assert.equal(requests.length, 0);
  });

  await withMockTransport([commandPayload], async (requests) => {
    const result = await applyCanonicalTimelineStructuralEdit({
      apiBase: "http://127.0.0.1:5519",
      projectId: "proj_1",
      dbPath: "/db/library.sqlite",
      expectedDatabaseUuid: databaseUuid,
      expectedBlueprint,
      expectedCoverage,
      expectedTimelineHead,
      edit,
    });
    assert.equal(result.command_type, "timeline.apply_structural_edit");
    assert.equal(
      requests[0].url,
      `http://127.0.0.1:5519/v1/creative/projects/proj_1/timeline/structural-edit?db_path=%2Fdb%2Flibrary.sqlite&expected_database_uuid=${databaseUuid}`,
    );
    assert.deepEqual(JSON.parse(requests[0].init.body), {
      expected_blueprint: expectedBlueprint,
      expected_coverage: expectedCoverage,
      expected_timeline_head: expectedTimelineHead,
      structural_edit: edit,
    });
    const expectedKey = await canonicalTimelineStructuralEditIdempotencyKey(
      databaseUuid,
      expectedTimelineHead,
      edit,
    );
    assert.equal(requests[0].init.headers["Idempotency-Key"], expectedKey);
    assert.match(expectedKey, /^timeline-structural-edit-/);
  });
});
