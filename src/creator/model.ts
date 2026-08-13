import type { CreativeBriefInput } from "../video/types";
import type {
  CreatorEvidence,
  CreatorProfileContent,
  CreatorProfileField,
  CreatorProfileRevision,
  CreatorProfileSource,
  CreatorProfileSuggestion,
  CreatorSuggestionValue,
} from "./types";

export const EMPTY_CREATOR_PROFILE: CreatorProfileContent = {
  platform: "",
  audience: "",
  duration_ms: null,
  aspect_ratio: "",
  tone: "",
  pace: "",
  narrative_arc: "",
  must_include: [],
  must_exclude: [],
};

export const CREATOR_DURATION_MIN_SECONDS = 1;
export const CREATOR_DURATION_MAX_SECONDS = 1_800;

const PROFILE_FIELDS = new Set<CreatorProfileField>([
  "platform",
  "audience",
  "duration_ms",
  "aspect_ratio",
  "tone",
  "pace",
  "narrative_arc",
  "must_include",
  "must_exclude",
]);

const PROFILE_FIELD_LABELS: Record<CreatorProfileField, string> = {
  platform: "Platform",
  audience: "Audience",
  duration_ms: "Length",
  aspect_ratio: "Format",
  tone: "Tone",
  pace: "Pace",
  narrative_arc: "Story rhythm",
  must_include: "Include",
  must_exclude: "Avoid",
};

export function creatorProfileFieldLabel(field: CreatorProfileField): string {
  return PROFILE_FIELD_LABELS[field];
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

function nullableText(value: unknown): string | null {
  const normalized = text(value);
  return normalized || null;
}

function positiveInteger(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? Math.round(numeric) : null;
}

function creatorDurationMilliseconds(value: unknown): number | null {
  const numeric = Number(value);
  return Number.isInteger(numeric)
    && numeric >= CREATOR_DURATION_MIN_SECONDS * 1_000
    && numeric <= CREATOR_DURATION_MAX_SECONDS * 1_000
    ? numeric
    : null;
}

export function creatorDurationSecondsToMilliseconds(value: unknown): number | null {
  if (typeof value === "string" && !value.trim()) return null;
  const seconds = Number(value);
  return Number.isInteger(seconds)
    && seconds >= CREATOR_DURATION_MIN_SECONDS
    && seconds <= CREATOR_DURATION_MAX_SECONDS
    ? seconds * 1_000
    : null;
}

export function uniqueTerms(value: unknown): string[] {
  const values = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split(/[,;，；\n]/)
      : [];
  return values
    .map(text)
    .filter((term, index, terms) => Boolean(term) && terms.indexOf(term) === index);
}

export function creatorProfileDraftIsDirty(input: {
  draft: CreatorProfileContent;
  durationText: string;
  includeText: string;
  excludeText: string;
  persistedProfile: CreatorProfileContent | null;
}): boolean {
  const durationMs = creatorDurationSecondsToMilliseconds(input.durationText);
  if (input.durationText.trim() && durationMs === null) return true;

  const editableProfile = normalizeCreatorProfileContent({
    ...input.draft,
    duration_ms: durationMs,
    must_include: uniqueTerms(input.includeText),
    must_exclude: uniqueTerms(input.excludeText),
  });
  const persistedProfile = normalizeCreatorProfileContent(
    input.persistedProfile ?? EMPTY_CREATOR_PROFILE,
  );
  return JSON.stringify(editableProfile) !== JSON.stringify(persistedProfile);
}

export function normalizeCreatorProfileContent(value: unknown): CreatorProfileContent {
  const raw = record(value);
  return {
    platform: text(raw.platform),
    audience: text(raw.audience),
    duration_ms: creatorDurationMilliseconds(raw.duration_ms),
    aspect_ratio: text(raw.aspect_ratio),
    tone: text(raw.tone),
    pace: text(raw.pace),
    narrative_arc: text(raw.narrative_arc),
    must_include: uniqueTerms(raw.must_include),
    must_exclude: uniqueTerms(raw.must_exclude),
  };
}

function normalizeEvidence(value: unknown): CreatorEvidence[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const raw = record(entry);
    const projectId = text(raw.project_id);
    const briefRevision = positiveInteger(raw.brief_revision);
    return projectId && briefRevision
      ? [{ project_id: projectId, brief_revision: briefRevision }]
      : [];
  });
}

function normalizeSource(value: unknown): CreatorProfileSource | null {
  return value === "user_edit" || value === "confirmed_suggestion" || value === "reset"
    ? value
    : null;
}

export function normalizeCreatorProfileRevision(value: unknown): CreatorProfileRevision {
  const envelope = record(value);
  const raw = record(envelope.profile);
  const nestedProfile = Object.prototype.hasOwnProperty.call(raw, "profile")
    ? raw.profile
    : raw;
  return {
    profile_id: text(raw.profile_id) || "default",
    revision: Math.max(0, positiveInteger(raw.revision) ?? 0),
    content_sha256: nullableText(raw.content_sha256),
    profile: normalizeCreatorProfileContent(nestedProfile),
    evidence: normalizeEvidence(raw.evidence),
    source: normalizeSource(raw.source),
    created_at: nullableText(raw.created_at),
  };
}

function normalizeSuggestionValue(
  field: CreatorProfileField,
  value: unknown,
): CreatorSuggestionValue | null {
  if (field === "duration_ms") return creatorDurationMilliseconds(value);
  if (field === "must_include" || field === "must_exclude") {
    const terms = uniqueTerms(value);
    return terms.length > 0 ? terms : null;
  }
  return nullableText(value);
}

export function normalizeCreatorSuggestions(value: unknown): CreatorProfileSuggestion[] {
  const envelope = record(value);
  const entries = Array.isArray(envelope.data)
    ? envelope.data
    : Array.isArray(envelope.suggestions)
      ? envelope.suggestions
      : [];
  return entries.flatMap((entry) => {
    const raw = record(entry);
    const field = text(raw.field) as CreatorProfileField;
    if (!PROFILE_FIELDS.has(field)) return [];
    const suggestionValue = normalizeSuggestionValue(field, raw.value);
    const evidenceCount = Math.max(0, positiveInteger(raw.evidence_count) ?? 0);
    if (suggestionValue === null || evidenceCount < 2) return [];
    return [{
      field,
      value: suggestionValue,
      evidence_count: evidenceCount,
      evidence: normalizeEvidence(raw.evidence),
    }];
  });
}

export function countCreatorPreferences(profile: CreatorProfileContent | null): number {
  if (!profile) return 0;
  return [
    profile.platform,
    profile.audience,
    profile.duration_ms,
    profile.aspect_ratio,
    profile.tone,
    profile.pace,
    profile.narrative_arc,
    profile.must_include.length > 0 ? profile.must_include : null,
    profile.must_exclude.length > 0 ? profile.must_exclude : null,
  ].filter((value) => value !== null && value !== "").length;
}

export function activeCreatorPreferenceFields(
  profile: CreatorProfileContent | null,
): CreatorProfileField[] {
  if (!profile) return [];
  return (Object.keys(profile) as CreatorProfileField[]).filter((field) => {
    const value = profile[field];
    return Array.isArray(value) ? value.length > 0 : value !== null && value !== "";
  });
}

export function filterCreatorProfile(
  profile: CreatorProfileContent | null,
  fields: CreatorProfileField[],
): CreatorProfileContent | null {
  if (!profile) return null;
  const selected = new Set(fields);
  return {
    platform: selected.has("platform") ? profile.platform : "",
    audience: selected.has("audience") ? profile.audience : "",
    duration_ms: selected.has("duration_ms") ? profile.duration_ms : null,
    aspect_ratio: selected.has("aspect_ratio") ? profile.aspect_ratio : "",
    tone: selected.has("tone") ? profile.tone : "",
    pace: selected.has("pace") ? profile.pace : "",
    narrative_arc: selected.has("narrative_arc") ? profile.narrative_arc : "",
    must_include: selected.has("must_include") ? [...profile.must_include] : [],
    must_exclude: selected.has("must_exclude") ? [...profile.must_exclude] : [],
  };
}

export function creatorProfilePromptContext(profile: CreatorProfileContent | null): string {
  if (!profile || countCreatorPreferences(profile) === 0) return "";
  const parts = [
    profile.platform ? `platform ${profile.platform}` : null,
    profile.audience ? `audience ${profile.audience}` : null,
    profile.duration_ms ? `target ${Math.round(profile.duration_ms / 1000)} seconds` : null,
    profile.aspect_ratio ? `frame ${profile.aspect_ratio}` : null,
    profile.tone ? `tone ${profile.tone}` : null,
    profile.pace ? `pace ${profile.pace}` : null,
    profile.narrative_arc ? `arc ${profile.narrative_arc}` : null,
    profile.must_include.length > 0 ? `include ${profile.must_include.join(", ")}` : null,
    profile.must_exclude.length > 0 ? `exclude ${profile.must_exclude.join(", ")}` : null,
  ].filter((part): part is string => Boolean(part));
  return `Confirmed creator preferences — ${parts.join("; ")}`;
}

export function applySuggestion(
  profile: CreatorProfileContent,
  suggestion: CreatorProfileSuggestion,
): CreatorProfileContent {
  return normalizeCreatorProfileContent({
    ...profile,
    [suggestion.field]: suggestion.value,
  });
}

export function applyCreatorProfileToBrief(
  brief: CreativeBriefInput,
  profile: CreatorProfileContent | null,
): CreativeBriefInput {
  if (!profile) return brief;
  return {
    ...brief,
    platform: profile.platform || brief.platform,
    audience: profile.audience || brief.audience,
    duration_ms: profile.duration_ms ?? brief.duration_ms,
    aspect_ratio: profile.aspect_ratio || brief.aspect_ratio,
    tone: profile.tone || brief.tone,
    pace: profile.pace || brief.pace,
    narrative_arc: profile.narrative_arc || brief.narrative_arc,
    must_include: profile.must_include.length > 0
      ? [...profile.must_include]
      : brief.must_include,
    must_exclude: profile.must_exclude.length > 0
      ? [...profile.must_exclude]
      : brief.must_exclude,
  };
}

export function mergeCreatorProfileIntoBrief(
  brief: CreativeBriefInput,
  profile: CreatorProfileContent | null,
  editedFields: Iterable<CreatorProfileField>,
  fallback: CreativeBriefInput,
): CreativeBriefInput {
  const edited = new Set(editedFields);
  const use = <Field extends CreatorProfileField>(field: Field) => (
    edited.has(field) ? brief[field] : profile?.[field] ?? fallback[field]
  );
  const useTerms = (field: "must_include" | "must_exclude") => {
    if (edited.has(field)) return [...brief[field]];
    const profileTerms = profile?.[field] ?? [];
    return profileTerms.length > 0 ? [...profileTerms] : [...fallback[field]];
  };
  return {
    ...brief,
    platform: use("platform") || fallback.platform,
    audience: use("audience") || fallback.audience,
    duration_ms: use("duration_ms") ?? fallback.duration_ms,
    aspect_ratio: use("aspect_ratio") || fallback.aspect_ratio,
    tone: use("tone") || fallback.tone,
    pace: use("pace") || fallback.pace,
    narrative_arc: use("narrative_arc") || fallback.narrative_arc,
    must_include: useTerms("must_include"),
    must_exclude: useTerms("must_exclude"),
  };
}

export function deriveAppliedCreatorProfileFields(
  brief: CreativeBriefInput,
  profile: CreatorProfileContent | null,
  editedFields: Iterable<CreatorProfileField>,
): CreatorProfileField[] {
  if (!profile) return [];
  const edited = new Set(editedFields);
  const available = new Set(activeCreatorPreferenceFields(profile));
  const comparable: Record<CreatorProfileField, unknown> = {
    platform: brief.platform,
    audience: brief.audience,
    duration_ms: brief.duration_ms,
    aspect_ratio: brief.aspect_ratio,
    tone: brief.tone,
    pace: brief.pace,
    narrative_arc: brief.narrative_arc ?? "",
    must_include: brief.must_include,
    must_exclude: brief.must_exclude,
  };
  return (Object.keys(comparable) as CreatorProfileField[]).filter((field) => (
    available.has(field)
    && !edited.has(field)
    && JSON.stringify(comparable[field]) === JSON.stringify(profile[field])
  ));
}

export function creatorMemorySummary(profile: CreatorProfileContent | null): string {
  if (!profile || countCreatorPreferences(profile) === 0) {
    return "No confirmed preferences yet";
  }
  return [profile.platform, profile.aspect_ratio, profile.tone, profile.pace]
    .filter(Boolean)
    .slice(0, 3)
    .join(" · ") || `${countCreatorPreferences(profile)} confirmed preferences`;
}

export function formatSuggestionValue(value: CreatorSuggestionValue): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "number") return `${Math.round(value / 1000)} sec`;
  return value;
}
