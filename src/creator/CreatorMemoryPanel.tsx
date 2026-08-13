import { useEffect, useState } from "react";

import {
  CREATOR_DURATION_MAX_SECONDS,
  CREATOR_DURATION_MIN_SECONDS,
  EMPTY_CREATOR_PROFILE,
  countCreatorPreferences,
  creatorProfileFieldLabel,
  creatorDurationSecondsToMilliseconds,
  creatorProfileDraftIsDirty,
  formatSuggestionValue,
  uniqueTerms,
} from "./model";
import type { CreatorProfileSource } from "./types";
import type { CreatorMemoryStore } from "./useCreatorMemory";

interface CreatorMemoryPanelProps {
  store: CreatorMemoryStore;
}

function sourceLabel(source: CreatorProfileSource | null): string {
  if (source === "user_edit") return "edited by you";
  if (source === "confirmed_suggestion") return "confirmed suggestion";
  if (source === "reset") return "reset by you";
  return "not yet created";
}

export function CreatorMemoryPanel({ store }: CreatorMemoryPanelProps) {
  const [draft, setDraft] = useState(EMPTY_CREATOR_PROFILE);
  const [durationText, setDurationText] = useState("");
  const [durationError, setDurationError] = useState<string | null>(null);
  const [includeText, setIncludeText] = useState("");
  const [excludeText, setExcludeText] = useState("");
  const [savedMessage, setSavedMessage] = useState<string | null>(null);

  useEffect(() => {
    const profile = store.profile?.profile ?? EMPTY_CREATOR_PROFILE;
    setDraft(profile);
    setDurationText(profile.duration_ms === null ? "" : String(profile.duration_ms / 1_000));
    setDurationError(null);
    setIncludeText(profile.must_include.join(", "));
    setExcludeText(profile.must_exclude.join(", "));
  }, [store.profile?.revision, store.profile?.profile]);

  async function handleSave(): Promise<void> {
    setSavedMessage(null);
    const durationMilliseconds = creatorDurationSecondsToMilliseconds(durationText);
    if (durationText.trim() && durationMilliseconds === null) {
      setDurationError(
        `Duration must be a whole number from ${CREATOR_DURATION_MIN_SECONDS} to ${CREATOR_DURATION_MAX_SECONDS} seconds.`,
      );
      return;
    }
    setDurationError(null);
    const saved = await store.save({
      ...draft,
      duration_ms: durationMilliseconds,
      must_include: uniqueTerms(includeText),
      must_exclude: uniqueTerms(excludeText),
    });
    if (saved) setSavedMessage("Creator Memory saved as a new revision.");
  }

  async function handleReset(): Promise<void> {
    setSavedMessage(null);
    const saved = await store.reset();
    if (saved) setSavedMessage("Preferences reset in a new revision. Earlier revisions remain auditable.");
  }

  const preferenceCount = countCreatorPreferences(store.profile?.profile ?? null);
  const busy = store.phase === "saving";
  const draftIsDirty = creatorProfileDraftIsDirty({
    draft,
    durationText,
    includeText,
    excludeText,
    persistedProfile: store.profile?.profile ?? null,
  });

  return (
    <section className="creator-memory-panel" aria-labelledby="creator-memory-title">
      <div className="creator-memory-heading">
        <div>
          <p className="eyebrow">Creator Memory</p>
          <h2 id="creator-memory-title">Your confirmed creative defaults.</h2>
          <p>Transparent, editable, and learned only after you confirm it.</p>
        </div>
        <div className="meta-pills creator-memory-meta">
          <span className="status-pill">Using {preferenceCount} preferences</span>
          <span className="meta-pill">Revision {store.profile?.revision ?? 0}</span>
          <span className="meta-pill">{sourceLabel(store.profile?.source ?? null)}</span>
        </div>
      </div>

      {!store.canWrite ? (
        <p className="inline-note">Read-only here. Open MemoLens Desktop to edit confirmed preferences.</p>
      ) : null}

      <div className="creator-memory-grid">
        <label>
          <span>Platform</span>
          <input
            value={draft.platform}
            onChange={(event) => setDraft({ ...draft, platform: event.target.value })}
            placeholder="Xiaohongshu, Douyin, YouTube"
            disabled={!store.canWrite || busy}
          />
        </label>
        <label>
          <span>Audience</span>
          <input
            value={draft.audience}
            onChange={(event) => setDraft({ ...draft, audience: event.target.value })}
            placeholder="Who you create for"
            disabled={!store.canWrite || busy}
          />
        </label>
        <label>
          <span>Default duration</span>
          <input
            type="number"
            min={CREATOR_DURATION_MIN_SECONDS}
            max={CREATOR_DURATION_MAX_SECONDS}
            step={1}
            value={durationText}
            onChange={(event) => {
              setDurationText(event.target.value);
              setDurationError(null);
            }}
            placeholder="Seconds"
            aria-invalid={durationError !== null}
            aria-describedby={durationError ? "creator-duration-error" : undefined}
            disabled={!store.canWrite || busy}
          />
          {durationError ? (
            <span id="creator-duration-error" className="inline-error" role="alert">
              {durationError}
            </span>
          ) : null}
        </label>
        <label>
          <span>Aspect ratio</span>
          <select
            value={draft.aspect_ratio}
            onChange={(event) => setDraft({ ...draft, aspect_ratio: event.target.value })}
            disabled={!store.canWrite || busy}
          >
            <option value="">No default</option>
            <option value="9:16">9:16 vertical</option>
            <option value="16:9">16:9 landscape</option>
            <option value="1:1">1:1 square</option>
            <option value="4:5">4:5 portrait</option>
          </select>
        </label>
        <label>
          <span>Tone</span>
          <input
            value={draft.tone}
            onChange={(event) => setDraft({ ...draft, tone: event.target.value })}
            placeholder="Warm, candid, precise"
            disabled={!store.canWrite || busy}
          />
        </label>
        <label>
          <span>Pace</span>
          <input
            value={draft.pace}
            onChange={(event) => setDraft({ ...draft, pace: event.target.value })}
            placeholder="Measured opening, energetic finish"
            disabled={!store.canWrite || busy}
          />
        </label>
        <label className="creator-memory-wide">
          <span>Narrative arc</span>
          <input
            value={draft.narrative_arc}
            onChange={(event) => setDraft({ ...draft, narrative_arc: event.target.value })}
            placeholder="How your stories usually move"
            disabled={!store.canWrite || busy}
          />
        </label>
        <label className="creator-memory-wide">
          <span>Must include</span>
          <input
            value={includeText}
            onChange={(event) => setIncludeText(event.target.value)}
            placeholder="Recurring elements, separated by commas"
            disabled={!store.canWrite || busy}
          />
        </label>
        <label className="creator-memory-wide">
          <span>Exclude</span>
          <input
            value={excludeText}
            onChange={(event) => setExcludeText(event.target.value)}
            placeholder="Patterns you do not want"
            disabled={!store.canWrite || busy}
          />
        </label>
      </div>

      <div className="creator-memory-actions">
        <button
          className="primary-button"
          type="button"
          onClick={() => void handleSave()}
          disabled={!store.canWrite || busy}
        >
          {busy ? "Saving…" : "Save preferences"}
        </button>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void handleReset()}
          disabled={!store.canWrite || busy || (store.profile?.revision ?? 0) === 0}
        >
          Reset in new revision
        </button>
      </div>

      {store.suggestions.length > 0 ? (
        <div className="creator-suggestions">
          <div><p className="eyebrow">Waiting for you</p><h3>Patterns seen across saved projects</h3></div>
          {draftIsDirty ? (
            <p id="creator-suggestion-dirty-note" className="inline-note" role="status">
              Save or reset your current edits before confirming a suggestion.
            </p>
          ) : null}
          <ul>
            {store.suggestions.map((suggestion) => (
              <li key={`${suggestion.field}-${formatSuggestionValue(suggestion.value)}`}>
                <div><strong>{creatorProfileFieldLabel(suggestion.field)}</strong><span>{formatSuggestionValue(suggestion.value)} · {suggestion.evidence_count} projects</span></div>
                <button
                  className="secondary-button compact-button"
                  type="button"
                  onClick={() => void store.confirm(suggestion)}
                  disabled={!store.canWrite || busy || draftIsDirty}
                  aria-describedby={draftIsDirty ? "creator-suggestion-dirty-note" : undefined}
                >
                  Confirm
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="creator-memory-footnote">Suggestions appear only when at least two independent saved projects support the same preference.</p>
      )}

      {store.error ? <p className="inline-error" role="alert">{store.error}</p> : null}
      {savedMessage ? <p className="inline-note" role="status" aria-live="polite">{savedMessage}</p> : null}
    </section>
  );
}
