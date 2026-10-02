import type {
  CanonicalTimelineClip,
  CanonicalTimelineDocument,
  CanonicalTimelinePendingPreview,
  CanonicalTimelineSourceBinding,
  CanonicalTimelineVideoClip,
  CanonicalTimelineWorkspace,
} from "./timelineTypes.js";
import { isSameCanonicalTimelineHead } from "./timelineModel";

export interface TimelinePreviewLocation {
  clip: CanonicalTimelineClip;
  index: number;
  playheadMs: number;
  offsetMs: number;
}

export interface TimelinePreviewMediaItem {
  clip: CanonicalTimelineClip;
  sourceBinding: CanonicalTimelineSourceBinding;
  mediaUrl: string | null;
  thumbnailUrl: string | null;
}

export function clampTimelinePreviewPlayhead(
  playheadMs: number,
  durationMs: number,
): number {
  if (!Number.isFinite(durationMs) || durationMs <= 0) return 0;
  if (!Number.isFinite(playheadMs)) return 0;
  return Math.min(durationMs, Math.max(0, playheadMs));
}

export function timelinePreviewPlayheadFromFraction(
  fraction: number,
  durationMs: number,
): number {
  const boundedFraction = Number.isFinite(fraction)
    ? Math.min(1, Math.max(0, fraction))
    : 0;
  return clampTimelinePreviewPlayhead(boundedFraction * durationMs, durationMs);
}

/** Locate a clip using the Timeline's half-open hard-cut intervals. */
export function timelinePreviewClipAtPlayhead(
  clips: readonly CanonicalTimelineClip[],
  playheadMs: number,
): TimelinePreviewLocation | null {
  if (clips.length === 0) return null;
  const durationMs = clips[clips.length - 1]?.end_ms ?? 0;
  const bounded = clampTimelinePreviewPlayhead(playheadMs, durationMs);
  const index = bounded === durationMs
    ? clips.length - 1
    : clips.findIndex((clip) => bounded >= clip.start_ms && bounded < clip.end_ms);
  if (index < 0) return null;
  const clip = clips[index];
  if (!clip) return null;
  return {
    clip,
    index,
    playheadMs: bounded,
    offsetMs: Math.min(clip.end_ms - clip.start_ms, Math.max(0, bounded - clip.start_ms)),
  };
}

/** Map Timeline time to the exact source span without crossing its half-open end. */
export function timelinePreviewVideoSourceTimeMs(
  clip: CanonicalTimelineVideoClip,
  playheadMs: number,
): number {
  const offsetMs = clampTimelinePreviewPlayhead(
    playheadMs - clip.start_ms,
    clip.end_ms - clip.start_ms,
  );
  return Math.min(
    Math.max(clip.source_in_ms, clip.source_out_ms - 1),
    clip.source_in_ms + offsetMs,
  );
}

export function timelinePreviewResourceUrl(
  apiBase: string,
  resourcePath: string,
): string | null {
  try {
    const backend = new URL(`${apiBase.replace(/\/+$/, "")}/`);
    if (backend.protocol !== "http:" && backend.protocol !== "https:") return null;
    const resource = new URL(resourcePath, backend);
    if (resource.origin !== backend.origin) return null;
    return resource.toString();
  } catch {
    return null;
  }
}

function bindingMatchesClip(
  clip: CanonicalTimelineClip,
  binding: CanonicalTimelineSourceBinding,
): boolean {
  if (
    clip.clip_id !== binding.clip_id
    || clip.evidence_ref !== binding.evidence_ref
    || clip.asset_id !== binding.asset_id
    || clip.asset_sha256 !== binding.asset_sha256
    || clip.media_kind !== binding.media_kind
  ) return false;
  if (clip.media_kind === "image" || binding.media_kind === "image") {
    if (clip.media_kind !== binding.media_kind) return false;
    const fields = ["analysis_run_id", "analysis_revision", "analysis_content_sha256", "source_binding_sha256"];
    const clipHasAnalysis = fields.some((field) => Object.hasOwn(clip, field));
    const bindingHasAnalysis = fields.some((field) => Object.hasOwn(binding, field));
    if (clipHasAnalysis !== bindingHasAnalysis) return false;
    if (!clipHasAnalysis) return true;
    return fields.every((field) => (
      Object.hasOwn(clip, field) && Object.hasOwn(binding, field)
      && Reflect.get(clip, field) === Reflect.get(binding, field)
    ));
  }
  const clipIsResidual = "residual_id" in clip;
  const bindingIsResidual = "residual_id" in binding;
  if (clipIsResidual !== bindingIsResidual) return false;
  const residualMatches = !clipIsResidual || !bindingIsResidual || (
    clip.residual_id === binding.residual_id
    && clip.parent_segment_id === binding.parent_segment_id
    && JSON.stringify(clip.residual_binding) === JSON.stringify(binding.residual_binding)
  );
  return residualMatches
    && clip.span_id === binding.span_id
    && clip.analysis_run_id === binding.analysis_run_id
    && clip.analysis_revision === binding.analysis_revision
    && clip.input_asset_sha256 === binding.input_asset_sha256
    && clip.source_in_ms === binding.source_in_ms
    && clip.source_out_ms === binding.source_out_ms;
}

function buildMediaItemsFromBindings(
  apiBase: string,
  timeline: CanonicalTimelineDocument,
  sourceBindings: readonly CanonicalTimelineSourceBinding[],
): TimelinePreviewMediaItem[] {
  const clips = timeline.tracks[0]?.clips ?? [];
  if (sourceBindings.length !== clips.length) return [];
  const items: TimelinePreviewMediaItem[] = [];
  for (const [index, clip] of clips.entries()) {
    const sourceBinding = sourceBindings[index];
    if (!sourceBinding || !bindingMatchesClip(clip, sourceBinding)) return [];
    const assetId = encodeURIComponent(clip.asset_id);
    if (clip.media_kind === "image") {
      items.push({
        clip,
        sourceBinding,
        mediaUrl: null,
        thumbnailUrl: timelinePreviewResourceUrl(
          apiBase,
          `/v1/assets/${assetId}/thumbnail`,
        ),
      });
      continue;
    }
    items.push({
      clip,
      sourceBinding,
      mediaUrl: timelinePreviewResourceUrl(apiBase, `/v1/assets/${assetId}/media`),
      thumbnailUrl: timelinePreviewResourceUrl(
        apiBase,
        `/v1/video-segments/${encodeURIComponent(clip.span_id)}/thumbnail`,
      ),
    });
  }
  return items;
}

export function buildTimelinePreviewMediaItems(
  apiBase: string,
  workspace: CanonicalTimelineWorkspace,
): TimelinePreviewMediaItem[] {
  if (
    workspace.timeline === null
    || workspace.selected_revision === null
    || workspace.freshness.state === "stale_source_binding"
  ) return [];
  return buildMediaItemsFromBindings(
    apiBase,
    workspace.timeline,
    workspace.source_bindings,
  );
}

export function buildPendingTimelinePreviewMediaItems(
  apiBase: string,
  preview: CanonicalTimelinePendingPreview,
): TimelinePreviewMediaItem[] {
  return buildMediaItemsFromBindings(
    apiBase,
    preview.timeline,
    preview.source_bindings,
  );
}

export function isPendingTimelinePreviewForWorkspace(
  preview: CanonicalTimelinePendingPreview,
  workspace: CanonicalTimelineWorkspace,
): boolean {
  const head = workspace.head;
  const selected = workspace.selected_revision;
  return head !== null
    && selected !== null
    && selected.is_head
    && workspace.timeline !== null
    && workspace.freshness.state === "current"
    && preview.database_uuid === workspace.database_uuid
    && preview.project_id === workspace.project_id
    && preview.timeline.project_id === workspace.project_id
    && preview.timeline.timeline_id === head.timeline_id
    && isSameCanonicalTimelineHead(preview.based_on_head, head)
    && selected.revision === head.revision
    && selected.revision_sha256 === head.revision_sha256
    && selected.timeline_id === head.timeline_id
    && selected.timeline_content_sha256 === head.timeline_content_sha256
    && JSON.stringify(selected.blueprint_binding) === JSON.stringify(head.blueprint_binding)
    && JSON.stringify(selected.coverage_binding) === JSON.stringify(head.coverage_binding)
    && selected.operation_id === head.operation_id;
}

export function canonicalTimelinePreviewIdentity(
  workspace: CanonicalTimelineWorkspace,
): string {
  const selected = workspace.selected_revision;
  return [
    workspace.database_uuid,
    workspace.project_id,
    selected?.timeline_id ?? "missing",
    selected?.revision ?? 0,
    selected?.revision_sha256 ?? "missing",
    selected?.timeline_content_sha256 ?? "missing",
  ].join(":");
}

export function pendingTimelinePreviewIdentity(
  preview: CanonicalTimelinePendingPreview,
): string {
  return [
    preview.database_uuid,
    preview.project_id,
    preview.based_on_head.timeline_id,
    preview.based_on_head.revision,
    preview.based_on_head.revision_sha256,
    "pending",
  ].join(":");
}
