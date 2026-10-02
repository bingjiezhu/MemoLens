import type { BlueprintDecisionUnitName } from "./authorityTypes";
import type {
  CanonicalTimelineBlueprintBinding,
  CanonicalTimelineCoverageBinding,
  CanonicalTimelineFreshnessState,
  CanonicalTimelineHead,
  CanonicalTimelineLifecycle,
} from "./timelineTypes";
import type { CanonicalExportWorkspaceSummary } from "./exportTypes";

export type BlueprintSemanticSection =
  | "intent"
  | "script"
  | "direction"
  | "output"
  | "constraints"
  | "material_hints"
  | "reference_refs"
  | "technique_refs"
  | "bindings"
  | "assumptions"
  | "missing_evidence"
  | "open_decisions";

export interface BlueprintRevisionRef {
  revision: number;
  content_sha256: string;
}

export interface BlueprintIntent {
  goal: string | null;
  stance: string | null;
  audience: string | null;
  platform: string | null;
}

export interface BlueprintScriptBlock {
  block_id: string;
  text: string;
}

export interface BlueprintDirection {
  theme: string | null;
  narrative_arc: string | null;
  emotion: string | null;
  tone: string | null;
  pace: string | null;
}

export interface BlueprintOutput {
  duration_target_ms: number | null;
  aspect_ratio: "16:9" | "9:16" | "1:1" | "4:5" | null;
}

export interface BlueprintConstraint {
  constraint_id: string;
  text: string | null;
  evidence_ref: string | null;
  script_block_ids: string[];
}

export interface BlueprintMaterialHint {
  hint_id: string;
  evidence_ref: string;
  script_block_ids: string[];
  reason: string | null;
}

export interface BlueprintReference {
  reference_id: string;
  kind: "project" | "research_snapshot" | "evidence" | "external_https" | "user_text";
  locator: string;
  note: string | null;
}

export interface BlueprintTechniqueReference {
  card_id: string;
  revision: number;
  declared_state: "proposed" | "selected" | "rejected";
}

export interface BlueprintCreatorContextBinding {
  profile_id: string;
  revision: number;
  content_sha256: string;
}

export interface BlueprintWikiGenerationBinding {
  generation_id: string;
  content_sha256: string;
}

export interface BlueprintAssumption {
  assumption_id: string;
  text: string;
}

export type BlueprintRequiredBefore = "coverage" | "timeline" | "render" | "publish" | "not_blocking";

export interface BlueprintMissingEvidence {
  gap_id: string;
  description: string;
  script_block_ids: string[];
  required_before: BlueprintRequiredBefore;
}

export interface BlueprintOpenDecision {
  decision_id: string;
  question: string;
  scope: "intent" | "stance" | "script" | "direction" | "output" | "material" | "reference" | "technique" | "other";
  required_before: BlueprintRequiredBefore;
}

export interface BlueprintSemantic {
  intent: BlueprintIntent;
  script: { blocks: BlueprintScriptBlock[] };
  direction: BlueprintDirection;
  output: BlueprintOutput;
  constraints: {
    must_include: BlueprintConstraint[];
    must_exclude: BlueprintConstraint[];
  };
  material_hints: BlueprintMaterialHint[];
  reference_refs: BlueprintReference[];
  technique_refs: BlueprintTechniqueReference[];
  bindings: {
    creator_context: BlueprintCreatorContextBinding | null;
    wiki_generation: BlueprintWikiGenerationBinding | null;
  };
  assumptions: BlueprintAssumption[];
  missing_evidence: BlueprintMissingEvidence[];
  open_decisions: BlueprintOpenDecision[];
}

export interface BlueprintDocumentAuthorityUnit {
  semantic_sha256: string;
  claim: "agent_proposal";
  verified: false;
}

export interface BlueprintDocumentAuthority {
  state: "unverified";
  decision_units: Record<BlueprintDecisionUnitName, BlueprintDocumentAuthorityUnit>;
}

export interface BlueprintEvidenceManifestItem {
  evidence_ref: string;
  status: "verified" | "unresolved";
  proof_sha256: string | null;
}

export interface CreativeBlueprintDocument {
  object: "memolens.creative_blueprint";
  schema_version: "1";
  schema_sha256: string;
  project_id: string;
  revision: number;
  parent: BlueprintRevisionRef | null;
  created_by_operation_id: string;
  status: "proposal";
  semantic_sha256: string;
  semantic: BlueprintSemantic;
  lineage: {
    compiler: "memolens.blueprint-proposal-compiler/v1";
    source_candidate_sha256: string | null;
    initial_legacy_brief: BlueprintRevisionRef;
  };
  evidence_manifest: BlueprintEvidenceManifestItem[];
  authority: BlueprintDocumentAuthority;
}

export interface BlueprintAuthorityUnitProjection {
  semantic_sha256: string;
  state: "confirmed" | "unverified";
  confirmed: boolean;
  active_confirmation: {
    event_id: string;
    confirmed_at: string;
    observed_revision: number;
    presentation_sha256: string;
  } | null;
}

export interface BlueprintAuthorityProjection {
  state: "unverified" | "partially_confirmed" | "confirmed";
  confirmed_decision_unit_count: number;
  decision_unit_count: 8;
  as_of_revision: number;
  as_of_content_sha256: string;
  decision_units: Record<BlueprintDecisionUnitName, BlueprintAuthorityUnitProjection>;
}

export interface BlueprintCurrentRevision {
  object: "creative_blueprint.revision";
  schema_version: "1";
  database_uuid: string;
  project_id: string;
  revision: number;
  content_sha256: string;
  semantic_sha256: string;
  operation_id: string;
  current: boolean;
  blueprint: CreativeBlueprintDocument;
  authority: { state: "unverified"; verified: false };
  creation_authority: { state: "unverified"; verified: false };
  authority_projection: BlueprintAuthorityProjection;
}

export interface BlueprintOperationSummary {
  operation_id: string;
  sequence: number;
  parent_operation_id: string | null;
  command_type: "blueprint.commit_proposal" | "blueprint.restore_revision";
  intent_code: "propose_blueprint" | "restore_blueprint";
  result_kind: "revision_created" | "revision_restored" | "no_change";
  result_revision: number;
  result_content_sha256: string;
  changed_sections: BlueprintSemanticSection[];
  restore_from: BlueprintRevisionRef | null;
  input_sha256: string;
  output_sha256: string;
  created_at: string;
}

export interface BlueprintAuthorityEventSummary {
  event_id: string;
  sequence: number;
  authority_operation: "confirm" | "revoke";
  observed_revision: number;
  decision_units: BlueprintDecisionUnitName[];
  created_at: string;
}

export interface LegacyBriefSummary extends BlueprintRevisionRef {
  created_at: string;
  role: "migration_context_only";
}

export interface LegacyTimelineSummary {
  timeline_id: string;
  revision: number;
  parent_revision: number | null;
  brief_revision: number;
  schema_version: "1.0";
  content_sha256: string;
  validation_status: "valid" | "invalid";
  created_at: string;
  role: "historical_observed_context_only";
}

export type BlueprintCoverageState =
  | "missing"
  | "current"
  | "stale_blueprint"
  | "stale_evidence";

export interface BlueprintCoverageBinding extends BlueprintRevisionRef {
  semantic_sha256: string;
}

export interface BlueprintCoverageSummary {
  state: BlueprintCoverageState;
  head: BlueprintRevisionRef | null;
  blueprint_binding: BlueprintCoverageBinding | null;
  beat_count: number;
  selected_assignment_count: number;
  gap_count: number;
}

export type BlueprintExecutableReasonCode =
  | "coverage_plan_missing"
  | "coverage_plan_stale_blueprint"
  | "coverage_plan_stale_evidence"
  | "coverage_not_lowerable"
  | "canonical_timeline_materialization_available"
  | "canonical_timeline_current"
  | "canonical_timeline_stale_blueprint"
  | "canonical_timeline_stale_coverage"
  | "canonical_timeline_stale_source_binding";

export interface BlueprintTimelineSummary {
  state: CanonicalTimelineFreshnessState;
  head: CanonicalTimelineHead | null;
  blueprint_binding: CanonicalTimelineBlueprintBinding | null;
  coverage_binding: CanonicalTimelineCoverageBinding | null;
  clip_count: number;
  source_binding_count: number;
  lifecycle: CanonicalTimelineLifecycle;
}

export interface BlueprintProjectWorkspace {
  object: "creative.project_workspace";
  schema_version: "1";
  database_uuid: string;
  id: string;
  project_id: string;
  title: string;
  status: "draft" | "active" | "archived";
  canonical_source: "creative_blueprint";
  workspace_mode: "canonical_blueprint";
  current_blueprint: BlueprintCurrentRevision;
  coverage: BlueprintCoverageSummary;
  timeline: BlueprintTimelineSummary;
  canonical_export: CanonicalExportWorkspaceSummary;
  executable: {
    state: "not_compiled" | "compiled_draft";
    current_timeline: CanonicalTimelineHead | null;
    reason_code: BlueprintExecutableReasonCode;
  };
  history: {
    complete_project_history: false;
    global_total_order: false;
    replay_scope: "per_lane_only";
    blueprint: {
      lane: "semantic_changes";
      operations: BlueprintOperationSummary[];
      available_revision_count: number;
      available_operation_count: number;
      operations_truncated: boolean;
      ledger: {
        coverage_scope: "creative_blueprint";
        complete_project_history: false;
      };
      order: "sequence_desc";
    };
    decision_authority: {
      lane: "decision_authority";
      events: BlueprintAuthorityEventSummary[];
      available_event_count: number;
      events_truncated: boolean;
      projection: BlueprintAuthorityProjection;
      coverage_scope: "blueprint_decision_authority";
      complete_project_history: false;
      order: "sequence_asc";
    };
    legacy_artifacts: {
      lane: "legacy_artifacts";
      status: "available" | "unavailable";
      reason_code: "legacy_artifact_history_integrity_error" | null;
      role: "migration_context_only";
      coverage_scope: "legacy_briefs_and_timelines";
      order: "brief_revision_desc_and_timeline_created_at_desc_id_asc_revision_desc";
      authoritative_timeline_head: false;
      briefs: LegacyBriefSummary[];
      brief_count: number;
      briefs_truncated: boolean;
      timelines: LegacyTimelineSummary[];
      timeline_revision_count: number;
      timelines_truncated: boolean;
    };
  };
  capabilities: {
    complete_project_history: false;
    authoritative_timeline_head: boolean;
    coverage_plan_materialization: true;
    blueprint_timeline_compiler: true;
    timeline_materialization: boolean;
    timeline_reconciliation: boolean;
    timeline_mutation: boolean;
    preview: false;
    render: false;
    export: false;
    canonical_export: boolean;
  };
  created_at: string;
  updated_at: string;
}

export interface BlueprintSectionDiff {
  section: BlueprintSemanticSection;
  changed: boolean;
  before_summary: string;
  after_summary: string;
}
