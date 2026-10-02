import type { BlueprintCoverageBinding, BlueprintCoverageState, BlueprintRevisionRef } from "./workspaceTypes";

export type CoverageFreshnessReason =
  | "blueprint_head_changed"
  | "evidence_head_changed";

export interface CoverageFreshness {
  state: BlueprintCoverageState;
  reasons: CoverageFreshnessReason[];
}

export interface CoverageSelectedRevision extends BlueprintRevisionRef {
  evidence_manifest_sha256: string;
  operation_id: string;
  is_head: boolean;
}

export interface CanonicalImageAnalysisBinding {
  analysis_run_id: string;
  analysis_revision: number;
  analysis_content_sha256: string;
  source_binding_sha256: string;
}

export interface CoverageLegacyAssetProof {
  kind: "asset";
  asset_id: string;
  asset_sha256: string;
}

export interface CoverageCurrentAssetProof extends CoverageLegacyAssetProof, CanonicalImageAnalysisBinding {}

export type CoverageAssetProof = CoverageLegacyAssetProof | CoverageCurrentAssetProof;

export interface CoverageSpanProof {
  kind: "span";
  span_id: string;
  asset_id: string;
  asset_sha256: string;
  analysis_run_id: string;
  analysis_revision: number;
  input_asset_sha256: string;
  start_ms: number;
  end_ms: number;
}

export type CoverageEvidenceProof = CoverageAssetProof | CoverageSpanProof;

export interface CoverageEvidenceManifestItem {
  evidence_ref: string;
  status: "verified" | "unresolved";
  proof_sha256: string | null;
  proof: CoverageEvidenceProof | null;
}

export interface CoverageNeed {
  need_id: string;
  kind: "depiction_unspecified";
  priority: "should";
  allow_empty: true;
}

export interface CoverageAssignment {
  assignment_id: string;
  need_id: string;
  hint_id: string;
  evidence_ref: string;
  match_type: "depiction_unspecified";
  timeline_duration_ms: number;
  reason_sha256: string;
  locked: false;
}

export type CoverageAlternativeReason =
  | "evidence_unresolved"
  | "duplicate_evidence_reserved"
  | "baseline_first_verified_selected"
  | "evidence_span_too_short";

export interface CoverageAlternative extends CoverageAssignment {
  not_selected_reason: CoverageAlternativeReason;
}

export type CoverageGapCode =
  | "no_material_hint"
  | "evidence_unresolved"
  | "duplicate_evidence_reserved"
  | "evidence_span_too_short";

export interface CoverageGap {
  code: CoverageGapCode;
  blocking: false;
}

export interface CoverageBeat {
  beat_id: string;
  script_block_id: string;
  ordinal: number;
  text_sha256: string;
  timing: {
    basis: "estimated_text";
    start_ms: number;
    end_ms: number;
  };
  needs: [CoverageNeed];
  selected_assignments: CoverageAssignment[];
  alternatives: CoverageAlternative[];
  gap: CoverageGap | null;
}

export interface CanonicalCoveragePlan {
  object: "memolens.coverage_plan";
  schema_version: "1";
  project_id: string;
  revision: number;
  parent: BlueprintRevisionRef | null;
  blueprint_binding: BlueprintCoverageBinding;
  compiler: {
    id: "memolens.coverage-baseline/v1";
    timing_capability: "estimated_text";
    semantic_matching: false;
    global_optimization: false;
  };
  output_binding: {
    duration_target_ms: number;
    aspect_ratio: "16:9" | "9:16" | "1:1" | "4:5" | null;
  };
  beats: CoverageBeat[];
  evidence_manifest: CoverageEvidenceManifestItem[];
}

export interface CoverageResultHead extends BlueprintRevisionRef {
  blueprint_revision: number;
  blueprint_content_sha256: string;
  blueprint_semantic_sha256: string;
  operation_id: string;
}

export interface CoverageOperationResult {
  kind: "revision_created";
  result_head: CoverageResultHead;
}

export interface CoverageOperation {
  id: string;
  project_id: string;
  sequence: number;
  parent_operation_id: string | null;
  command_type: "coverage.materialize_baseline";
  command_version: "1";
  effect_class: "reversible_project_write";
  expected_blueprint_revision: number;
  expected_blueprint_content_sha256: string;
  expected_plan_head: BlueprintRevisionRef | null;
  request_sha256: string;
  result_revision: number;
  result_content_sha256: string;
  result: CoverageOperationResult;
  created_at: string;
}

export interface CoveragePlanWorkspace {
  object: "coverage_plan.workspace";
  schema_version: "1";
  database_uuid: string;
  project_id: string;
  head: BlueprintRevisionRef | null;
  selected_revision: CoverageSelectedRevision | null;
  freshness: CoverageFreshness;
  coverage_plan: CanonicalCoveragePlan | null;
  operations: CoverageOperation[];
}

export interface CoverageMaterializeCommandResult {
  object: "coverage_plan.command_result";
  schema_version: "1";
  project_id: string;
  command_type: "coverage.materialize_baseline";
  operation_id: string;
  result: CoverageOperationResult;
}
