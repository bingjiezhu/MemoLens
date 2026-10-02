export interface CanonicalTimelineRevisionRef {
  revision: number;
  content_sha256: string;
}

export interface CanonicalTimelineBlueprintBinding extends CanonicalTimelineRevisionRef {
  semantic_sha256: string;
  operation_id: string;
}

export interface CanonicalTimelineCoverageBinding extends CanonicalTimelineRevisionRef {
  evidence_manifest_sha256: string;
  operation_id: string;
}

export interface CanonicalTimelineHead {
  revision: number;
  revision_sha256: string;
  timeline_id: string;
  timeline_content_sha256: string;
  blueprint_binding: CanonicalTimelineBlueprintBinding;
  coverage_binding: CanonicalTimelineCoverageBinding;
  operation_id: string;
}

export interface CanonicalTimelineSelectedRevision extends CanonicalTimelineHead {
  is_head: boolean;
}

export type CanonicalTimelineSchemaVersion = "1" | "2";

export type CanonicalTimelineRestoreFrom = Omit<
  CanonicalTimelineSelectedRevision,
  "is_head"
>;

export type CanonicalTimelineFreshnessState =
  | "missing"
  | "current"
  | "stale_blueprint"
  | "stale_coverage"
  | "stale_source_binding";

export type CanonicalTimelineFreshnessReason =
  | "blueprint_head_changed"
  | "coverage_head_changed"
  | "coverage_plan_stale_blueprint"
  | "coverage_plan_stale_evidence"
  | "fixed_source_binding_changed";

export interface CanonicalTimelineFreshness {
  state: CanonicalTimelineFreshnessState;
  reasons: CanonicalTimelineFreshnessReason[];
}

interface CanonicalTimelineClipBase {
  clip_id: string;
  ordinal: number;
  beat_id: string;
  assignment_id: string;
  evidence_ref: string;
  asset_id: string;
  asset_sha256: string;
  start_ms: number;
  end_ms: number;
  fit: "cover";
  audio_enabled: false;
}

export interface CanonicalTimelineLegacyImageClip extends CanonicalTimelineClipBase {
  media_kind: "image";
}

export interface CanonicalTimelineCurrentImageClip
  extends CanonicalTimelineLegacyImageClip, CanonicalImageAnalysisBinding {}

export type CanonicalTimelineImageClip =
  | CanonicalTimelineLegacyImageClip
  | CanonicalTimelineCurrentImageClip;

interface CanonicalTimelineVideoClipBase extends CanonicalTimelineClipBase {
  media_kind: "video";
  span_id: string;
  analysis_run_id: string;
  analysis_revision: number;
  input_asset_sha256: string;
  source_in_ms: number;
  source_out_ms: number;
}

export interface CanonicalTimelineOrdinaryVideoClip
  extends CanonicalTimelineVideoClipBase {}

export interface CanonicalTimelineResidualVideoClip
  extends CanonicalTimelineVideoClipBase {
  residual_id: string;
  parent_segment_id: string;
  residual_binding: ResidualBinding;
}

export type CanonicalTimelineVideoClip =
  | CanonicalTimelineOrdinaryVideoClip
  | CanonicalTimelineResidualVideoClip;

export type CanonicalTimelineClip =
  | CanonicalTimelineImageClip
  | CanonicalTimelineVideoClip;

export interface CanonicalTimelineDocument {
  object: "memolens.canonical_timeline";
  schema_version: CanonicalTimelineSchemaVersion;
  timeline_id: string;
  project_id: string;
  revision: number;
  parent: CanonicalTimelineRevisionRef | null;
  blueprint_binding: CanonicalTimelineBlueprintBinding;
  coverage_binding: CanonicalTimelineCoverageBinding;
  compiler: {
    id: "memolens.timeline-lowerer/v1";
    media: "image_and_video_span";
    transitions: "hard_cut";
    audio: "silent";
    subtitles: "none";
  };
  output: {
    duration_ms: number;
    aspect_ratio: "16:9" | "9:16" | "1:1" | "4:5";
  };
  tracks: [{
    track_id: string;
    kind: "primary_visual";
    clips: CanonicalTimelineClip[];
  }];
}

interface CanonicalTimelineSourceBindingBase {
  clip_id: string;
  evidence_ref: string;
  asset_id: string;
  asset_sha256: string;
  asset_source_id: string;
  coverage_proof_sha256: string;
}

export interface CanonicalTimelineLegacyImageSourceBinding
  extends CanonicalTimelineSourceBindingBase {
  media_kind: "image";
}

export interface CanonicalTimelineCurrentImageSourceBinding
  extends CanonicalTimelineLegacyImageSourceBinding, CanonicalImageAnalysisBinding {}

export type CanonicalTimelineImageSourceBinding =
  | CanonicalTimelineLegacyImageSourceBinding
  | CanonicalTimelineCurrentImageSourceBinding;

interface CanonicalTimelineVideoSourceBindingBase
  extends CanonicalTimelineSourceBindingBase {
  media_kind: "video";
  span_id: string;
  analysis_run_id: string;
  analysis_revision: number;
  input_asset_sha256: string;
  source_in_ms: number;
  source_out_ms: number;
}

export interface CanonicalTimelineOrdinaryVideoSourceBinding
  extends CanonicalTimelineVideoSourceBindingBase {}

export interface CanonicalTimelineResidualVideoSourceBinding
  extends CanonicalTimelineVideoSourceBindingBase {
  residual_id: string;
  parent_segment_id: string;
  residual_binding: ResidualBinding;
}

export type CanonicalTimelineVideoSourceBinding =
  | CanonicalTimelineOrdinaryVideoSourceBinding
  | CanonicalTimelineResidualVideoSourceBinding;

export type CanonicalTimelineSourceBinding =
  | CanonicalTimelineImageSourceBinding
  | CanonicalTimelineVideoSourceBinding;

export interface CanonicalTimelineLifecycle {
  state: "not_materialized" | "draft";
  approval: "not_established";
  exportable: false;
}

export interface CanonicalTimelineWorkspace {
  object: "canonical_timeline.workspace";
  schema_version: "1";
  database_uuid: string;
  project_id: string;
  head: CanonicalTimelineHead | null;
  selected_revision: CanonicalTimelineSelectedRevision | null;
  freshness: CanonicalTimelineFreshness;
  timeline: CanonicalTimelineDocument | null;
  source_bindings: CanonicalTimelineSourceBinding[];
  lifecycle: CanonicalTimelineLifecycle;
}

/**
 * A deliberately non-canonical view model for one unsaved local edit effect.
 * It cannot be passed anywhere that requires a canonical workspace/head.
 */
export interface CanonicalTimelinePendingPreview {
  object: "canonical_timeline.pending_preview";
  schema_version: "1";
  database_uuid: string;
  project_id: string;
  based_on_head: CanonicalTimelineHead;
  timeline: CanonicalTimelineDocument;
  source_bindings: CanonicalTimelineSourceBinding[];
}

export interface CanonicalTimelineOperationResult {
  kind: "revision_created";
  result_head: CanonicalTimelineHead;
}

export interface CanonicalTimelineMaterializeCommandResult {
  object: "canonical_timeline.command_result";
  schema_version: "1";
  project_id: string;
  command_type: "timeline.materialize_first_cut";
  operation_id: string;
  result: CanonicalTimelineOperationResult;
}

export interface CanonicalTimelineReconcileCommandResult {
  object: "canonical_timeline.command_result";
  schema_version: "1";
  project_id: string;
  command_type: "timeline.reconcile_from_coverage";
  operation_id: string;
  result: CanonicalTimelineOperationResult;
}

export interface CanonicalTimelineMoveClipEdit {
  op: "move_clip";
  clip_id: string;
  to_index: number;
}

export interface CanonicalTimelineTrimClipEdit {
  op: "trim_clip";
  clip_id: string;
  source_in_ms: number;
  source_out_ms: number;
}

export interface CanonicalTimelineSetClipDurationEdit {
  op: "set_clip_duration";
  clip_id: string;
  duration_ms: number;
}

export interface CanonicalTimelineReplaceClipEdit {
  op: "replace_clip";
  clip_id: string;
  assignment_id: string;
}

export type CanonicalTimelineEdit =
  | CanonicalTimelineMoveClipEdit
  | CanonicalTimelineTrimClipEdit
  | CanonicalTimelineSetClipDurationEdit
  | CanonicalTimelineReplaceClipEdit;

export interface CanonicalTimelineSplitClipEdit {
  op: "split_clip";
  clip_id: string;
  source_split_ms: number;
}

export interface CanonicalTimelineDeleteClipEdit {
  op: "delete_clip";
  clip_id: string;
}

/**
 * A separately authorized structural dialect. It must never be widened into
 * CanonicalTimelineEdit, whose existing capability is timeline.apply_edit.
 */
export type CanonicalTimelineStructuralEdit =
  | CanonicalTimelineSplitClipEdit
  | CanonicalTimelineDeleteClipEdit;

export interface CanonicalTimelineEditOperationResult
  extends CanonicalTimelineOperationResult {
  edit: CanonicalTimelineEdit;
}

export interface CanonicalTimelineEditCommandResult {
  object: "canonical_timeline.command_result";
  schema_version: "1";
  project_id: string;
  command_type: "timeline.apply_edit";
  operation_id: string;
  result: CanonicalTimelineEditOperationResult;
}

export interface CanonicalTimelineStructuralEditOperationResult
  extends CanonicalTimelineOperationResult {
  structural_edit: CanonicalTimelineStructuralEdit;
}

export interface CanonicalTimelineStructuralEditCommandResult {
  object: "canonical_timeline.command_result";
  schema_version: "1";
  project_id: string;
  command_type: "timeline.apply_structural_edit";
  operation_id: string;
  result: CanonicalTimelineStructuralEditOperationResult;
}

export interface CanonicalTimelineRestoreOperationResult {
  kind: "revision_restored";
  result_head: CanonicalTimelineHead;
  restore_from: CanonicalTimelineRestoreFrom;
}

export interface CanonicalTimelineRestoreCommandResult {
  object: "canonical_timeline.command_result";
  schema_version: "1";
  project_id: string;
  command_type: "timeline.restore_revision";
  operation_id: string;
  result: CanonicalTimelineRestoreOperationResult;
}
import type { ResidualBinding } from "../video/types.js";
import type { CanonicalImageAnalysisBinding } from "./coverageTypes";
