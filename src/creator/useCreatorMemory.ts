import { useCallback, useEffect, useMemo, useState } from "react";

import {
  CreatorApiError,
  fetchCreatorProfile,
  fetchCreatorSuggestions,
  putCreatorProfile,
} from "./api";
import { EMPTY_CREATOR_PROFILE } from "./model";
import { PendingMutationKeyStore, type MutationPayload } from "./mutationKeyStore";
import type {
  CreatorEvidence,
  CreatorProfileContent,
  CreatorProfileRevision,
  CreatorProfileSource,
  CreatorProfileSuggestion,
} from "./types";

export interface CreatorMemoryStore {
  profile: CreatorProfileRevision | null;
  suggestions: CreatorProfileSuggestion[];
  phase: "idle" | "loading" | "ready" | "saving" | "error";
  error: string | null;
  canWrite: boolean;
  save(
    profile: CreatorProfileContent,
    source?: CreatorProfileSource,
    evidence?: CreatorEvidence[],
  ): Promise<boolean>;
  reset(): Promise<boolean>;
  confirm(suggestion: CreatorProfileSuggestion): Promise<boolean>;
  reload(): void;
}

export function useCreatorMemory(input: {
  apiBase: string;
  dbPath: string | null;
  canRead: boolean;
  canWrite: boolean;
}): CreatorMemoryStore {
  const [profile, setProfile] = useState<CreatorProfileRevision | null>(null);
  const [suggestions, setSuggestions] = useState<CreatorProfileSuggestion[]>([]);
  const [phase, setPhase] = useState<CreatorMemoryStore["phase"]>("idle");
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const mutationKeys = useMemo(() => new PendingMutationKeyStore(window.localStorage), []);

  useEffect(() => {
    if (!input.canRead || !input.dbPath) {
      setProfile(null);
      setSuggestions([]);
      setPhase("idle");
      setError(null);
      return;
    }
    const controller = new AbortController();
    setPhase("loading");
    setError(null);
    void Promise.all([
      fetchCreatorProfile(input.apiBase, input.dbPath, controller.signal),
      fetchCreatorSuggestions(input.apiBase, input.dbPath, controller.signal),
    ]).then(([nextProfile, nextSuggestions]) => {
      setProfile(nextProfile);
      setSuggestions(nextSuggestions);
      setPhase("ready");
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : "Creator Memory could not be loaded.");
      setPhase("error");
    });
    return () => controller.abort();
  }, [input.apiBase, input.canRead, input.dbPath, reloadKey]);

  const write = useCallback(async (
    nextProfile: Partial<CreatorProfileContent>,
    source: CreatorProfileSource,
    evidence: CreatorEvidence[] = [],
  ): Promise<boolean> => {
    if (!input.canWrite || !input.dbPath || !profile) return false;
    setPhase("saving");
    setError(null);
    const payload: MutationPayload = {
      db_path: input.dbPath,
      base_revision: profile.revision,
      profile: nextProfile,
      source,
      evidence,
    };
    const scope = `creator-profile:${input.dbPath}`;
    const key = await mutationKeys.acquire(scope, payload);
    try {
      const saved = await putCreatorProfile({
        apiBase: input.apiBase,
        dbPath: input.dbPath,
        baseRevision: profile.revision,
        profile: nextProfile,
        source,
        evidence,
        idempotencyKey: key,
      });
      await mutationKeys.settle(scope, key);
      setProfile(saved);
      setSuggestions((current) => current.filter((suggestion) => (
        source !== "confirmed_suggestion"
        || JSON.stringify(nextProfile[suggestion.field]) !== JSON.stringify(suggestion.value)
      )));
      setPhase("ready");
      return true;
    } catch (reason) {
      await mutationKeys.settle(scope, key, reason);
      const conflict = reason instanceof CreatorApiError
        && reason.code === "profile_revision_conflict";
      setError(conflict
        ? "Creator Memory changed in another view. Reloaded the latest revision; review and save again."
        : reason instanceof Error ? reason.message : "Creator Memory could not be saved.");
      setPhase("error");
      if (conflict) setReloadKey((current) => current + 1);
      return false;
    }
  }, [input.apiBase, input.canWrite, input.dbPath, mutationKeys, profile]);

  const save = useCallback((
    nextProfile: CreatorProfileContent,
    source: CreatorProfileSource = "user_edit",
    evidence: CreatorEvidence[] = [],
  ) => write(nextProfile, source, evidence), [write]);

  const reset = useCallback(
    () => write({}, "reset"),
    [write],
  );

  const confirm = useCallback((suggestion: CreatorProfileSuggestion) => {
    const currentProfile = profile?.profile ?? EMPTY_CREATOR_PROFILE;
    return write(
      { ...currentProfile, [suggestion.field]: suggestion.value },
      "confirmed_suggestion",
      suggestion.evidence,
    );
  }, [profile, write]);

  return {
    profile,
    suggestions,
    phase,
    error,
    canWrite: input.canWrite,
    save,
    reset,
    confirm,
    reload: () => setReloadKey((current) => current + 1),
  };
}
