import type {
  CanonicalCoveragePlan,
  CoverageAlternative,
  CoverageEvidenceManifestItem,
  CoveragePlanWorkspace,
} from "./coverageTypes.js";
import type {
  CanonicalTimelineClip,
  CanonicalTimelineEdit,
  CanonicalTimelineHead,
  CanonicalTimelinePendingPreview,
  CanonicalTimelineSourceBinding,
  CanonicalTimelineWorkspace,
} from "./timelineTypes.js";

const MIN_CLIP_DURATION_MS = 1;
const MIN_VIDEO_TRIM_MS = 100;
const MAX_CLIP_DURATION_MS = 1_800_000;
const MAX_SOURCE_TIME_MS = Number.MAX_SAFE_INTEGER;

export interface CanonicalTimelinePendingEdit {
  database_uuid: string;
  project_id: string;
  based_on_head: CanonicalTimelineHead;
  edit: CanonicalTimelineEdit;
  preview: CanonicalTimelinePendingPreview | null;
  previewKind: "local_effect_preview" | "save_then_reread";
  summary: string;
}

function fail(detail: string): never {
  throw new Error(`Canonical Timeline edit cannot be staged: ${detail}.`);
}

function isIntegerInRange(value: number, minimum: number, maximum: number): boolean {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}

function exactCurrentTimeline(
  workspace: CanonicalTimelineWorkspace,
): NonNullable<CanonicalTimelineWorkspace["timeline"]> {
  const timeline = workspace.timeline;
  const head = workspace.head;
  const selected = workspace.selected_revision;
  if (
    timeline === null
    || head === null
    || selected === null
    || !selected.is_head
    || workspace.freshness.state !== "current"
    || selected.revision !== head.revision
    || selected.revision !== timeline.revision
    || selected.timeline_id !== head.timeline_id
    || selected.timeline_id !== timeline.timeline_id
    || selected.revision_sha256 !== head.revision_sha256
    || selected.timeline_content_sha256 !== head.timeline_content_sha256
  ) return fail("the displayed resource is not the exact current head");
  const clips = timeline.tracks[0]?.clips ?? [];
  if (clips.length === 0 || workspace.source_bindings.length !== clips.length) {
    return fail("the current head has no complete fixed source manifest");
  }
  const bindings = new Map(workspace.source_bindings.map((binding) => [binding.clip_id, binding]));
  if (
    bindings.size !== clips.length
    || clips.some((clip) => {
      const binding = bindings.get(clip.clip_id);
      return binding === undefined
        || binding.media_kind !== clip.media_kind
        || binding.evidence_ref !== clip.evidence_ref
        || binding.asset_id !== clip.asset_id
        || binding.asset_sha256 !== clip.asset_sha256;
    })
  ) return fail("the fixed source manifest does not match the clips");
  return timeline;
}

function exactCurrentCoverage(
  timelineWorkspace: CanonicalTimelineWorkspace,
  coverageWorkspace: CoveragePlanWorkspace,
  timeline: NonNullable<CanonicalTimelineWorkspace["timeline"]>,
): CanonicalCoveragePlan {
  const head = coverageWorkspace.head;
  const selected = coverageWorkspace.selected_revision;
  const coverage = coverageWorkspace.coverage_plan;
  const binding = timeline.coverage_binding;
  if (
    coverageWorkspace.database_uuid !== timelineWorkspace.database_uuid
    || coverageWorkspace.project_id !== timelineWorkspace.project_id
    || coverageWorkspace.freshness.state !== "current"
    || head === null
    || selected === null
    || coverage === null
    || !selected.is_head
    || head.revision !== binding.revision
    || head.content_sha256 !== binding.content_sha256
    || selected.revision !== binding.revision
    || selected.content_sha256 !== binding.content_sha256
    || selected.evidence_manifest_sha256 !== binding.evidence_manifest_sha256
    || selected.operation_id !== binding.operation_id
    || coverage.project_id !== timelineWorkspace.project_id
    || coverage.revision !== binding.revision
    || coverage.blueprint_binding.revision !== timeline.blueprint_binding.revision
    || coverage.blueprint_binding.content_sha256 !== timeline.blueprint_binding.content_sha256
    || coverage.blueprint_binding.semantic_sha256 !== timeline.blueprint_binding.semantic_sha256
  ) return fail("the Coverage workspace is not the Timeline's exact pinned current input");
  return coverage;
}

function evidenceFor(
  coverage: CanonicalCoveragePlan,
  evidenceRef: string,
): CoverageEvidenceManifestItem | null {
  return coverage.evidence_manifest.find((item) => item.evidence_ref === evidenceRef) ?? null;
}

function clipDuration(clip: CanonicalTimelineClip): number {
  return clip.end_ms - clip.start_ms;
}

function reflowClips(clips: readonly CanonicalTimelineClip[]): CanonicalTimelineClip[] {
  let cursor = 0;
  return clips.map((clip, ordinal) => {
    const duration = clipDuration(clip);
    if (!isIntegerInRange(duration, MIN_CLIP_DURATION_MS, MAX_CLIP_DURATION_MS)) {
      return fail(`clip ${clip.clip_id} has an invalid duration`);
    }
    const next = {
      ...clip,
      ordinal,
      start_ms: cursor,
      end_ms: cursor + duration,
    } as CanonicalTimelineClip;
    cursor = next.end_ms;
    return next;
  });
}

function pendingPreview(
  workspace: CanonicalTimelineWorkspace,
  clips: readonly CanonicalTimelineClip[],
  sourceBindings: readonly CanonicalTimelineSourceBinding[],
): CanonicalTimelinePendingPreview {
  const timeline = workspace.timeline;
  const head = workspace.head;
  if (timeline === null || head === null) return fail("the Timeline document is missing");
  const nextClips = reflowClips(clips);
  const durationMs = nextClips.at(-1)?.end_ms ?? 0;
  if (!isIntegerInRange(durationMs, 1, MAX_CLIP_DURATION_MS)) {
    return fail("the edited Timeline exceeds the canonical duration limit");
  }
  const bindingsByClip = new Map(sourceBindings.map((binding) => [binding.clip_id, binding]));
  const nextBindings = nextClips.map((clip) => {
    const binding = bindingsByClip.get(clip.clip_id);
    if (binding === undefined) return fail(`clip ${clip.clip_id} has no fixed source binding`);
    return binding;
  });
  return {
    object: "canonical_timeline.pending_preview",
    schema_version: "1",
    database_uuid: workspace.database_uuid,
    project_id: workspace.project_id,
    based_on_head: head,
    timeline: {
      ...timeline,
      output: {
        ...timeline.output,
        duration_ms: durationMs,
      },
      tracks: [{
        ...timeline.tracks[0],
        clips: nextClips,
      }],
    },
    source_bindings: nextBindings,
  };
}

function beatForClip(
  coverage: CanonicalCoveragePlan,
  clip: CanonicalTimelineClip,
): CanonicalCoveragePlan["beats"][number] | null {
  return coverage.beats.find((beat) => beat.beat_id === clip.beat_id) ?? null;
}

export function canonicalTimelineReplacementCandidates(
  workspace: CanonicalTimelineWorkspace,
  coverageWorkspace: CoveragePlanWorkspace,
  clipId: string,
): CoverageAlternative[] {
  const timeline = exactCurrentTimeline(workspace);
  const coverage = exactCurrentCoverage(workspace, coverageWorkspace, timeline);
  const clips = timeline.tracks[0].clips;
  const clip = clips.find((candidate) => candidate.clip_id === clipId);
  if (clip === undefined) return [];
  const beat = beatForClip(coverage, clip);
  if (beat === null) return [];
  const occupiedEvidence = new Set(
    clips.filter((candidate) => candidate.clip_id !== clipId).map((candidate) => candidate.evidence_ref),
  );
  const requiredDuration = clipDuration(clip);
  return beat.alternatives.filter((alternative) => {
    if (
      alternative.assignment_id === clip.assignment_id
      || occupiedEvidence.has(alternative.evidence_ref)
    ) return false;
    const evidence = evidenceFor(coverage, alternative.evidence_ref);
    if (evidence?.status !== "verified" || evidence.proof === null || evidence.proof_sha256 === null) {
      return false;
    }
    return evidence.proof.kind === "asset"
      || evidence.proof.end_ms - evidence.proof.start_ms >= requiredDuration;
  });
}

export function canonicalTimelinePendingEditKey(edit: CanonicalTimelineEdit): string {
  if (edit.op === "move_clip") return `${edit.op}:${edit.clip_id}:${edit.to_index}`;
  if (edit.op === "trim_clip") {
    return `${edit.op}:${edit.clip_id}:${edit.source_in_ms}:${edit.source_out_ms}`;
  }
  if (edit.op === "set_clip_duration") {
    return `${edit.op}:${edit.clip_id}:${edit.duration_ms}`;
  }
  if (edit.op === "replace_clip") {
    return `${edit.op}:${edit.clip_id}:${edit.assignment_id}`;
  }
  return fail("the edit operation is outside timeline.apply_edit");
}

export function stageCanonicalTimelineEdit(input: {
  workspace: CanonicalTimelineWorkspace;
  coverage: CoveragePlanWorkspace;
  edit: CanonicalTimelineEdit;
}): CanonicalTimelinePendingEdit {
  const { workspace, edit } = input;
  const timeline = exactCurrentTimeline(workspace);
  const coverage = exactCurrentCoverage(workspace, input.coverage, timeline);
  const head = workspace.head;
  if (head === null) return fail("the current Timeline head is missing");
  const commandIdentity = {
    database_uuid: workspace.database_uuid,
    project_id: workspace.project_id,
    based_on_head: head,
  };

  const clips = [...timeline.tracks[0].clips];
  const index = clips.findIndex((clip) => clip.clip_id === edit.clip_id);
  if (index < 0) return fail(`clip ${edit.clip_id} is not in the current head`);
  const clip = clips[index];
  if (clip === undefined) return fail(`clip ${edit.clip_id} is unavailable`);

  if (edit.op === "move_clip") {
    if (!isIntegerInRange(edit.to_index, 0, clips.length - 1)) {
      return fail("the destination index is outside the primary track");
    }
    if (edit.to_index === index) return fail("the clip is already at that position");
    clips.splice(index, 1);
    clips.splice(edit.to_index, 0, clip);
    return {
      ...commandIdentity,
      edit,
      preview: pendingPreview(workspace, clips, workspace.source_bindings),
      previewKind: "local_effect_preview",
      summary: `Move clip ${index + 1} to position ${edit.to_index + 1}`,
    };
  }

  if (edit.op === "trim_clip") {
    if (clip.media_kind !== "video") return fail("trim applies only to video clips");
    if (
      !isIntegerInRange(edit.source_in_ms, 0, MAX_SOURCE_TIME_MS)
      || !isIntegerInRange(edit.source_out_ms, 1, MAX_SOURCE_TIME_MS)
      || edit.source_out_ms - edit.source_in_ms < MIN_VIDEO_TRIM_MS
    ) return fail("the trim interval is invalid");
    const evidence = evidenceFor(coverage, clip.evidence_ref);
    if (
      evidence?.status !== "verified"
      || evidence.proof?.kind !== "span"
      || evidence.proof.span_id !== clip.span_id
      || edit.source_in_ms < evidence.proof.start_ms
      || edit.source_out_ms > evidence.proof.end_ms
    ) return fail("the trim interval is outside the pinned verified span");
    const nextClip = {
      ...clip,
      source_in_ms: edit.source_in_ms,
      source_out_ms: edit.source_out_ms,
      end_ms: clip.start_ms + edit.source_out_ms - edit.source_in_ms,
    };
    clips[index] = nextClip;
    const bindings = workspace.source_bindings.map((binding) => (
      binding.clip_id === clip.clip_id && binding.media_kind === "video"
        ? {
          ...binding,
          source_in_ms: edit.source_in_ms,
          source_out_ms: edit.source_out_ms,
        }
        : binding
    ));
    return {
      ...commandIdentity,
      edit,
      preview: pendingPreview(workspace, clips, bindings),
      previewKind: "local_effect_preview",
      summary: `Trim clip ${index + 1} to ${edit.source_in_ms}–${edit.source_out_ms} ms`,
    };
  }

  if (edit.op === "set_clip_duration") {
    if (clip.media_kind !== "image") return fail("duration applies only to image clips");
    if (!isIntegerInRange(edit.duration_ms, MIN_CLIP_DURATION_MS, MAX_CLIP_DURATION_MS)) {
      return fail("the image duration is outside the supported range");
    }
    clips[index] = {
      ...clip,
      end_ms: clip.start_ms + edit.duration_ms,
    };
    return {
      ...commandIdentity,
      edit,
      preview: pendingPreview(workspace, clips, workspace.source_bindings),
      previewKind: "local_effect_preview",
      summary: `Set clip ${index + 1} duration to ${edit.duration_ms} ms`,
    };
  }

  if (edit.op === "replace_clip") {
    const candidates = canonicalTimelineReplacementCandidates(
      workspace,
      input.coverage,
      clip.clip_id,
    );
    if (!candidates.some((candidate) => candidate.assignment_id === edit.assignment_id)) {
      return fail("the replacement is not an eligible same-Beat verified alternative");
    }
    return {
      ...commandIdentity,
      edit,
      preview: null,
      previewKind: "save_then_reread",
      summary: `Replace clip ${index + 1} with Coverage assignment ${edit.assignment_id}`,
    };
  }
  return fail("the edit operation is outside timeline.apply_edit");
}
