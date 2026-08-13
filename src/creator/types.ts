export interface CreatorProfileContent {
  platform: string;
  audience: string;
  duration_ms: number | null;
  aspect_ratio: string;
  tone: string;
  pace: string;
  narrative_arc: string;
  must_include: string[];
  must_exclude: string[];
}

export type CreatorProfileSource =
  | "user_edit"
  | "confirmed_suggestion"
  | "reset";

export interface CreatorEvidence {
  project_id: string;
  brief_revision: number;
}

export interface CreatorProfileRevision {
  profile_id: string;
  revision: number;
  content_sha256: string | null;
  profile: CreatorProfileContent;
  evidence: CreatorEvidence[];
  source: CreatorProfileSource | null;
  created_at: string | null;
}

export type CreatorProfileField = keyof CreatorProfileContent;
export type CreatorSuggestionValue = string | number | string[];

export interface CreatorProfileSuggestion {
  field: CreatorProfileField;
  value: CreatorSuggestionValue;
  evidence_count: number;
  evidence: CreatorEvidence[];
}
