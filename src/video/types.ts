export type MediaKind = "image" | "video" | "audio";
export type CreativeResultType = "image_asset" | "video_segment";
export type MediaJobStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "succeeded"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "interrupted"
  | "blocked_source_unavailable";
export type TimelineTrackType = "video" | "image" | "text" | "audio";
export type TimelineFit = "contain" | "cover" | "stretch";
export type TimelineTransitionType = "none";
export type RenderKind = "preview" | "export";

export interface VideoCapabilityStatus {
  object: string;
  schema_version: string;
  status: string;
  ffmpeg: {
    available: boolean;
    version: string | null;
  };
  ffprobe: {
    available: boolean;
    version: string | null;
  };
  encoder_probe: {
    available: boolean;
    code: string | null;
    message: string | null;
    profiles: string[];
    duration_ms: number | null;
  };
  local_mode: boolean;
  transcription: {
    available: boolean;
    mode: "local" | "external" | "unavailable" | string;
  };
  vision: {
    available: boolean;
    mode: "local" | "external" | "metadata_only" | string;
  };
  supported_inputs: string[];
  supported_output: string[];
  preview_root_id: string | null;
  verified_preview_save_as: boolean;
  message?: string | null;
}

export interface MediaJobError {
  code?: string | null;
  message: string;
  asset_id?: string | null;
  relative_path?: string | null;
  retryable?: boolean;
}

export interface MediaJob {
  object: string;
  schema_version: string;
  id: string;
  kind: string;
  status: MediaJobStatus;
  stage: string;
  progress: number;
  attempt?: number;
  completed_assets?: number;
  total_assets?: number;
  asset_ids?: string[];
  errors: MediaJobError[];
  message?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
}

export interface AssetImportResult {
  object: string;
  schema_version: string;
  id: string;
  status: string;
  job: MediaJob | null;
  jobs: MediaJob[];
  job_id: string | null;
  asset_ids: string[];
  imported: number;
  skipped: number;
  rejected: MediaJobError[];
  message?: string | null;
}

export interface CreativeAssetMatch {
  object: "creative_asset_match" | string;
  schema_version?: string;
  result_type: CreativeResultType;
  id: string;
  asset_id: string;
  asset_source_id: string;
  filename?: string | null;
  title?: string | null;
  start_ms: number | null;
  end_ms: number | null;
  duration_ms?: number | null;
  thumbnail_url: string | null;
  media_url?: string | null;
  summary: string;
  matched_terms: string[];
  score: number;
  confidence: number | null;
  analysis_revision: number | null;
  provenance: string[];
  reasons?: string[];
  warnings?: string[];
}

export interface MixedSearchResponse {
  object: string;
  schema_version: string;
  id: string;
  status: string;
  results: CreativeAssetMatch[];
  candidate_count: number;
  message?: string | null;
  refinement_job?: MediaJob | null;
}

export interface VideoSegmentDetail {
  object: string;
  schema_version: string;
  id: string;
  asset_id: string;
  start_ms: number;
  end_ms: number;
  summary: string;
  media_url: string | null;
  thumbnail_url: string | null;
  keyframes: Array<{
    id: string;
    timestamp_ms: number;
    thumbnail_url: string | null;
    selection_reason: string;
  }>;
  transcript: Array<{
    id: string;
    start_ms: number;
    end_ms: number;
    text: string;
  }>;
  visual_status?: string;
  transcript_status?: string;
}

export interface CreativeBriefInput {
  goal: string;
  audience: string;
  platform: string;
  duration_ms: number;
  aspect_ratio: string;
  tone: string;
  pace: string;
  must_include: string[];
  must_exclude: string[];
  narrative_arc?: string;
  candidate_refs?: string[];
}

export interface CreatorProfileReference {
  profile_id: string;
  revision: number;
  content_sha256: string;
}

export interface CreativeBrief extends CreativeBriefInput {
  id: string;
  revision: number;
  narrative_arc: string;
  candidate_refs: string[];
  missing_assets: string[];
  assumptions: string[];
  creator_profile_ref?: CreatorProfileReference | null;
  applied_profile_fields?: string[];
}

export interface CreativeProject {
  object: string;
  schema_version: string;
  id: string;
  title: string;
  status: string;
  brief: CreativeBrief;
  candidates: CreativeAssetMatch[];
  latest_timeline_id?: string | null;
  latest_timeline_revision?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TimelineTransition {
  type: TimelineTransitionType;
  duration_ms: number;
}

export interface TimelineCrop {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface TimelineClip {
  id: string;
  kind: "video" | "image" | "audio" | "text";
  asset_id: string;
  asset_source_id: string;
  source_id?: string | null;
  segment_id?: string | null;
  source_in_ms?: number | null;
  source_out_ms?: number | null;
  timeline_start_ms: number;
  timeline_duration_ms: number;
  fit?: TimelineFit;
  crop?: TimelineCrop | null;
  volume_db?: number | null;
  transition_in?: TimelineTransition | null;
  transition_out?: TimelineTransition | null;
  text?: string | null;
  provenance: {
    reason: string;
    match_id?: string | null;
    [key: string]: unknown;
  };
}

export interface TimelineTrack {
  id: string;
  type: TimelineTrackType;
  z_index: number;
  muted: boolean;
  clips: TimelineClip[];
}

export interface TimelineTransitionRecord {
  id: string;
  type: "crossfade" | "fade_to_black";
  from_clip_id: string;
  to_clip_id: string | null;
  duration_ms: number;
}

export interface CreativeTimeline {
  object?: string;
  schema_version: "1.0" | string;
  id: string;
  project_id: string;
  revision: number;
  format: {
    width: number;
    height: number;
    fps: 24 | 25 | 30 | 50 | 60 | number;
    sample_rate: number;
    duration_ms: number;
    background_color: string;
  };
  tracks: TimelineTrack[];
  transitions?: TimelineTransitionRecord[];
  provenance: {
    created_by: "user" | "director" | "codex" | string;
    parent_revision: number | null;
    brief_revision: number;
    operations: TimelineOperation[];
    source_analysis_revisions: Record<string, number>;
    created_at: string;
  };
  validation_status?: string;
  content_sha256?: string;
}

export type TimelineOperation =
  | {
      op: "move_clip";
      clip_id: string;
      to_index: number;
    }
  | {
      op: "trim_clip";
      clip_id: string;
      source_in_ms: number;
      source_out_ms: number;
      timeline_duration_ms?: number;
    }
  | {
      op: "delete_clip";
      clip_id: string;
    }
  | {
      op: "replace_clip";
      clip_id: string;
      match_id: string;
    }
  | {
      op: "set_volume";
      clip_id: string;
      volume_db: number;
    }
  | {
      op: "set_transition";
      clip_id: string;
      edge: "in" | "out";
      transition: TimelineTransition;
    }
  | {
      op: "set_format";
      width: number;
      height: number;
      fps: number;
      background_color?: string;
    }
  | {
      op: "set_text";
      clip_id: string;
      text: string;
    }
  | {
      op: "set_clip_duration" | "set_duration";
      clip_id: string;
      timeline_duration_ms: number;
    };

export interface TimelineDiff {
  op: string;
  summary: string;
  clip_id?: string | null;
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
}

export interface TimelineRevisionResponse {
  object: string;
  schema_version: string;
  id: string;
  timeline: CreativeTimeline;
  diff: TimelineDiff[];
  message?: string | null;
}

export interface TimelineInstructionPreview {
  object: string;
  schema_version: string;
  id: string;
  operations: TimelineOperation[];
  diff: TimelineDiff[];
  message?: string | null;
}

export interface TimelineValidationIssue {
  code: string;
  field: string | null;
  message: string;
  severity?: "error" | "warning" | string;
}

export interface TimelineValidation {
  object: string;
  schema_version: string;
  id: string;
  valid: boolean;
  status: string;
  errors: TimelineValidationIssue[];
  warnings: TimelineValidationIssue[];
}

export interface RenderJob {
  object: string;
  schema_version: string;
  id: string;
  kind: RenderKind;
  profile: "preview-low" | "export-1080p" | string;
  status: MediaJobStatus;
  stage: string;
  progress: number;
  timeline_id: string;
  timeline_revision: number;
  output_sha256: string | null;
  size_bytes: number | null;
  download_url: string | null;
  media_url?: string | null;
  filename?: string | null;
  output?: {
    download_url?: string | null;
    media_url?: string | null;
    filename?: string | null;
    output_sha256?: string | null;
    size_bytes?: number | null;
    duration_ms?: number | null;
    width?: number | null;
    height?: number | null;
  } | null;
  error?: {
    code?: string | null;
    message: string;
    retryable?: boolean;
  } | null;
  message?: string | null;
}

export interface DesktopArtifactSaveRequest {
  artifactUrl: string;
  suggestedFilename: string;
  expectedSha256: string;
  expectedSizeBytes: number;
}

export interface DesktopArtifactSaveResult {
  status: "saved" | "cancelled" | "exists" | "failed";
  filename: string | null;
  message: string;
}
