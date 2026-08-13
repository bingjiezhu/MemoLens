import { creatorProfileFieldLabel, formatSuggestionValue } from "./model";
import type {
  CreatorProfileContent,
  CreatorProfileField,
  CreatorSuggestionValue,
} from "./types";

interface CreatorPreferenceContextProps {
  profile: CreatorProfileContent;
  availableFields: CreatorProfileField[];
  activeFields: CreatorProfileField[];
  onToggle(field: CreatorProfileField): void;
  enabled: boolean;
  onEnabledChange(enabled: boolean): void;
}

function fieldValue(profile: CreatorProfileContent, field: CreatorProfileField): CreatorSuggestionValue {
  const value = profile[field];
  return value === null ? "" : value;
}

export function CreatorPreferenceContext({
  profile,
  availableFields,
  activeFields,
  onToggle,
  enabled,
  onEnabledChange,
}: CreatorPreferenceContextProps) {
  if (availableFields.length === 0) return null;
  return (
    <section className="creator-context" aria-labelledby="creator-context-title">
      <div>
        <p className="eyebrow">This creation</p>
        <h2 id="creator-context-title">Using {enabled ? activeFields.length : 0} creator preferences</h2>
        <p>These confirmed values are passed into this draft. Turn off any value for this creation only.</p>
      </div>
      <label className="creator-context-toggle">
        <input type="checkbox" checked={enabled} onChange={(event) => onEnabledChange(event.target.checked)} />
        <span>Use Creator Memory</span>
      </label>
      <div className="creator-context-chips" aria-label="Creator preferences used for this creation">
        {availableFields.map((field) => {
          const active = enabled && activeFields.includes(field);
          return (
            <button
              key={field}
              type="button"
              className={active ? "active" : ""}
              aria-pressed={active}
              disabled={!enabled}
              onClick={() => onToggle(field)}
            >
              <span>{creatorProfileFieldLabel(field)}</span>
              <strong>{formatSuggestionValue(fieldValue(profile, field))}</strong>
            </button>
          );
        })}
      </div>
    </section>
  );
}
