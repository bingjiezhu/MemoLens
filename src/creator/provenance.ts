import {
  activeCreatorPreferenceFields,
  creatorProfilePromptContext,
} from "./model";
import type {
  CreatorProfileContent,
  CreatorProfileField,
  CreatorProfileRevision,
} from "./types";

export interface CreatorProfileProvenance {
  profile_id: string;
  revision: number;
  content_sha256: string;
  applied_profile_fields: CreatorProfileField[];
  prompt_context: string;
}

export function snapshotCreatorProfileProvenance(
  revision: CreatorProfileRevision | null,
  selectedProfile: CreatorProfileContent | null,
): CreatorProfileProvenance | null {
  if (
    !revision
    || revision.revision < 1
    || !revision.content_sha256
    || !selectedProfile
  ) {
    return null;
  }
  const appliedFields = activeCreatorPreferenceFields(selectedProfile);
  const promptContext = creatorProfilePromptContext(selectedProfile);
  if (appliedFields.length === 0 || !promptContext) {
    return null;
  }
  return {
    profile_id: revision.profile_id,
    revision: revision.revision,
    content_sha256: revision.content_sha256,
    applied_profile_fields: appliedFields,
    prompt_context: promptContext,
  };
}

export function formatCreatorProfileProvenance(
  provenance: CreatorProfileProvenance | null,
): string {
  if (!provenance) {
    return "Creator Memory was not used for this draft.";
  }
  return [
    `Creator Memory ${provenance.profile_id} revision ${provenance.revision}`,
    `content SHA-256 ${provenance.content_sha256}`,
    `applied fields: ${provenance.applied_profile_fields.join(", ")}`,
  ].join(" · ");
}
