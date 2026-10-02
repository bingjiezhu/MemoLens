export const CANONICAL_EXPORT_PACKAGE_ROLES = [
  "final_video",
  "script",
  "package_manifest",
  "human_usage_list",
  "completion_marker",
] as const;

export type CanonicalExportPackageRole = typeof CANONICAL_EXPORT_PACKAGE_ROLES[number];

export const CANONICAL_EXPORT_JOB_STATUSES = [
  "requested",
  "rendering",
  "packaging",
  "commit_pending",
  "succeeded",
  "failed",
  "cancelling",
  "cancelled",
  "interrupted",
] as const;

export type CanonicalExportJobStatus = typeof CANONICAL_EXPORT_JOB_STATUSES[number];

export interface CanonicalExportBlueprintBinding {
  revision: number;
  content_sha256: string;
  semantic_sha256: string;
  operation_id: string;
}

export interface CanonicalExportCoverageBinding {
  revision: number;
  content_sha256: string;
  evidence_manifest_sha256: string;
  operation_id: string;
}

export interface CanonicalExportTimelineBinding {
  revision: number;
  revision_sha256: string;
  timeline_id: string;
  timeline_content_sha256: string;
  source_bindings_sha256: string;
  blueprint_binding: CanonicalExportBlueprintBinding;
  coverage_binding: CanonicalExportCoverageBinding;
}

export interface CanonicalExportPresentation {
  object: "canonical_export.presentation";
  schema_version: "1";
  database_uuid: string;
  project_id: string;
  timeline_binding: CanonicalExportTimelineBinding;
  profile: "export-1080p";
  package_roles: CanonicalExportPackageRole[];
  duration_ms: number;
  aspect_ratio: "16:9" | "9:16" | "1:1" | "4:5";
  clip_count: number;
}

export interface CanonicalExportPresentationEnvelope {
  object: "canonical_export.presentation_envelope";
  schema_version: "1";
  database_uuid: string;
  presentation: CanonicalExportPresentation;
  presentation_sha256: string;
}

export interface DesktopCanonicalExportExpectedTimelineBinding {
  revision: number;
  revisionSha256: string;
  timelineId: string;
  timelineContentSha256: string;
  sourceBindingsSha256: string;
}

export interface CanonicalExportJobTimelineBinding {
  revision: number;
  revision_sha256: string;
  timeline_content_sha256: string;
  source_bindings_sha256: string;
}

export interface CanonicalExportJobOutputBinding {
  output_root_id: string;
  permission_fingerprint: string;
  package_basename: string;
  profile: "export-1080p";
}

export interface CanonicalExportJobResultProof {
  revision: number;
  revision_sha256: string;
  video_sha256: string;
  video_size_bytes: number;
  video_duration_ms: number;
  package_manifest_sha256: string;
  usage_sha256: string;
  runtime_manifest_sha256: string;
  human_usage_sha256: string;
  completion_marker_sha256: string;
  completed_at: string;
}

export interface CanonicalExportJobError {
  code: string;
  message: string;
  retryable: boolean;
}

export interface CanonicalExportJob {
  id: string;
  project_id: string;
  operation_id: string;
  presentation_sha256: string;
  request_sha256: string;
  timeline_binding: CanonicalExportJobTimelineBinding;
  output_binding: CanonicalExportJobOutputBinding;
  status: CanonicalExportJobStatus;
  stage: string;
  progress: number;
  attempt: number;
  cancel_requested: boolean;
  error: CanonicalExportJobError | null;
  created_at: string;
  started_at: string | null;
  heartbeat_at: string | null;
  finished_at: string | null;
  result: CanonicalExportJobResultProof | null;
}

export interface CanonicalExportCommandResult {
  object: "canonical_export.command_result";
  schema_version: "1";
  project_id: string;
  operation_id: string;
  job: CanonicalExportJob;
}

export interface DesktopCanonicalExportApprovalRequest {
  projectId: string;
  databaseUuid: string;
  expectedTimelineBinding: DesktopCanonicalExportExpectedTimelineBinding;
  suggestedPackageName: string;
}

export interface DesktopCanonicalExportJobSummary {
  jobId: string;
  status: CanonicalExportJobStatus;
  packageBasename: string;
}

export type DesktopCanonicalExportApprovalResult =
  | {
    status: "submitted";
    message: string;
    job: DesktopCanonicalExportJobSummary;
  }
  | {
    status: "cancelled" | "failed" | "unknown";
    message: string;
    job: null;
  };

export type CanonicalExportWorkspaceState = "blocked" | "available" | "in_progress";

export type CanonicalExportWorkspaceReason =
  | "timeline_missing"
  | "timeline_stale"
  | "export_recovery_required"
  | "requires_native_confirmation"
  | "export_in_progress";

export interface CanonicalExportWorkspaceJobSummary {
  job_id: string;
  status: CanonicalExportJobStatus;
  package_basename: string;
}

export interface CanonicalExportWorkspaceSummary {
  state: CanonicalExportWorkspaceState;
  reason_code: CanonicalExportWorkspaceReason;
  latest_job: CanonicalExportWorkspaceJobSummary | null;
}
