import assert from "node:assert/strict";
import test from "node:test";

import {
  EMPTY_CREATOR_PROFILE,
  activeCreatorPreferenceFields,
  applyCreatorProfileToBrief,
  countCreatorPreferences,
  creatorDurationSecondsToMilliseconds,
  creatorProfileFieldLabel,
  creatorProfileDraftIsDirty,
  creatorProfilePromptContext,
  deriveAppliedCreatorProfileFields,
  filterCreatorProfile,
  mergeCreatorProfileIntoBrief,
  normalizeCreatorProfileRevision,
  normalizeCreatorSuggestions,
} from "../src/creator/model.ts";
import {
  formatCreatorProfileProvenance,
  snapshotCreatorProfileProvenance,
} from "../src/creator/provenance.ts";
import {
  PendingMutationKeyStore,
  mutationFingerprint,
  shouldRetainMutationKey,
} from "../src/creator/mutationKeyStore.ts";

const EMPTY_BRIEF = {
  goal: "Make a grounded short",
  audience: "General",
  platform: "Social video",
  duration_ms: 15_000,
  aspect_ratio: "9:16",
  tone: "natural",
  pace: "balanced",
  narrative_arc: "begin, develop, finish",
  must_include: [],
  must_exclude: ["black frames"],
};

test("creator duration accepts only whole seconds within the backend range", () => {
  assert.equal(creatorDurationSecondsToMilliseconds("1"), 1_000);
  assert.equal(creatorDurationSecondsToMilliseconds("1800"), 1_800_000);
  assert.equal(creatorDurationSecondsToMilliseconds(""), null);
  assert.equal(creatorDurationSecondsToMilliseconds("0"), null);
  assert.equal(creatorDurationSecondsToMilliseconds("1801"), null);
  assert.equal(creatorDurationSecondsToMilliseconds("1.5"), null);

  const invalidRevision = normalizeCreatorProfileRevision({
    profile: { profile: { duration_ms: 1_800_001 } },
  });
  assert.equal(invalidRevision.profile.duration_ms, null);
});

test("creator fields use human product labels instead of storage names", () => {
  assert.equal(creatorProfileFieldLabel("duration_ms"), "Length");
  assert.equal(creatorProfileFieldLabel("aspect_ratio"), "Format");
  assert.equal(creatorProfileFieldLabel("must_exclude"), "Avoid");
});

test("profile normalizer preserves canonical empty fields and clearing", () => {
  const revision = normalizeCreatorProfileRevision({
    profile: {
      profile_id: "default",
      revision: 4,
      content_sha256: "a".repeat(64),
      profile: { tone: "quiet and candid" },
      evidence: [],
      source: "user_edit",
    },
  });

  assert.equal(revision.profile.tone, "quiet and candid");
  assert.equal(revision.profile.audience, "");
  assert.equal(revision.profile.duration_ms, null);
  assert.deepEqual(revision.profile.must_include, []);
  assert.equal(countCreatorPreferences(revision.profile), 1);

  const reset = normalizeCreatorProfileRevision({
    profile: { profile_id: "default", revision: 5, profile: {}, source: "reset" },
  });
  assert.equal(countCreatorPreferences(reset.profile), 0);
  assert.equal(reset.source, "reset");
});

test("suggestion dirty guard clears only after save or reset catches up", () => {
  const persistedProfile = normalizeCreatorProfileRevision({
    profile: {
      profile: {
        platform: "Xiaohongshu",
        duration_ms: 30_000,
        tone: "warm",
        must_include: ["human detail"],
      },
    },
  }).profile;
  const draft = { ...persistedProfile, must_include: [...persistedProfile.must_include] };
  const input = {
    draft,
    durationText: "30",
    includeText: "human detail",
    excludeText: "",
    persistedProfile,
  };

  assert.equal(creatorProfileDraftIsDirty(input), false);
  assert.equal(creatorProfileDraftIsDirty({ ...input, draft: { ...draft, tone: "precise" } }), true);
  assert.equal(creatorProfileDraftIsDirty({ ...input, durationText: "1801" }), true);
  assert.equal(creatorProfileDraftIsDirty({ ...input, includeText: "human detail, opening hook" }), true);

  const savedProfile = { ...persistedProfile, tone: "precise" };
  assert.equal(creatorProfileDraftIsDirty({
    ...input,
    draft: savedProfile,
    persistedProfile: savedProfile,
  }), false);
  assert.equal(creatorProfileDraftIsDirty({
    draft: EMPTY_CREATOR_PROFILE,
    durationText: "",
    includeText: "",
    excludeText: "",
    persistedProfile: EMPTY_CREATOR_PROFILE,
  }), false);
});

test("profile filtering creates visible, reversible generation context", () => {
  const profile = normalizeCreatorProfileRevision({
    profile: {
      profile: {
        platform: "Xiaohongshu",
        audience: "New creators",
        duration_ms: 30_000,
        aspect_ratio: "9:16",
        tone: "warm",
        pace: "measured",
        must_include: ["human detail"],
      },
    },
  }).profile;
  assert.deepEqual(activeCreatorPreferenceFields(profile), [
    "platform",
    "audience",
    "duration_ms",
    "aspect_ratio",
    "tone",
    "pace",
    "must_include",
  ]);

  const filtered = filterCreatorProfile(profile, ["platform", "tone"]);
  assert.equal(countCreatorPreferences(filtered), 2);
  assert.match(creatorProfilePromptContext(filtered), /platform Xiaohongshu/);
  assert.doesNotMatch(creatorProfilePromptContext(filtered), /New creators/);
  assert.equal(creatorProfilePromptContext(null), "");
});

test("video profile prefill respects user values and tracks exact unchanged fields", () => {
  const profile = normalizeCreatorProfileRevision({
    profile: {
      profile: {
        platform: "Xiaohongshu",
        audience: "New creators",
        duration_ms: 30_000,
        aspect_ratio: "9:16",
        tone: "warm",
        pace: "measured",
        narrative_arc: "hook, proof, release",
        must_include: ["human detail"],
        must_exclude: ["hard sell"],
      },
    },
  }).profile;
  const prefilled = applyCreatorProfileToBrief(EMPTY_BRIEF, profile);
  assert.equal(prefilled.platform, "Xiaohongshu");
  assert.equal(prefilled.duration_ms, 30_000);
  assert.deepEqual(prefilled.must_exclude, ["hard sell"]);

  const edited = { ...prefilled, tone: "precise" };
  const fields = deriveAppliedCreatorProfileFields(edited, profile, ["tone"]);
  assert.ok(fields.includes("platform"));
  assert.ok(fields.includes("must_include"));
  assert.ok(!fields.includes("tone"));

  const memoryOff = mergeCreatorProfileIntoBrief(
    edited,
    null,
    ["tone"],
    EMPTY_BRIEF,
  );
  assert.equal(memoryOff.tone, "precise");
  assert.equal(memoryOff.platform, EMPTY_BRIEF.platform);
});

test("video profile toggles restore only unedited fields", () => {
  const profile = normalizeCreatorProfileRevision({
    profile: {
      profile: {
        platform: "Xiaohongshu",
        tone: "warm",
      },
    },
  }).profile;
  const prefilled = mergeCreatorProfileIntoBrief(EMPTY_BRIEF, profile, [], EMPTY_BRIEF);
  assert.equal(prefilled.platform, "Xiaohongshu");
  assert.equal(prefilled.tone, "warm");
  assert.deepEqual(prefilled.must_exclude, EMPTY_BRIEF.must_exclude);

  const edited = { ...prefilled, goal: "A more specific story", tone: "precise" };
  const disabled = mergeCreatorProfileIntoBrief(edited, null, ["tone"], EMPTY_BRIEF);
  assert.equal(disabled.goal, "A more specific story");
  assert.equal(disabled.tone, "precise");
  assert.equal(disabled.platform, EMPTY_BRIEF.platform);
  assert.deepEqual(disabled.must_exclude, EMPTY_BRIEF.must_exclude);

  const enabledAgain = mergeCreatorProfileIntoBrief(disabled, profile, ["tone"], EMPTY_BRIEF);
  assert.equal(enabledAgain.platform, "Xiaohongshu");
  assert.equal(enabledAgain.tone, "precise");
});

test("photo generation provenance is an immutable point-in-time value", () => {
  const revision = normalizeCreatorProfileRevision({
    profile: {
      profile_id: "default",
      revision: 7,
      content_sha256: "b".repeat(64),
      profile: {
        platform: "Xiaohongshu",
        tone: "warm",
        must_include: ["human detail"],
      },
    },
  });
  const selectedProfile = filterCreatorProfile(revision.profile, [
    "platform",
    "tone",
    "must_include",
  ]);
  const provenance = snapshotCreatorProfileProvenance(revision, selectedProfile);
  assert.ok(provenance);

  revision.revision = 8;
  revision.content_sha256 = "c".repeat(64);
  selectedProfile.tone = "cold";
  selectedProfile.must_include.push("new preference");

  assert.equal(provenance.revision, 7);
  assert.equal(provenance.content_sha256, "b".repeat(64));
  assert.deepEqual(provenance.applied_profile_fields, ["platform", "tone", "must_include"]);
  assert.match(provenance.prompt_context, /tone warm/);
  assert.doesNotMatch(provenance.prompt_context, /new preference/);
  assert.match(formatCreatorProfileProvenance(provenance), /revision 7/);
  assert.match(formatCreatorProfileProvenance(provenance), /content SHA-256 b{64}/);
});

test("suggestions require two independent evidence points before display", () => {
  const suggestions = normalizeCreatorSuggestions({
    data: [
      { field: "tone", value: "warm", evidence_count: 2, evidence: [
        { project_id: "p1", brief_revision: 1 },
        { project_id: "p2", brief_revision: 1 },
      ] },
      { field: "pace", value: "fast", evidence_count: 1 },
      { field: "unknown", value: "ignored", evidence_count: 9 },
    ],
  });
  assert.equal(suggestions.length, 1);
  assert.equal(suggestions[0].field, "tone");
  assert.equal(suggestions[0].evidence.length, 2);
});

test("mutation keys survive ambiguous failures without persisting private payloads", async () => {
  const data = new Map();
  const storage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, value),
    removeItem: (key) => data.delete(key),
  };
  const store = new PendingMutationKeyStore(storage);
  const payload = { base_revision: 3, profile: { tone: "warm" } };
  const scope = "profile:/Users/private/memolens.sqlite:asset-secret";
  const first = await store.acquire(scope, payload);
  await store.settle(scope, first, new TypeError("network lost"));
  assert.equal(await store.acquire(scope, payload), first);
  const persisted = JSON.stringify([...data.entries()]);
  assert.doesNotMatch(persisted, /Users\/private/);
  assert.doesNotMatch(persisted, /asset-secret/);
  assert.doesNotMatch(persisted, /warm/);

  await store.settle(scope, first);
  assert.notEqual(await store.acquire(scope, payload), first);
  assert.equal(shouldRetainMutationKey({ status: 400 }), false);
  assert.equal(shouldRetainMutationKey({ status: 503 }), true);
  assert.equal(
    await mutationFingerprint({ b: 2, a: { z: 3, x: 1 } }),
    await mutationFingerprint({ a: { x: 1, z: 3 }, b: 2 }),
  );
});
