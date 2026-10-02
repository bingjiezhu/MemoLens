import type { CoveragePlanWorkspace } from "./coverageTypes";
import type {
  CanonicalTimelineBlueprintBinding,
  CanonicalTimelineClip,
  CanonicalTimelineCoverageBinding,
  CanonicalTimelineDocument,
  CanonicalTimelineEdit,
  CanonicalTimelineEditCommandResult,
  CanonicalTimelineHead,
  CanonicalTimelineMaterializeCommandResult,
  CanonicalTimelineReconcileCommandResult,
  CanonicalTimelineRestoreCommandResult,
  CanonicalTimelineRestoreFrom,
  CanonicalTimelineSelectedRevision,
  CanonicalTimelineSourceBinding,
  CanonicalTimelineStructuralEdit,
  CanonicalTimelineStructuralEditCommandResult,
  CanonicalTimelineWorkspace,
} from "./timelineTypes";
import type { BlueprintProjectWorkspace } from "./workspaceTypes";
import { normalizeClosedResidualBinding } from "../video/residual";

const SHA256 = /^[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const DATABASE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const ASSET_ID = /^(?:asset|img)_[0-9a-f]{24}$/;
const SOURCE_ID = /^src_[0-9a-f]{24}$/;
const SPAN_ID = /^seg_([0-9a-f]{24})_([1-9][0-9]{0,6})_(?:0|[1-9][0-9]{0,6})$/;
const ANALYSIS_RUN_ID = /^arun_[0-9a-f]{32}$/;
const ASSET_REF = /^memolens:\/\/evidence\/asset\/((?:asset|img)_[0-9a-f]{24})$/;
const SPAN_REF = /^memolens:\/\/evidence\/span\/(seg_[0-9a-f]{24}_[1-9][0-9]{0,6}_(?:0|[1-9][0-9]{0,6}))$/;
const RESIDUAL_ID = /^rseg_[0-9a-f]{64}$/;
const RESIDUAL_REF = /^memolens:\/\/evidence\/span\/(rseg_[0-9a-f]{64})$/;
const MAX_CLIPS = 256;
const IMAGE_ANALYSIS_FIELDS = [
  "analysis_run_id", "analysis_revision", "analysis_content_sha256", "source_binding_sha256",
];

function fail(path: string): never {
  throw new Error(`Invalid canonical Timeline workspace at ${path}.`);
}

function residualBinding(value: unknown, path: string) {
  try {
    return normalizeClosedResidualBinding(value);
  } catch {
    return fail(`${path}.residual_binding`);
  }
}

function record(value: unknown, path: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fail(path);
  return value as Record<string, unknown>;
}

function closedRecord(
  value: unknown,
  expected: readonly string[],
  path: string,
): Record<string, unknown> {
  const raw = record(value, path);
  const actual = Object.keys(raw).sort();
  const frozen = [...expected].sort();
  if (
    actual.length !== frozen.length
    || actual.some((key, index) => key !== frozen[index])
  ) return fail(path);
  return raw;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) return fail(path);
  return value;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) return fail(path);
  return value;
}

function exactString<T extends string>(value: unknown, expected: T, path: string): T {
  if (value !== expected) return fail(path);
  return expected;
}

function oneOf<T extends string>(value: unknown, choices: readonly T[], path: string): T {
  if (typeof value !== "string" || !choices.includes(value as T)) return fail(path);
  return value as T;
}

function integer(
  value: unknown,
  path: string,
  minimum = 0,
  maximum = 1_800_000,
): number {
  if (!Number.isInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    return fail(path);
  }
  return Number(value);
}

function identifier(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (!IDENTIFIER.test(normalized)) return fail(path);
  return normalized;
}

function digest(value: unknown, path: string): string {
  const normalized = string(value, path);
  if (!SHA256.test(normalized)) return fail(path);
  return normalized;
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function isContentAddressed(assetId: string, assetDigest: string): boolean {
  return assetId === `${assetId.split("_", 1)[0]}_${assetDigest.slice(0, 24)}`;
}

function blueprintBinding(value: unknown, path: string): CanonicalTimelineBlueprintBinding {
  const raw = closedRecord(
    value,
    ["revision", "content_sha256", "semantic_sha256", "operation_id"],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
    semantic_sha256: digest(raw.semantic_sha256, `${path}.semantic_sha256`),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function coverageBinding(value: unknown, path: string): CanonicalTimelineCoverageBinding {
  const raw = closedRecord(
    value,
    ["revision", "content_sha256", "evidence_manifest_sha256", "operation_id"],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    content_sha256: digest(raw.content_sha256, `${path}.content_sha256`),
    evidence_manifest_sha256: digest(
      raw.evidence_manifest_sha256,
      `${path}.evidence_manifest_sha256`,
    ),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function head(value: unknown, path: string): CanonicalTimelineHead {
  const raw = closedRecord(
    value,
    [
      "revision",
      "revision_sha256",
      "timeline_id",
      "timeline_content_sha256",
      "blueprint_binding",
      "coverage_binding",
      "operation_id",
    ],
    path,
  );
  return {
    revision: integer(raw.revision, `${path}.revision`, 1),
    revision_sha256: digest(raw.revision_sha256, `${path}.revision_sha256`),
    timeline_id: identifier(raw.timeline_id, `${path}.timeline_id`),
    timeline_content_sha256: digest(
      raw.timeline_content_sha256,
      `${path}.timeline_content_sha256`,
    ),
    blueprint_binding: blueprintBinding(raw.blueprint_binding, `${path}.blueprint_binding`),
    coverage_binding: coverageBinding(raw.coverage_binding, `${path}.coverage_binding`),
    operation_id: identifier(raw.operation_id, `${path}.operation_id`),
  };
}

function selectedRevision(value: unknown, path: string): CanonicalTimelineSelectedRevision {
  const raw = closedRecord(
    value,
    [
      "revision",
      "revision_sha256",
      "timeline_id",
      "timeline_content_sha256",
      "blueprint_binding",
      "coverage_binding",
      "operation_id",
      "is_head",
    ],
    path,
  );
  return {
    ...head(
      Object.fromEntries(Object.entries(raw).filter(([key]) => key !== "is_head")),
      path,
    ),
    is_head: raw.is_head === true
      ? true
      : raw.is_head === false
        ? false
        : fail(`${path}.is_head`),
  };
}

function clip(value: unknown, index: number, expectedStartMs: number): CanonicalTimelineClip {
  const path = `timeline.timeline.tracks[0].clips[${index}]`;
  const candidate = record(value, path);
  const rawKind = candidate.media_kind;
  const residualReference = typeof candidate.evidence_ref === "string"
    ? RESIDUAL_REF.exec(candidate.evidence_ref)
    : null;
  const commonFields = [
    "clip_id",
    "ordinal",
    "beat_id",
    "assignment_id",
    "evidence_ref",
    "asset_id",
    "asset_sha256",
    "media_kind",
    "start_ms",
    "end_ms",
    "fit",
    "audio_enabled",
  ];
  const videoFields = [
    "span_id",
    "analysis_run_id",
    "analysis_revision",
    "input_asset_sha256",
    "source_in_ms",
    "source_out_ms",
  ];
  const residualFields = ["residual_id", "parent_segment_id", "residual_binding"];
  const imageHasAnalysis = rawKind === "image"
    && IMAGE_ANALYSIS_FIELDS.some((field) => Object.hasOwn(candidate, field));
  if (rawKind !== "image" && rawKind !== "video") return fail(`${path}.media_kind`);
  const raw = closedRecord(
    value,
    rawKind === "video"
      ? [...commonFields, ...videoFields, ...(residualReference === null ? [] : residualFields)]
      : [...commonFields, ...(imageHasAnalysis ? IMAGE_ANALYSIS_FIELDS : [])],
    path,
  );
  const clipId = identifier(raw.clip_id, `${path}.clip_id`);
  if (!/^clip_[0-9a-f]{24}$/.test(clipId)) return fail(`${path}.clip_id`);
  const ordinal = integer(raw.ordinal, `${path}.ordinal`);
  const evidenceRef = string(raw.evidence_ref, `${path}.evidence_ref`);
  const assetId = identifier(raw.asset_id, `${path}.asset_id`);
  const assetDigest = digest(raw.asset_sha256, `${path}.asset_sha256`);
  const start = integer(raw.start_ms, `${path}.start_ms`, 0, Number.MAX_SAFE_INTEGER);
  const end = integer(raw.end_ms, `${path}.end_ms`, 1, Number.MAX_SAFE_INTEGER);
  if (
    ordinal !== index
    || start !== expectedStartMs
    || end <= start
    || !ASSET_ID.test(assetId)
    || !isContentAddressed(assetId, assetDigest)
    || raw.fit !== "cover"
    || raw.audio_enabled !== false
  ) return fail(path);
  const base = {
    clip_id: clipId,
    ordinal,
    beat_id: identifier(raw.beat_id, `${path}.beat_id`),
    assignment_id: identifier(raw.assignment_id, `${path}.assignment_id`),
    evidence_ref: evidenceRef,
    asset_id: assetId,
    asset_sha256: assetDigest,
    start_ms: start,
    end_ms: end,
    fit: exactString(raw.fit, "cover", `${path}.fit`),
    audio_enabled: raw.audio_enabled === false ? false as const : fail(`${path}.audio_enabled`),
  };
  if (rawKind === "image") {
    const reference = ASSET_REF.exec(evidenceRef);
    if (reference === null || reference[1] !== assetId) return fail(`${path}.evidence_ref`);
    if (!imageHasAnalysis) return { ...base, media_kind: "image" };
    const analysisRunId = identifier(raw.analysis_run_id, `${path}.analysis_run_id`);
    if (!ANALYSIS_RUN_ID.test(analysisRunId)) return fail(`${path}.analysis_run_id`);
    return {
      ...base,
      media_kind: "image",
      analysis_run_id: analysisRunId,
      analysis_revision: integer(raw.analysis_revision, `${path}.analysis_revision`, 1),
      analysis_content_sha256: digest(raw.analysis_content_sha256, `${path}.analysis_content_sha256`),
      source_binding_sha256: digest(raw.source_binding_sha256, `${path}.source_binding_sha256`),
    };
  }
  const spanId = identifier(raw.span_id, `${path}.span_id`);
  const spanMatch = SPAN_ID.exec(spanId);
  const ordinaryReference = SPAN_REF.exec(evidenceRef);
  const analysisRunId = identifier(raw.analysis_run_id, `${path}.analysis_run_id`);
  const analysisRevision = integer(raw.analysis_revision, `${path}.analysis_revision`, 1);
  const inputDigest = digest(raw.input_asset_sha256, `${path}.input_asset_sha256`);
  const sourceIn = integer(raw.source_in_ms, `${path}.source_in_ms`, 0, Number.MAX_SAFE_INTEGER);
  const sourceOut = integer(raw.source_out_ms, `${path}.source_out_ms`, 1, Number.MAX_SAFE_INTEGER);
  if (
    spanMatch === null
    || !assetId.startsWith("asset_")
    || spanMatch[1] !== assetId.slice("asset_".length)
    || Number(spanMatch[2]) !== analysisRevision
    || !ANALYSIS_RUN_ID.test(analysisRunId)
    || inputDigest !== assetDigest
    || sourceOut <= sourceIn
    || sourceOut - sourceIn !== end - start
  ) return fail(path);
  const videoBase = {
    ...base,
    media_kind: "video" as const,
    span_id: spanId,
    analysis_run_id: analysisRunId,
    analysis_revision: analysisRevision,
    input_asset_sha256: inputDigest,
    source_in_ms: sourceIn,
    source_out_ms: sourceOut,
  };
  if (residualReference === null) {
    if (ordinaryReference === null || ordinaryReference[1] !== spanId) {
      return fail(`${path}.evidence_ref`);
    }
    return videoBase;
  }
  const residualId = string(raw.residual_id, `${path}.residual_id`);
  const parentSegmentId = string(raw.parent_segment_id, `${path}.parent_segment_id`);
  if (!RESIDUAL_ID.test(residualId) || !SPAN_ID.test(parentSegmentId)) {
    return fail(path);
  }
  const normalizedResidualBinding = residualBinding(raw.residual_binding, path);
  if (
    residualReference[1] !== residualId
    || normalizedResidualBinding.residual_id !== residualId
    || normalizedResidualBinding.parent_segment_id !== parentSegmentId
    || spanId !== parentSegmentId
    || normalizedResidualBinding.asset_id !== assetId
    || normalizedResidualBinding.asset_sha256 !== assetDigest
    || normalizedResidualBinding.analysis_run_id !== analysisRunId
    || normalizedResidualBinding.analysis_revision !== analysisRevision
    || normalizedResidualBinding.input_asset_sha256 !== inputDigest
    || sourceIn < normalizedResidualBinding.source_in_ms
    || sourceOut > normalizedResidualBinding.source_out_ms
  ) return fail(path);
  return {
    ...videoBase,
    residual_id: residualId,
    parent_segment_id: parentSegmentId,
    residual_binding: normalizedResidualBinding,
  };
}

function timelineDocument(value: unknown): CanonicalTimelineDocument {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "timeline_id",
      "project_id",
      "revision",
      "parent",
      "blueprint_binding",
      "coverage_binding",
      "compiler",
      "output",
      "tracks",
    ],
    "timeline.timeline",
  );
  const schemaVersion = oneOf(
    raw.schema_version,
    ["1", "2"] as const,
    "timeline.timeline.schema_version",
  );
  const revision = integer(raw.revision, "timeline.timeline.revision", 1);
  const parent = raw.parent === null
    ? null
    : (() => {
      const row = closedRecord(
        raw.parent,
        ["revision", "content_sha256"],
        "timeline.timeline.parent",
      );
      return {
        revision: integer(row.revision, "timeline.timeline.parent.revision", 1),
        content_sha256: digest(row.content_sha256, "timeline.timeline.parent.content_sha256"),
      };
    })();
  if ((revision === 1) !== (parent === null) || (parent && parent.revision !== revision - 1)) {
    return fail("timeline.timeline.parent");
  }
  const compiler = closedRecord(
    raw.compiler,
    ["id", "media", "transitions", "audio", "subtitles"],
    "timeline.timeline.compiler",
  );
  const output = closedRecord(
    raw.output,
    ["duration_ms", "aspect_ratio"],
    "timeline.timeline.output",
  );
  const tracks = array(raw.tracks, "timeline.timeline.tracks");
  if (tracks.length !== 1) return fail("timeline.timeline.tracks");
  const track = closedRecord(
    tracks[0],
    ["track_id", "kind", "clips"],
    "timeline.timeline.tracks[0]",
  );
  const trackId = identifier(track.track_id, "timeline.timeline.tracks[0].track_id");
  if (!/^track_[0-9a-f]{24}$/.test(trackId)) return fail("timeline.timeline.tracks[0].track_id");
  const rawClips = array(track.clips, "timeline.timeline.tracks[0].clips");
  if (rawClips.length < 1 || rawClips.length > MAX_CLIPS) {
    return fail("timeline.timeline.tracks[0].clips");
  }
  const clips: CanonicalTimelineClip[] = [];
  let cursor = 0;
  rawClips.forEach((item, index) => {
    const normalized = clip(item, index, cursor);
    clips.push(normalized);
    cursor = normalized.end_ms;
  });
  const identities = schemaVersion === "1"
    ? [
      clips.map((item) => item.clip_id),
      clips.map((item) => item.beat_id),
      clips.map((item) => item.assignment_id),
      clips.map((item) => item.evidence_ref),
    ]
    : [clips.map((item) => item.clip_id)];
  if (identities.some((items) => new Set(items).size !== items.length)) {
    return fail("timeline.timeline.tracks[0].clips.identities");
  }
  const duration = integer(
    output.duration_ms,
    "timeline.timeline.output.duration_ms",
    1,
    Number.MAX_SAFE_INTEGER,
  );
  if (cursor !== duration) return fail("timeline.timeline.output.duration_ms");
  return {
    object: exactString(raw.object, "memolens.canonical_timeline", "timeline.timeline.object"),
    schema_version: schemaVersion,
    timeline_id: identifier(raw.timeline_id, "timeline.timeline.timeline_id"),
    project_id: identifier(raw.project_id, "timeline.timeline.project_id"),
    revision,
    parent,
    blueprint_binding: blueprintBinding(raw.blueprint_binding, "timeline.timeline.blueprint_binding"),
    coverage_binding: coverageBinding(raw.coverage_binding, "timeline.timeline.coverage_binding"),
    compiler: {
      id: exactString(compiler.id, "memolens.timeline-lowerer/v1", "timeline.timeline.compiler.id"),
      media: exactString(compiler.media, "image_and_video_span", "timeline.timeline.compiler.media"),
      transitions: exactString(compiler.transitions, "hard_cut", "timeline.timeline.compiler.transitions"),
      audio: exactString(compiler.audio, "silent", "timeline.timeline.compiler.audio"),
      subtitles: exactString(compiler.subtitles, "none", "timeline.timeline.compiler.subtitles"),
    },
    output: {
      duration_ms: duration,
      aspect_ratio: oneOf(
        output.aspect_ratio,
        ["16:9", "9:16", "1:1", "4:5"] as const,
        "timeline.timeline.output.aspect_ratio",
      ),
    },
    tracks: [{
      track_id: trackId,
      kind: exactString(track.kind, "primary_visual", "timeline.timeline.tracks[0].kind"),
      clips,
    }],
  };
}

function sourceBinding(
  value: unknown,
  clipValue: CanonicalTimelineClip,
  index: number,
): CanonicalTimelineSourceBinding {
  const path = `timeline.source_bindings[${index}]`;
  const commonFields = [
    "clip_id",
    "evidence_ref",
    "asset_id",
    "asset_sha256",
    "asset_source_id",
    "coverage_proof_sha256",
    "media_kind",
  ];
  const videoFields = [
    "span_id",
    "analysis_run_id",
    "analysis_revision",
    "input_asset_sha256",
    "source_in_ms",
    "source_out_ms",
  ];
  const residualFields = ["residual_id", "parent_segment_id", "residual_binding"];
  const residualClip = clipValue.media_kind === "video" && "residual_id" in clipValue;
  const currentImageClip = clipValue.media_kind === "image" && "analysis_run_id" in clipValue;
  const raw = closedRecord(
    value,
    clipValue.media_kind === "video"
      ? [...commonFields, ...videoFields, ...(residualClip ? residualFields : [])]
      : [...commonFields, ...(currentImageClip ? IMAGE_ANALYSIS_FIELDS : [])],
    path,
  );
  const sourceId = identifier(raw.asset_source_id, `${path}.asset_source_id`);
  if (!SOURCE_ID.test(sourceId)) return fail(`${path}.asset_source_id`);
  const comparisonFields = clipValue.media_kind === "video"
    ? [
      "clip_id",
      "evidence_ref",
      "asset_id",
      "asset_sha256",
      "media_kind",
      ...videoFields,
      ...(residualClip ? ["residual_id", "parent_segment_id"] : []),
    ]
    : ["clip_id", "evidence_ref", "asset_id", "asset_sha256", "media_kind", ...(currentImageClip ? IMAGE_ANALYSIS_FIELDS : [])];
  if (comparisonFields.some((field) => raw[field] !== clipValue[field as keyof CanonicalTimelineClip])) {
    return fail(path);
  }
  const base = {
    clip_id: string(raw.clip_id, `${path}.clip_id`),
    evidence_ref: string(raw.evidence_ref, `${path}.evidence_ref`),
    asset_id: string(raw.asset_id, `${path}.asset_id`),
    asset_sha256: digest(raw.asset_sha256, `${path}.asset_sha256`),
    asset_source_id: sourceId,
    coverage_proof_sha256: digest(
      raw.coverage_proof_sha256,
      `${path}.coverage_proof_sha256`,
    ),
  };
  if (clipValue.media_kind === "image") {
    return currentImageClip ? {
      ...base,
      media_kind: "image",
      analysis_run_id: clipValue.analysis_run_id,
      analysis_revision: clipValue.analysis_revision,
      analysis_content_sha256: clipValue.analysis_content_sha256,
      source_binding_sha256: clipValue.source_binding_sha256,
    } : { ...base, media_kind: "image" };
  }
  const videoBinding = {
    ...base,
    media_kind: "video" as const,
    span_id: string(raw.span_id, `${path}.span_id`),
    analysis_run_id: string(raw.analysis_run_id, `${path}.analysis_run_id`),
    analysis_revision: integer(raw.analysis_revision, `${path}.analysis_revision`, 1),
    input_asset_sha256: digest(raw.input_asset_sha256, `${path}.input_asset_sha256`),
    source_in_ms: integer(raw.source_in_ms, `${path}.source_in_ms`, 0, Number.MAX_SAFE_INTEGER),
    source_out_ms: integer(raw.source_out_ms, `${path}.source_out_ms`, 1, Number.MAX_SAFE_INTEGER),
  };
  if (!residualClip) return videoBinding;
  const normalizedResidualBinding = residualBinding(raw.residual_binding, path);
  if (
    !sameValue(normalizedResidualBinding, clipValue.residual_binding)
    || normalizedResidualBinding.asset_source_id !== sourceId
  ) return fail(path);
  return {
    ...videoBinding,
    residual_id: string(raw.residual_id, `${path}.residual_id`),
    parent_segment_id: string(raw.parent_segment_id, `${path}.parent_segment_id`),
    residual_binding: normalizedResidualBinding,
  };
}

function freshness(value: unknown): CanonicalTimelineWorkspace["freshness"] {
  const raw = closedRecord(value, ["state", "reasons"], "timeline.freshness");
  const state = oneOf(
    raw.state,
    ["missing", "current", "stale_blueprint", "stale_coverage", "stale_source_binding"] as const,
    "timeline.freshness.state",
  );
  const reasons = array(raw.reasons, "timeline.freshness.reasons").map((item, index) => (
    oneOf(
      item,
      [
        "blueprint_head_changed",
        "coverage_head_changed",
        "coverage_plan_stale_blueprint",
        "coverage_plan_stale_evidence",
        "fixed_source_binding_changed",
      ] as const,
      `timeline.freshness.reasons[${index}]`,
    )
  ));
  const exactReason = reasons.length === 1 ? reasons[0] : null;
  const consistent = state === "missing" || state === "current"
    ? reasons.length === 0
    : state === "stale_blueprint"
      ? exactReason === "blueprint_head_changed"
      : state === "stale_source_binding"
        ? exactReason === "fixed_source_binding_changed"
        : exactReason === "coverage_head_changed"
          || exactReason === "coverage_plan_stale_blueprint"
          || exactReason === "coverage_plan_stale_evidence";
  if (!consistent) return fail("timeline.freshness");
  return { state, reasons };
}

export function normalizeCanonicalTimelineWorkspace(value: unknown): CanonicalTimelineWorkspace {
  const raw = closedRecord(
    value,
    [
      "object",
      "schema_version",
      "database_uuid",
      "project_id",
      "head",
      "selected_revision",
      "freshness",
      "timeline",
      "source_bindings",
      "lifecycle",
    ],
    "timeline",
  );
  const databaseUuid = string(raw.database_uuid, "timeline.database_uuid");
  if (!DATABASE_UUID.test(databaseUuid)) return fail("timeline.database_uuid");
  const projectId = identifier(raw.project_id, "timeline.project_id");
  const currentHead = raw.head === null ? null : head(raw.head, "timeline.head");
  const selected = raw.selected_revision === null
    ? null
    : selectedRevision(raw.selected_revision, "timeline.selected_revision");
  const state = freshness(raw.freshness);
  const document = raw.timeline === null ? null : timelineDocument(raw.timeline);
  const rawSources = array(raw.source_bindings, "timeline.source_bindings");
  if (document !== null && rawSources.length !== document.tracks[0].clips.length) {
    return fail("timeline.source_bindings");
  }
  const sources = document === null
    ? []
    : rawSources.map((item, index) => sourceBinding(item, document.tracks[0].clips[index], index));
  const lifecycle = closedRecord(
    raw.lifecycle,
    ["state", "approval", "exportable"],
    "timeline.lifecycle",
  );
  if (
    (selected === null) !== (document === null)
    || (selected === null) !== (currentHead === null)
    || (selected === null) !== (state.state === "missing")
    || (selected === null) !== (rawSources.length === 0)
    || lifecycle.approval !== "not_established"
    || lifecycle.exportable !== false
    || lifecycle.state !== (selected === null ? "not_materialized" : "draft")
  ) return fail("timeline.consistency");
  if (selected && document && currentHead) {
    const selectedHead = { ...selected };
    delete (selectedHead as Partial<CanonicalTimelineSelectedRevision>).is_head;
    if (
      selected.is_head !== sameValue(selectedHead, currentHead)
      || document.project_id !== projectId
      || document.revision !== selected.revision
      || document.timeline_id !== selected.timeline_id
      || !sameValue(document.blueprint_binding, selected.blueprint_binding)
      || !sameValue(document.coverage_binding, selected.coverage_binding)
    ) return fail("timeline.consistency");
  }
  const sourceIdentities = new Map<string, string>();
  const evidenceIdentities = new Map<string, string>();
  for (const source of sources) {
    const identity = `${source.asset_id}:${source.asset_sha256}`;
    const existingIdentity = sourceIdentities.get(source.asset_source_id);
    if (existingIdentity !== undefined && existingIdentity !== identity) {
      return fail("timeline.source_bindings");
    }
    sourceIdentities.set(source.asset_source_id, identity);
    const evidenceIdentity = JSON.stringify(source.media_kind === "image"
      ? {
        media_kind: source.media_kind,
        asset_id: source.asset_id,
        asset_sha256: source.asset_sha256,
        asset_source_id: source.asset_source_id,
        coverage_proof_sha256: source.coverage_proof_sha256,
        ...("analysis_run_id" in source ? {
          analysis_run_id: source.analysis_run_id,
          analysis_revision: source.analysis_revision,
          analysis_content_sha256: source.analysis_content_sha256,
          source_binding_sha256: source.source_binding_sha256,
        } : {}),
      }
      : {
        media_kind: source.media_kind,
        asset_id: source.asset_id,
        asset_sha256: source.asset_sha256,
        asset_source_id: source.asset_source_id,
        coverage_proof_sha256: source.coverage_proof_sha256,
        span_id: source.span_id,
        analysis_run_id: source.analysis_run_id,
        analysis_revision: source.analysis_revision,
        input_asset_sha256: source.input_asset_sha256,
        ...("residual_id" in source
          ? {
            residual_id: source.residual_id,
            parent_segment_id: source.parent_segment_id,
            residual_binding: source.residual_binding,
          }
          : {}),
      });
    const existingEvidenceIdentity = evidenceIdentities.get(source.evidence_ref);
    if (
      existingEvidenceIdentity !== undefined
      && existingEvidenceIdentity !== evidenceIdentity
    ) return fail("timeline.source_bindings");
    evidenceIdentities.set(source.evidence_ref, evidenceIdentity);
  }
  return {
    object: exactString(raw.object, "canonical_timeline.workspace", "timeline.object"),
    schema_version: exactString(raw.schema_version, "1", "timeline.schema_version"),
    database_uuid: databaseUuid,
    project_id: projectId,
    head: currentHead,
    selected_revision: selected,
    freshness: state,
    timeline: document,
    source_bindings: sources,
    lifecycle: {
      state: exactString(
        lifecycle.state,
        selected === null ? "not_materialized" : "draft",
        "timeline.lifecycle.state",
      ),
      approval: exactString(
        lifecycle.approval,
        "not_established",
        "timeline.lifecycle.approval",
      ),
      exportable: lifecycle.exportable === false
        ? false
        : fail("timeline.lifecycle.exportable"),
    },
  };
}

export function normalizeCanonicalTimelineMaterializeCommandResult(
  value: unknown,
): CanonicalTimelineMaterializeCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "command_type", "operation_id", "result"],
    "timeline_command",
  );
  const result = closedRecord(raw.result, ["kind", "result_head"], "timeline_command.result");
  const operationId = identifier(raw.operation_id, "timeline_command.operation_id");
  const resultHead = head(result.result_head, "timeline_command.result.result_head");
  if (resultHead.operation_id !== operationId) return fail("timeline_command.result");
  return {
    object: exactString(raw.object, "canonical_timeline.command_result", "timeline_command.object"),
    schema_version: exactString(raw.schema_version, "1", "timeline_command.schema_version"),
    project_id: identifier(raw.project_id, "timeline_command.project_id"),
    command_type: exactString(
      raw.command_type,
      "timeline.materialize_first_cut",
      "timeline_command.command_type",
    ),
    operation_id: operationId,
    result: {
      kind: exactString(result.kind, "revision_created", "timeline_command.result.kind"),
      result_head: resultHead,
    },
  };
}

export function normalizeCanonicalTimelineReconcileCommandResult(
  value: unknown,
): CanonicalTimelineReconcileCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "command_type", "operation_id", "result"],
    "timeline_reconcile_command",
  );
  const result = closedRecord(
    raw.result,
    ["kind", "result_head"],
    "timeline_reconcile_command.result",
  );
  const operationId = identifier(
    raw.operation_id,
    "timeline_reconcile_command.operation_id",
  );
  const resultHead = head(
    result.result_head,
    "timeline_reconcile_command.result.result_head",
  );
  if (resultHead.operation_id !== operationId) {
    return fail("timeline_reconcile_command.result");
  }
  return {
    object: exactString(
      raw.object,
      "canonical_timeline.command_result",
      "timeline_reconcile_command.object",
    ),
    schema_version: exactString(
      raw.schema_version,
      "1",
      "timeline_reconcile_command.schema_version",
    ),
    project_id: identifier(raw.project_id, "timeline_reconcile_command.project_id"),
    command_type: exactString(
      raw.command_type,
      "timeline.reconcile_from_coverage",
      "timeline_reconcile_command.command_type",
    ),
    operation_id: operationId,
    result: {
      kind: exactString(
        result.kind,
        "revision_created",
        "timeline_reconcile_command.result.kind",
      ),
      result_head: resultHead,
    },
  };
}

export function normalizeCanonicalTimelineEdit(value: unknown): CanonicalTimelineEdit {
  const raw = record(value, "timeline_edit");
  const op = oneOf(
    raw.op,
    ["move_clip", "trim_clip", "set_clip_duration", "replace_clip"] as const,
    "timeline_edit.op",
  );
  if (op === "move_clip") {
    const edit = closedRecord(raw, ["op", "clip_id", "to_index"], "timeline_edit");
    return {
      op,
      clip_id: identifier(edit.clip_id, "timeline_edit.clip_id"),
      to_index: integer(edit.to_index, "timeline_edit.to_index", 0, MAX_CLIPS - 1),
    };
  }
  if (op === "trim_clip") {
    const edit = closedRecord(
      raw,
      ["op", "clip_id", "source_in_ms", "source_out_ms"],
      "timeline_edit",
    );
    const sourceInMs = integer(
      edit.source_in_ms,
      "timeline_edit.source_in_ms",
      0,
      Number.MAX_SAFE_INTEGER,
    );
    const sourceOutMs = integer(
      edit.source_out_ms,
      "timeline_edit.source_out_ms",
      1,
      Number.MAX_SAFE_INTEGER,
    );
    if (sourceOutMs - sourceInMs < 100) return fail("timeline_edit.source_out_ms");
    return {
      op,
      clip_id: identifier(edit.clip_id, "timeline_edit.clip_id"),
      source_in_ms: sourceInMs,
      source_out_ms: sourceOutMs,
    };
  }
  if (op === "set_clip_duration") {
    const edit = closedRecord(
      raw,
      ["op", "clip_id", "duration_ms"],
      "timeline_edit",
    );
    return {
      op,
      clip_id: identifier(edit.clip_id, "timeline_edit.clip_id"),
      duration_ms: integer(edit.duration_ms, "timeline_edit.duration_ms", 1),
    };
  }
  const edit = closedRecord(
    raw,
    ["op", "clip_id", "assignment_id"],
    "timeline_edit",
  );
  return {
    op,
    clip_id: identifier(edit.clip_id, "timeline_edit.clip_id"),
    assignment_id: identifier(edit.assignment_id, "timeline_edit.assignment_id"),
  };
}

export function normalizeCanonicalTimelineStructuralEdit(
  value: unknown,
): CanonicalTimelineStructuralEdit {
  const raw = record(value, "timeline_structural_edit");
  const op = oneOf(
    raw.op,
    ["split_clip", "delete_clip"] as const,
    "timeline_structural_edit.op",
  );
  if (op === "delete_clip") {
    const edit = closedRecord(raw, ["op", "clip_id"], "timeline_structural_edit");
    return {
      op,
      clip_id: identifier(edit.clip_id, "timeline_structural_edit.clip_id"),
    };
  }
  const edit = closedRecord(
    raw,
    ["op", "clip_id", "source_split_ms"],
    "timeline_structural_edit",
  );
  return {
    op,
    clip_id: identifier(edit.clip_id, "timeline_structural_edit.clip_id"),
    source_split_ms: integer(
      edit.source_split_ms,
      "timeline_structural_edit.source_split_ms",
      0,
      Number.MAX_SAFE_INTEGER,
    ),
  };
}

export function normalizeCanonicalTimelineEditCommandResult(
  value: unknown,
): CanonicalTimelineEditCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "command_type", "operation_id", "result"],
    "timeline_edit_command",
  );
  const result = closedRecord(
    raw.result,
    ["kind", "result_head", "edit"],
    "timeline_edit_command.result",
  );
  const operationId = identifier(
    raw.operation_id,
    "timeline_edit_command.operation_id",
  );
  const resultHead = head(
    result.result_head,
    "timeline_edit_command.result.result_head",
  );
  if (resultHead.operation_id !== operationId) {
    return fail("timeline_edit_command.result");
  }
  return {
    object: exactString(
      raw.object,
      "canonical_timeline.command_result",
      "timeline_edit_command.object",
    ),
    schema_version: exactString(
      raw.schema_version,
      "1",
      "timeline_edit_command.schema_version",
    ),
    project_id: identifier(raw.project_id, "timeline_edit_command.project_id"),
    command_type: exactString(
      raw.command_type,
      "timeline.apply_edit",
      "timeline_edit_command.command_type",
    ),
    operation_id: operationId,
    result: {
      kind: exactString(
        result.kind,
        "revision_created",
        "timeline_edit_command.result.kind",
      ),
      result_head: resultHead,
      edit: normalizeCanonicalTimelineEdit(result.edit),
    },
  };
}

export function normalizeCanonicalTimelineStructuralEditCommandResult(
  value: unknown,
): CanonicalTimelineStructuralEditCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "command_type", "operation_id", "result"],
    "timeline_structural_edit_command",
  );
  const result = closedRecord(
    raw.result,
    ["kind", "result_head", "structural_edit"],
    "timeline_structural_edit_command.result",
  );
  const operationId = identifier(
    raw.operation_id,
    "timeline_structural_edit_command.operation_id",
  );
  const resultHead = head(
    result.result_head,
    "timeline_structural_edit_command.result.result_head",
  );
  if (resultHead.operation_id !== operationId) {
    return fail("timeline_structural_edit_command.result");
  }
  return {
    object: exactString(
      raw.object,
      "canonical_timeline.command_result",
      "timeline_structural_edit_command.object",
    ),
    schema_version: exactString(
      raw.schema_version,
      "1",
      "timeline_structural_edit_command.schema_version",
    ),
    project_id: identifier(raw.project_id, "timeline_structural_edit_command.project_id"),
    command_type: exactString(
      raw.command_type,
      "timeline.apply_structural_edit",
      "timeline_structural_edit_command.command_type",
    ),
    operation_id: operationId,
    result: {
      kind: exactString(
        result.kind,
        "revision_created",
        "timeline_structural_edit_command.result.kind",
      ),
      result_head: resultHead,
      structural_edit: normalizeCanonicalTimelineStructuralEdit(result.structural_edit),
    },
  };
}

export function normalizeCanonicalTimelineRestoreCommandResult(
  value: unknown,
): CanonicalTimelineRestoreCommandResult {
  const raw = closedRecord(
    value,
    ["object", "schema_version", "project_id", "command_type", "operation_id", "result"],
    "timeline_restore_command",
  );
  const result = closedRecord(
    raw.result,
    ["kind", "result_head", "restore_from"],
    "timeline_restore_command.result",
  );
  const operationId = identifier(
    raw.operation_id,
    "timeline_restore_command.operation_id",
  );
  const resultHead = head(
    result.result_head,
    "timeline_restore_command.result.result_head",
  );
  const restoreFrom = head(
    result.restore_from,
    "timeline_restore_command.result.restore_from",
  ) as CanonicalTimelineRestoreFrom;
  if (
    resultHead.operation_id !== operationId
    || restoreFrom.revision >= resultHead.revision - 1
    || restoreFrom.timeline_id !== resultHead.timeline_id
    || !sameValue(restoreFrom.blueprint_binding, resultHead.blueprint_binding)
    || !sameValue(restoreFrom.coverage_binding, resultHead.coverage_binding)
  ) return fail("timeline_restore_command.result");
  return {
    object: exactString(
      raw.object,
      "canonical_timeline.command_result",
      "timeline_restore_command.object",
    ),
    schema_version: exactString(
      raw.schema_version,
      "1",
      "timeline_restore_command.schema_version",
    ),
    project_id: identifier(raw.project_id, "timeline_restore_command.project_id"),
    command_type: exactString(
      raw.command_type,
      "timeline.restore_revision",
      "timeline_restore_command.command_type",
    ),
    operation_id: operationId,
    result: {
      kind: exactString(
        result.kind,
        "revision_restored",
        "timeline_restore_command.result.kind",
      ),
      result_head: resultHead,
      restore_from: restoreFrom,
    },
  };
}

export function timelineCoverageBindingFromWorkspace(
  coverage: CoveragePlanWorkspace,
): CanonicalTimelineCoverageBinding {
  const selected = coverage.selected_revision;
  if (
    coverage.freshness.state !== "current"
    || coverage.head === null
    || selected === null
    || !selected.is_head
    || coverage.coverage_plan === null
    || selected.revision !== coverage.head.revision
    || selected.content_sha256 !== coverage.head.content_sha256
    || selected.revision !== coverage.coverage_plan.revision
  ) return fail("timeline.coverage_precondition");
  return {
    revision: selected.revision,
    content_sha256: selected.content_sha256,
    evidence_manifest_sha256: selected.evidence_manifest_sha256,
    operation_id: selected.operation_id,
  };
}

export function isSameCanonicalTimelineHead(
  left: CanonicalTimelineHead,
  right: CanonicalTimelineHead,
): boolean {
  return left.revision === right.revision
    && left.revision_sha256 === right.revision_sha256
    && left.timeline_id === right.timeline_id
    && left.timeline_content_sha256 === right.timeline_content_sha256
    && left.operation_id === right.operation_id
    && left.blueprint_binding.revision === right.blueprint_binding.revision
    && left.blueprint_binding.content_sha256 === right.blueprint_binding.content_sha256
    && left.blueprint_binding.semantic_sha256 === right.blueprint_binding.semantic_sha256
    && left.blueprint_binding.operation_id === right.blueprint_binding.operation_id
    && left.coverage_binding.revision === right.coverage_binding.revision
    && left.coverage_binding.content_sha256 === right.coverage_binding.content_sha256
    && left.coverage_binding.evidence_manifest_sha256
      === right.coverage_binding.evidence_manifest_sha256
    && left.coverage_binding.operation_id === right.coverage_binding.operation_id;
}

export function isHistoricalTimelineWorkspaceForCurrentHead(
  current: CanonicalTimelineWorkspace,
  historical: CanonicalTimelineWorkspace,
): boolean {
  if (!isHistoricalTimelineSelectionForCurrentLedger(current, historical)) {
    return false;
  }
  const currentHead = current.head;
  const historicalSelected = historical.selected_revision;
  const historicalDocument = historical.timeline;
  if (
    currentHead === null
    || historicalSelected === null
    || historicalDocument === null
    || current.freshness.state !== "current"
    || historical.freshness.state !== "current"
    || !sameValue(historicalSelected.blueprint_binding, currentHead.blueprint_binding)
    || !sameValue(historicalSelected.coverage_binding, currentHead.coverage_binding)
    || !sameValue(historicalDocument.blueprint_binding, currentHead.blueprint_binding)
    || !sameValue(historicalDocument.coverage_binding, currentHead.coverage_binding)
  ) return false;
  return true;
}

export function isHistoricalTimelineSelectionForCurrentLedger(
  current: CanonicalTimelineWorkspace,
  historical: CanonicalTimelineWorkspace,
): boolean {
  const currentHead = current.head;
  const currentSelected = current.selected_revision;
  const currentDocument = current.timeline;
  const historicalHead = historical.head;
  const historicalSelected = historical.selected_revision;
  const historicalDocument = historical.timeline;
  if (
    currentHead === null
    || currentSelected === null
    || currentDocument === null
    || historicalHead === null
    || historicalSelected === null
    || historicalDocument === null
    || !currentSelected.is_head
    || historicalSelected.is_head
    || !isSameCanonicalTimelineHead(currentHead, currentSelected)
    || currentDocument.timeline_id !== currentHead.timeline_id
    || currentDocument.project_id !== current.project_id
    || currentDocument.revision !== currentHead.revision
    || !sameValue(currentDocument.blueprint_binding, currentHead.blueprint_binding)
    || !sameValue(currentDocument.coverage_binding, currentHead.coverage_binding)
    || current.database_uuid !== historical.database_uuid
    || current.project_id !== historical.project_id
    || !isSameCanonicalTimelineHead(currentHead, historicalHead)
    || historicalSelected.revision >= currentHead.revision
    || historicalSelected.timeline_id !== currentHead.timeline_id
    || historicalDocument.timeline_id !== currentHead.timeline_id
    || historicalDocument.project_id !== current.project_id
    || historicalDocument.revision !== historicalSelected.revision
    || !sameValue(
      historicalSelected.blueprint_binding,
      historicalDocument.blueprint_binding,
    )
    || !sameValue(
      historicalSelected.coverage_binding,
      historicalDocument.coverage_binding,
    )
  ) return false;
  return true;
}

export function isTimelineWorkspaceProjectionForBlueprint(
  workspace: Pick<
    BlueprintProjectWorkspace,
    | "database_uuid"
    | "project_id"
    | "current_blueprint"
    | "coverage"
    | "timeline"
    | "executable"
  >,
  timeline: CanonicalTimelineWorkspace,
): boolean {
  if (
    workspace.database_uuid !== timeline.database_uuid
    || workspace.project_id !== timeline.project_id
    || workspace.timeline.state !== timeline.freshness.state
    || !sameValue(workspace.timeline.head, timeline.head)
    || !sameValue(workspace.timeline.lifecycle, timeline.lifecycle)
    || !sameValue(workspace.executable.current_timeline, timeline.head)
  ) return false;
  if (timeline.timeline === null) {
    return workspace.timeline.blueprint_binding === null
      && workspace.timeline.coverage_binding === null
      && workspace.timeline.clip_count === 0
      && workspace.timeline.source_binding_count === 0;
  }
  const blueprintBinding = timeline.timeline.blueprint_binding;
  const coverageBinding = timeline.timeline.coverage_binding;
  const blueprintMatchesCurrent = blueprintBinding.revision
      === workspace.current_blueprint.revision
    && blueprintBinding.content_sha256 === workspace.current_blueprint.content_sha256
    && blueprintBinding.semantic_sha256 === workspace.current_blueprint.semantic_sha256
    && blueprintBinding.operation_id === workspace.current_blueprint.operation_id;
  const coverageMatchesCurrent = workspace.coverage.state === "current"
    && workspace.coverage.head !== null
    && coverageBinding.revision === workspace.coverage.head.revision
    && coverageBinding.content_sha256 === workspace.coverage.head.content_sha256;
  const upstreamStateMatches = timeline.freshness.state === "current"
    ? blueprintMatchesCurrent && coverageMatchesCurrent
    : timeline.freshness.state === "stale_blueprint"
      ? !blueprintMatchesCurrent
      : timeline.freshness.state === "stale_coverage"
        ? blueprintMatchesCurrent && !coverageMatchesCurrent
        : blueprintMatchesCurrent && coverageMatchesCurrent;
  return (
    timeline.selected_revision?.is_head === true
    && upstreamStateMatches
    && sameValue(workspace.timeline.blueprint_binding, timeline.timeline.blueprint_binding)
    && sameValue(workspace.timeline.coverage_binding, timeline.timeline.coverage_binding)
    && workspace.timeline.clip_count === timeline.timeline.tracks[0].clips.length
    && workspace.timeline.source_binding_count === timeline.source_bindings.length
  );
}
