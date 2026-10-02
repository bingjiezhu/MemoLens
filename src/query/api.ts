import { analyzePrompt } from "./studio";
import type {
  AtlasBasket,
  AtlasAsset,
  AtlasCanonicalImageObservation,
  AtlasLens,
  AtlasMemoryDetail,
  AtlasMode,
  AtlasOverview,
  AtlasQueryPreview,
  AtlasStatus,
  AtlasWorkbench,
  BackendSettingsResponse,
  DesktopIndexingResult,
  DraftResult,
  ParsedQueryPreview,
  PhotoAsset,
  ScopedIndexStatusResponse,
  ToneVariant,
} from "./types";

interface RetrievalApiImage {
  id: string;
  asset_id: string;
  analysis_status: "current";
  analysis_binding: AtlasCanonicalImageObservation["analysis_binding"];
  projection: AtlasCanonicalImageObservation["projection"];
  canonical_image_observation: AtlasCanonicalImageObservation;
  filename: string;
  relative_path: string;
  taken_at: string | null;
  place_name: string | null;
  country: string | null;
  description: string;
  tags: string[];
  score: number;
  matched_terms: string[];
}

interface RetrievalApiResponse {
  id: string;
  status: string;
  message: string | null;
  title?: string | null;
  caption?: string | null;
  notes?: string[];
  candidate_count?: number | null;
  generated_copy?: {
    model: string;
    title: string | null;
    body: string;
    highlights: string[];
    image_count: number;
  } | null;
  parsed_query?: {
    top_k: number;
    date_from: string | null;
    date_to: string | null;
    location_text: string | null;
    descriptive_query: string | null;
    required_terms: string[];
    optional_terms: string[];
    excluded_terms: string[];
  } | null;
  data: RetrievalApiImage[];
}

interface RetrievalCopyApiResponse {
  object?: string;
  message?: string | null;
  title?: string | null;
  caption?: string | null;
  notes?: string[] | null;
  generated_copy?: {
    model: string;
    title: string | null;
    body: string;
    highlights: string[];
    image_count: number;
  } | null;
}

interface DraftCopyUpdate {
  title?: string | null;
  caption?: string | null;
  notes?: string[] | null;
}

interface FetchDraftOptions {
  apiBase?: string;
  imageLibraryDir?: string | null;
  dbPath?: string | null;
  contextAssetIds?: string[];
  onCopyUpdate?: (update: DraftCopyUpdate) => void;
  shouldApplyCopyUpdate?: () => boolean;
  signal?: AbortSignal;
}

interface SaveBackendSettingsInput {
  apiBase?: string;
  imageLibraryDir: string;
  dbPath: string;
  processImageWidth: number;
  visionProfileName: string;
  queryProfileName: string;
}

interface IndexingApiResponse {
  status: string;
  message?: string | null;
  meta?: {
    image_dir?: string;
    db_path?: string;
    indexed_count?: number;
    skipped_count?: number;
    error_count?: number;
  };
  errors?: Array<{ message?: string | null }>;
}

interface AtlasRequestOptions {
  apiBase?: string;
  dbPath?: string | null;
  imageLibraryDir?: string | null;
  mode?: AtlasMode;
  lens?: AtlasLens;
  text?: string;
  noPeople?: boolean;
  minQuality?: number | null;
  showDuplicates?: boolean;
  limit?: number;
  clusterId?: string | null;
  assetIds?: string[];
  assetObservations?: Array<AtlasCanonicalImageObservation | undefined>;
  selectedMemoryIds?: string[];
  inspirationId?: string | null;
  previewWidth?: number;
  signal?: AbortSignal;
}

interface InspirationApiResponse {
  object?: string;
  status?: string;
  suggestions?: string[];
  message?: string | null;
}

const SURFACE_TINTS = [
  "#d8cdbd",
  "#c6d5ca",
  "#e2d7c9",
  "#c9d0d7",
  "#d9c8c3",
  "#d7d9ce",
  "#cfc5b7",
  "#d9d2c7",
  "#c7d4d0",
];

const SLOT_KEYWORDS: Array<{ slot: string; keywords: string[] }> = [
  { slot: "cover", keywords: ["cover", "hero", "wide", "landscape", "beach", "coast"] },
  { slot: "portrait", keywords: ["portrait", "person", "face"] },
  { slot: "detail", keywords: ["detail", "coffee", "food", "close", "still life"] },
  { slot: "city", keywords: ["city", "street", "skyline", "bridge", "building"] },
  { slot: "walk", keywords: ["walk", "road", "path", "trail"] },
  { slot: "quiet", keywords: ["quiet", "light", "window", "interior", "plant"] },
];

function normalizeDisplayText(value: string | null | undefined, fallback: string): string {
  const cleaned = String(value ?? "").replace(/\s+/g, " ").trim();
  return cleaned || fallback;
}

function normalizeTags(tags: string[]): string[] {
  return tags
    .map((tag) => normalizeDisplayText(tag, ""))
    .filter((tag, index, list) => tag.length > 0 && list.indexOf(tag) === index);
}

function encodeRelativePath(relativePath: string): string {
  return relativePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

export function buildPreviewImageUrl(
  apiBase: string,
  relativePath: string,
  imageLibraryDir: string | null | undefined,
  width = 900,
): string {
  const encodedRelativePath = encodeRelativePath(relativePath);
  const params = new URLSearchParams({
    width: String(Math.max(120, Math.min(1800, Math.round(width)))),
  });
  if (imageLibraryDir && imageLibraryDir.trim().length > 0) {
    params.set("root_path", imageLibraryDir);
  }

  return `${apiBase.replace(/\/$/, "")}/v1/library/previews/${encodedRelativePath}?${params.toString()}`;
}

function appendAtlasSearchParams(params: URLSearchParams, options: AtlasRequestOptions): void {
  if (options.dbPath && options.dbPath.trim().length > 0) {
    params.set("db_path", options.dbPath);
  }
  if (options.mode) {
    params.set("mode", options.mode);
  }
  if (options.lens) {
    params.set("lens", options.lens);
  }
  if (options.text && options.text.trim().length > 0) {
    params.set("query", options.text.trim());
  }
  if (typeof options.noPeople === "boolean") {
    params.set("no_people", String(options.noPeople));
  }
  if (typeof options.minQuality === "number") {
    params.set("min_quality", String(options.minQuality));
  }
  if (typeof options.showDuplicates === "boolean") {
    params.set("show_duplicates", String(options.showDuplicates));
  }
  if (typeof options.limit === "number") {
    params.set("limit", String(options.limit));
  }
  if (options.clusterId) {
    params.set("cluster_id", options.clusterId);
  }
}

function buildAtlasPayload(options: AtlasRequestOptions): Record<string, unknown> {
  return {
    db_path: options.dbPath && options.dbPath.trim().length > 0 ? options.dbPath : undefined,
    image_library_dir:
      options.imageLibraryDir && options.imageLibraryDir.trim().length > 0
        ? options.imageLibraryDir
        : undefined,
    mode: options.mode,
    lens: options.lens,
    text: options.text,
    no_people: options.noPeople,
    min_quality: options.minQuality ?? undefined,
    show_duplicates: options.showDuplicates,
    limit: options.limit,
    cluster_id: options.clusterId ?? undefined,
    asset_ids: options.assetIds && options.assetIds.length > 0 ? options.assetIds : undefined,
    selected_memory_ids:
      options.selectedMemoryIds && options.selectedMemoryIds.length > 0
        ? options.selectedMemoryIds
        : undefined,
    inspiration_id: options.inspirationId ?? undefined,
  };
}

type UnknownRecord = Record<string, unknown>;

const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: UnknownRecord, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function invalidAtlasObservation(): never {
  throw new Error(
    "Atlas response did not preserve an exact verified-current image observation.",
  );
}

function normalizeAnalysisBinding(
  value: unknown,
): AtlasCanonicalImageObservation["analysis_binding"] {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ["analysis_run_id", "revision", "content_sha256"])
    || typeof value.analysis_run_id !== "string"
    || value.analysis_run_id.length === 0
    || value.analysis_run_id.length > 200
    || !Number.isSafeInteger(value.revision)
    || Number(value.revision) < 1
    || typeof value.content_sha256 !== "string"
    || !SHA256_PATTERN.test(value.content_sha256)
  ) {
    return invalidAtlasObservation();
  }
  return {
    analysis_run_id: value.analysis_run_id,
    revision: Number(value.revision),
    content_sha256: value.content_sha256,
  };
}

function normalizeProjection(
  value: unknown,
): AtlasCanonicalImageObservation["projection"] {
  const fields = [
    "status",
    "generation_id",
    "processing_generation_id",
    "receipt_sha256",
    "row_sha256",
    "reason_code",
  ] as const;
  if (
    !isRecord(value)
    || !hasExactKeys(value, fields)
    || value.status !== "current"
    || typeof value.generation_id !== "string"
    || value.generation_id.length === 0
    || value.generation_id.length > 200
    || typeof value.processing_generation_id !== "string"
    || value.processing_generation_id.length === 0
    || value.processing_generation_id.length > 200
    || typeof value.receipt_sha256 !== "string"
    || !SHA256_PATTERN.test(value.receipt_sha256)
    || typeof value.row_sha256 !== "string"
    || !SHA256_PATTERN.test(value.row_sha256)
    || value.reason_code !== null
  ) {
    return invalidAtlasObservation();
  }
  return {
    status: "current",
    generation_id: value.generation_id,
    processing_generation_id: value.processing_generation_id,
    receipt_sha256: value.receipt_sha256,
    row_sha256: value.row_sha256,
    reason_code: null,
  };
}

const IMAGE_STAGE_NAMES = [
  "metadata",
  "geocode",
  "vision",
  "embedding",
  "quality",
] as const;
const IMAGE_STAGE_STATUSES = new Set([
  "succeeded",
  "partial",
  "unsupported",
  "disabled",
  "failed",
  "unknown",
]);

function normalizeStages(
  value: unknown,
): AtlasCanonicalImageObservation["stages"] {
  if (!isRecord(value) || !hasExactKeys(value, IMAGE_STAGE_NAMES)) {
    return invalidAtlasObservation();
  }
  const normalized: UnknownRecord = {};
  for (const name of IMAGE_STAGE_NAMES) {
    const stage = value[name];
    if (
      !isRecord(stage)
      || !hasExactKeys(stage, ["status", "provenance", "output", "reason_code"])
      || typeof stage.status !== "string"
      || !IMAGE_STAGE_STATUSES.has(stage.status)
      || !isRecord(stage.provenance)
      || !hasExactKeys(stage.provenance, [
        "producer_id",
        "producer_version",
        "model_id",
        "model_version",
        "rule_id",
        "rule_version",
      ])
      || typeof stage.provenance.producer_id !== "string"
      || typeof stage.provenance.producer_version !== "string"
      || (stage.provenance.model_id !== null
        && typeof stage.provenance.model_id !== "string")
      || (stage.provenance.model_version !== null
        && typeof stage.provenance.model_version !== "string")
      || (stage.provenance.rule_id !== null
        && typeof stage.provenance.rule_id !== "string")
      || (stage.provenance.rule_version !== null
        && typeof stage.provenance.rule_version !== "string")
      || (stage.output !== null && !isRecord(stage.output))
      || (stage.reason_code !== null && typeof stage.reason_code !== "string")
    ) {
      return invalidAtlasObservation();
    }
    normalized[name] = {
      status: stage.status,
      provenance: { ...stage.provenance },
      output: stage.output === null ? null : { ...stage.output },
      reason_code: stage.reason_code,
    };
  }
  return normalized as AtlasCanonicalImageObservation["stages"];
}

function sameAnalysisBinding(
  left: AtlasCanonicalImageObservation["analysis_binding"],
  right: AtlasCanonicalImageObservation["analysis_binding"],
): boolean {
  return left.analysis_run_id === right.analysis_run_id
    && left.revision === right.revision
    && left.content_sha256 === right.content_sha256;
}

function sameProjection(
  left: AtlasCanonicalImageObservation["projection"],
  right: AtlasCanonicalImageObservation["projection"],
): boolean {
  return left.status === right.status
    && left.generation_id === right.generation_id
    && left.processing_generation_id === right.processing_generation_id
    && left.receipt_sha256 === right.receipt_sha256
    && left.row_sha256 === right.row_sha256
    && left.reason_code === right.reason_code;
}

export function normalizeAtlasCanonicalImageObservation(
  value: unknown,
  expectedAssetId: string,
): AtlasCanonicalImageObservation {
  if (
    !isRecord(value)
    || !hasExactKeys(value, [
      "object",
      "schema_version",
      "status",
      "authority",
      "provenance_status",
      "asset_id",
      "analysis_binding",
      "source_binding_sha256",
      "projection",
      "stages",
      "reason_code",
    ])
    || value.object !== "memolens.canonical_image_observation"
    || value.schema_version !== "1"
    || value.status !== "current"
    || value.authority !== "canonical_image_analysis"
    || value.provenance_status !== "verified_current"
    || value.asset_id !== expectedAssetId
    || typeof value.source_binding_sha256 !== "string"
    || !SHA256_PATTERN.test(value.source_binding_sha256)
    || value.reason_code !== null
  ) {
    return invalidAtlasObservation();
  }
  return {
    object: "memolens.canonical_image_observation",
    schema_version: "1",
    status: "current",
    authority: "canonical_image_analysis",
    provenance_status: "verified_current",
    asset_id: expectedAssetId,
    analysis_binding: normalizeAnalysisBinding(value.analysis_binding),
    source_binding_sha256: value.source_binding_sha256,
    projection: normalizeProjection(value.projection),
    stages: normalizeStages(value.stages),
    reason_code: null,
  };
}

const ATLAS_GENERATE_MAX_CONTEXT_ASSETS = 24;
const ATLAS_ASSET_OBSERVATIONS_MAX_CANONICAL_BYTES = 256 * 1024;

function exactAtlasGenerateObservations(
  options: AtlasRequestOptions,
): AtlasCanonicalImageObservation[] | undefined {
  const assetIds = options.assetIds;
  const observations = options.assetObservations;
  if (!assetIds || assetIds.length === 0) {
    if (observations && observations.length > 0) {
      return invalidAtlasObservation();
    }
    return undefined;
  }
  if (
    assetIds.length > ATLAS_GENERATE_MAX_CONTEXT_ASSETS
    || new Set(assetIds).size !== assetIds.length
    || !observations
    || observations.length !== assetIds.length
  ) {
    return invalidAtlasObservation();
  }
  const normalized = observations.map((observation, index) =>
    normalizeAtlasCanonicalImageObservation(observation, assetIds[index]),
  );
  if (
    new TextEncoder().encode(JSON.stringify(normalized)).byteLength
    > ATLAS_ASSET_OBSERVATIONS_MAX_CANONICAL_BYTES
  ) {
    return invalidAtlasObservation();
  }
  return normalized;
}

export function normalizeAtlasAsset(value: unknown): AtlasAsset {
  if (
    !isRecord(value)
    || value.object !== "atlas.asset"
    || typeof value.id !== "string"
    || value.id.length === 0
    || value.asset_id !== value.id
    || value.analysis_status !== "current"
  ) {
    return invalidAtlasObservation();
  }
  const observation = normalizeAtlasCanonicalImageObservation(
    value.canonical_image_observation,
    value.id,
  );
  const binding = normalizeAnalysisBinding(value.analysis_binding);
  const projection = normalizeProjection(value.projection);
  if (
    !sameAnalysisBinding(binding, observation.analysis_binding)
    || !sameProjection(projection, observation.projection)
  ) {
    return invalidAtlasObservation();
  }
  return {
    ...value,
    asset_id: value.id,
    analysis_status: "current",
    analysis_binding: binding,
    projection,
    canonical_image_observation: observation,
  } as AtlasAsset;
}

function looksLikeAtlasAsset(value: UnknownRecord): boolean {
  return value.object === "atlas.asset"
    || (
      "relative_path" in value
      && "layout_version" in value
      && "cluster_id" in value
      && "quality_score" in value
    );
}

function normalizeAtlasTree<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => normalizeAtlasTree(item)) as T;
  }
  if (!isRecord(value)) {
    return value;
  }
  if (looksLikeAtlasAsset(value)) {
    return normalizeAtlasAsset(value) as T;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [key, normalizeAtlasTree(item)]),
  ) as T;
}

function normalizeAtlasRetrievalImage(value: unknown): RetrievalApiImage {
  if (
    !isRecord(value)
    || typeof value.id !== "string"
    || value.id.length === 0
    || value.asset_id !== value.id
    || value.analysis_status !== "current"
  ) {
    return invalidAtlasObservation();
  }
  const observation = normalizeAtlasCanonicalImageObservation(
    value.canonical_image_observation,
    value.id,
  );
  const binding = normalizeAnalysisBinding(value.analysis_binding);
  const projection = normalizeProjection(value.projection);
  if (
    !sameAnalysisBinding(binding, observation.analysis_binding)
    || !sameProjection(projection, observation.projection)
  ) {
    return invalidAtlasObservation();
  }
  return {
    ...value,
    asset_id: value.id,
    analysis_status: "current",
    analysis_binding: binding,
    projection,
    canonical_image_observation: observation,
  } as unknown as RetrievalApiImage;
}

function inferSlot(image: RetrievalApiImage, index: number): string {
  const searchable = `${image.filename} ${image.description} ${image.tags.join(" ")}`.toLowerCase();
  const matched = SLOT_KEYWORDS.find(({ keywords }) =>
    keywords.some((keyword) => searchable.includes(keyword)),
  );
  if (matched) {
    return matched.slot;
  }

  const fallbackSlots = ["cover", "candid", "detail", "city", "portrait", "quiet", "light", "walk", "still"];
  return fallbackSlots[index % fallbackSlots.length];
}

function toPhotoAsset(
  image: RetrievalApiImage,
  index: number,
  apiBase: string,
  imageLibraryDir: string | null | undefined,
): PhotoAsset {
  const location = normalizeDisplayText(
    [image.place_name, image.country].filter(Boolean).join(" · "),
    "Local library",
  );
  const imageUrl = buildPreviewImageUrl(apiBase, image.relative_path, imageLibraryDir, 1100);
  const title = normalizeDisplayText(
    image.filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "),
    "Photo",
  );
  const description = normalizeDisplayText(image.description, "Local library photo");
  const tags = normalizeTags(image.tags);

  return {
    id: image.id,
    title,
    summary: description,
    location,
    takenAt: image.taken_at?.slice(0, 10) ?? "unknown",
    slot: inferSlot(image, index),
    concepts: tags,
    surfaceTint: SURFACE_TINTS[index % SURFACE_TINTS.length],
    imageUrl,
    score: image.score,
    matchedTerms: image.matched_terms,
    canonicalImageObservation: normalizeAtlasCanonicalImageObservation(
      image.canonical_image_observation,
      image.id,
    ),
  };
}

function toParsedQueryPreview(
  parsedQuery: RetrievalApiResponse["parsed_query"],
): ParsedQueryPreview | null {
  if (!parsedQuery) {
    return null;
  }

  return {
    topK: parsedQuery.top_k,
    dateFrom: parsedQuery.date_from,
    dateTo: parsedQuery.date_to,
    locationText: normalizeDisplayText(parsedQuery.location_text, "") || null,
    descriptiveQuery: normalizeDisplayText(parsedQuery.descriptive_query, "") || null,
    requiredTerms: normalizeTags(parsedQuery.required_terms),
    optionalTerms: normalizeTags(parsedQuery.optional_terms),
    excludedTerms: normalizeTags(parsedQuery.excluded_terms),
  };
}

function fallbackNotes(images: RetrievalApiImage[]): string[] {
  if (images.length === 0) {
    return [];
  }

  const first = images[0];
  const title = normalizeDisplayText(first.filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "), "the lead photo");
  return [
    `The set opens with a stronger lead frame like ${title} to establish the theme quickly.`,
    "The middle introduces detail and space so the sequence does not stay stuck at one viewing distance.",
    "The ending keeps a quieter frame to make the result feel more like a real post-ready set.",
  ];
}

function buildDraftResult(args: {
  payload: RetrievalApiResponse;
  prompt: string;
  variant: ToneVariant;
  apiBase: string;
  imageLibraryDir?: string | null;
}): DraftResult {
  const { payload, prompt, variant, apiBase, imageLibraryDir } = args;
  const analysis = analyzePrompt(prompt.toLowerCase());
  const selected = payload.data.slice(0, 9).map((image, index) =>
    toPhotoAsset(image, index, apiBase, imageLibraryDir),
  );
  const generatedCopy = payload.generated_copy ?? null;
  const resolvedTitle = normalizeDisplayText(payload.title ?? generatedCopy?.title ?? "", "");
  const resolvedCaption = normalizeDisplayText(payload.caption ?? generatedCopy?.body ?? "", "");
  const resolvedNotes = (payload.notes ?? generatedCopy?.highlights ?? [])
    .map((note) => normalizeDisplayText(note, ""))
    .filter(Boolean);

  return {
    id: payload.id,
    prompt,
    title:
      resolvedTitle ||
      (variant === "soft" ? "Make the ordinary feel lighter" : "Recent life, arranged with intent"),
    caption:
      resolvedCaption ||
      "Reordering recent photos into a sequence makes the mood and pacing feel much clearer.",
    candidateCount: payload.candidate_count ?? payload.data.length,
    selectedCount: selected.length,
    selected,
    analysis,
    parsedQuery: toParsedQueryPreview(payload.parsed_query),
    notes: resolvedNotes.length > 0 ? resolvedNotes : fallbackNotes(payload.data),
  };
}

async function fetchGeneratedCopyFromBackend(args: {
  apiBase: string;
  prompt: string;
  imageLibraryDir?: string | null;
  images: RetrievalApiImage[];
  signal?: AbortSignal;
}): Promise<DraftCopyUpdate | null> {
  const response = await fetch(`${args.apiBase}/v1/retrieval/copy`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query_text: args.prompt,
      image_library_dir: args.imageLibraryDir ?? undefined,
      images: args.images.slice(0, 9),
    }),
    signal: args.signal,
  });

  const payload = (await response.json().catch(() => ({}))) as RetrievalCopyApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `retrieval copy failed with status ${response.status}`);
  }

  const generatedCopy = payload.generated_copy ?? null;
  const notes = (payload.notes ?? generatedCopy?.highlights ?? [])
    .map((note) => normalizeDisplayText(note, ""))
    .filter(Boolean);
  const title = normalizeDisplayText(payload.title ?? generatedCopy?.title ?? "", "");
  const caption = normalizeDisplayText(payload.caption ?? generatedCopy?.body ?? "", "");

  if (!title && !caption && notes.length === 0) {
    return null;
  }

  return {
    title: title || null,
    caption: caption || null,
    notes,
  };
}

export async function fetchDraftFromBackend(
  prompt: string,
  variant: ToneVariant,
  options: FetchDraftOptions = {},
): Promise<DraftResult | null> {
  const apiBase = options.apiBase ?? "";
  const requestBody: Record<string, unknown> = {
    text: prompt,
    top_k: 9,
    include_copy: false,
  };
  if (options.imageLibraryDir && options.imageLibraryDir.trim().length > 0) {
    requestBody.image_library_dir = options.imageLibraryDir;
  }
  if (options.dbPath && options.dbPath.trim().length > 0) {
    requestBody.db_path = options.dbPath;
  }
  const response = await fetch(`${apiBase}/v1/retrieval/query`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(requestBody),
    signal: options.signal,
  });

  const payload = (await response.json().catch(() => ({}))) as RetrievalApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `retrieval query failed with status ${response.status}`);
  }

  if (payload.status !== "completed" || !Array.isArray(payload.data) || payload.data.length === 0) {
    return null;
  }
  const normalizedPayload: RetrievalApiResponse = {
    ...payload,
    data: payload.data.map(normalizeAtlasRetrievalImage),
  };

  if (options.onCopyUpdate) {
    void fetchGeneratedCopyFromBackend({
      apiBase,
      prompt,
      imageLibraryDir: options.imageLibraryDir,
      images: normalizedPayload.data,
      signal: options.signal,
    })
      .then((copyUpdate) => {
        if (copyUpdate && (!options.shouldApplyCopyUpdate || options.shouldApplyCopyUpdate())) {
          options.onCopyUpdate?.(copyUpdate);
        }
      })
      .catch(() => {});
  }

  return buildDraftResult({
    payload: normalizedPayload,
    prompt,
    variant,
    apiBase,
    imageLibraryDir: options.imageLibraryDir,
  });
}

export async function fetchAtlasStatus(options: AtlasRequestOptions = {}): Promise<AtlasStatus> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  if (options.dbPath && options.dbPath.trim().length > 0) {
    params.set("db_path", options.dbPath);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/status${suffix}`, {
    signal: options.signal,
  });
  const payload = (await response.json().catch(() => ({}))) as AtlasStatus & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas status failed with status ${response.status}`);
  }
  return payload;
}

export async function fetchScopedIndexStatus(options: AtlasRequestOptions = {}): Promise<ScopedIndexStatusResponse> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  if (options.dbPath && options.dbPath.trim().length > 0) {
    params.set("db_path", options.dbPath);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/index/status${suffix}`, {
    signal: options.signal,
  });
  const payload = (await response.json().catch(() => ({}))) as Partial<ScopedIndexStatusResponse> & {
    message?: string;
  };
  if (!response.ok || !payload.index_stats || typeof payload.db_path !== "string") {
    throw new Error(payload.message ?? `index status failed with status ${response.status}`);
  }
  return payload as ScopedIndexStatusResponse;
}

export async function fetchAiInspirations(
  apiBase: string,
  dbPath?: string | null,
  contextAssetIds: string[] = [],
  signal?: AbortSignal,
): Promise<string[]> {
  const response = await fetch(`${apiBase.replace(/\/$/, "")}/v1/inspiration/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      db_path: dbPath && dbPath.trim().length > 0 ? dbPath : undefined,
      context_asset_ids: contextAssetIds.length > 0 ? contextAssetIds : undefined,
      count: 5,
    }),
    signal,
  });
  const payload = (await response.json().catch(() => ({}))) as InspirationApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `inspiration request failed with status ${response.status}`);
  }
  return Array.isArray(payload.suggestions)
    ? payload.suggestions
        .map((suggestion) => normalizeDisplayText(String(suggestion || "").trim(), ""))
        .filter((suggestion) => suggestion.length > 0)
    : [];
}

export function atlasAssetToPhotoAsset(
  asset: AtlasAsset,
  index: number,
  apiBase: string,
  imageLibraryDir: string | null | undefined,
): PhotoAsset {
  const normalized = normalizeAtlasAsset(asset);
  return toPhotoAsset(
    {
      id: normalized.id,
      asset_id: normalized.asset_id,
      analysis_status: normalized.analysis_status,
      analysis_binding: normalized.analysis_binding,
      projection: normalized.projection,
      canonical_image_observation: normalized.canonical_image_observation,
      filename: normalized.filename,
      relative_path: normalized.relative_path,
      taken_at: normalized.taken_at,
      place_name: normalized.place_name,
      country: normalized.country,
      description: normalized.description,
      tags: normalized.tags,
      score: normalized.quality_score,
      matched_terms: [],
    },
    index,
    apiBase,
    imageLibraryDir,
  );
}

export async function rebuildAtlas(options: AtlasRequestOptions = {}): Promise<AtlasStatus> {
  const apiBase = options.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/rebuild`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildAtlasPayload(options)),
    signal: options.signal,
  });
  const payload = (await response.json().catch(() => ({}))) as AtlasStatus & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas rebuild failed with status ${response.status}`);
  }
  return payload;
}

export async function fetchAtlasOverview(
  options: AtlasRequestOptions = {},
): Promise<AtlasOverview> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  appendAtlasSearchParams(params, options);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/overview${suffix}`);
  const wire = (await response.json().catch(() => ({}))) as AtlasOverview & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas overview failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function fetchAtlasWorkbench(
  options: AtlasRequestOptions = {},
): Promise<AtlasWorkbench> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  appendAtlasSearchParams(params, options);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/workbench${suffix}`, {
    signal: options.signal,
  });
  const wire = (await response.json().catch(() => ({}))) as AtlasWorkbench & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas workbench failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function fetchAtlasBasket(
  options: AtlasRequestOptions = {},
): Promise<AtlasBasket> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  if (options.dbPath && options.dbPath.trim().length > 0) {
    params.set("db_path", options.dbPath);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/basket${suffix}`, {
    signal: options.signal,
  });
  const wire = (await response.json().catch(() => ({}))) as {
    message?: string;
    basket?: AtlasBasket;
  };
  if (!response.ok || !wire.basket) {
    throw new Error(wire.message ?? `atlas basket failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire.basket);
}

export async function fetchAtlasMemoryDetail(
  memoryId: string,
  options: AtlasRequestOptions = {},
): Promise<AtlasMemoryDetail> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  if (options.dbPath && options.dbPath.trim().length > 0) {
    params.set("db_path", options.dbPath);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/memory/${encodeURIComponent(memoryId)}${suffix}`);
  const wire = (await response.json().catch(() => ({}))) as AtlasMemoryDetail & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas memory failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function fetchAtlasCleanup(
  options: AtlasRequestOptions = {},
): Promise<AtlasWorkbench["cleanup"]> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  if (options.dbPath && options.dbPath.trim().length > 0) {
    params.set("db_path", options.dbPath);
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/cleanup${suffix}`);
  const wire = (await response.json().catch(() => ({}))) as AtlasWorkbench["cleanup"] & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas cleanup failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function searchAtlas(options: AtlasRequestOptions = {}): Promise<AtlasOverview> {
  const apiBase = options.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/search`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildAtlasPayload(options)),
  });
  const wire = (await response.json().catch(() => ({}))) as AtlasOverview & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas search failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function selectAtlas(options: AtlasRequestOptions = {}): Promise<AtlasOverview> {
  const apiBase = options.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/select`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(buildAtlasPayload(options)),
  });
  const wire = (await response.json().catch(() => ({}))) as AtlasOverview & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas select failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function fetchAtlasQueryPreview(
  text: string,
  options: AtlasRequestOptions = {},
): Promise<AtlasQueryPreview> {
  const apiBase = options.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/query-preview`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ...buildAtlasPayload(options),
      text,
    }),
  });
  const wire = (await response.json().catch(() => ({}))) as AtlasQueryPreview & { message?: string };
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas query preview failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire);
}

export async function sendAtlasFeedback(input: AtlasRequestOptions & {
  targetKind: "asset" | "cluster";
  targetId: string;
  action: "more_like" | "less_like" | "hide" | "hide_similar" | "never_show_people";
  weight?: number;
  note?: string | null;
}): Promise<void> {
  const apiBase = input.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/feedback`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      db_path: input.dbPath ?? undefined,
      target_kind: input.targetKind,
      target_id: input.targetId,
      action: input.action,
      weight: input.weight ?? 1,
      note: input.note ?? undefined,
    }),
  });
  const payload = (await response.json().catch(() => ({}))) as { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas feedback failed with status ${response.status}`);
  }
}

export async function saveAtlasBasket(input: AtlasRequestOptions & {
  assetIds: string[];
  name?: string | null;
}): Promise<AtlasBasket> {
  const apiBase = input.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/basket`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      db_path: input.dbPath ?? undefined,
      asset_ids: input.assetIds,
      name: input.name ?? undefined,
    }),
    signal: input.signal,
  });
  const wire = (await response.json().catch(() => ({}))) as { message?: string; basket?: AtlasBasket };
  if (!response.ok || !wire.basket) {
    throw new Error(wire.message ?? `atlas basket failed with status ${response.status}`);
  }
  return normalizeAtlasTree(wire.basket);
}

export async function sendAtlasStackAction(input: AtlasRequestOptions & {
  stackId: string;
  action: "keep_best" | "hide_similar" | "unstack";
  keepAssetId?: string | null;
}): Promise<void> {
  const apiBase = input.apiBase ?? "";
  const response = await fetch(`${apiBase}/v1/atlas/stack/action`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      db_path: input.dbPath ?? undefined,
      stack_id: input.stackId,
      action: input.action,
      keep_asset_id: input.keepAssetId ?? undefined,
    }),
  });
  const payload = (await response.json().catch(() => ({}))) as { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas stack action failed with status ${response.status}`);
  }
}

export async function fetchAtlasDraftFromBackend(
  prompt: string,
  variant: ToneVariant,
  options: AtlasRequestOptions = {},
): Promise<DraftResult | null> {
  const apiBase = options.apiBase ?? "";
  const assetObservations = exactAtlasGenerateObservations(options);
  const response = await fetch(`${apiBase}/v1/atlas/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ...buildAtlasPayload(options),
      asset_observations: assetObservations,
      text: prompt,
      top_k: 9,
      include_copy: true,
    }),
    signal: options.signal,
  });
  const wire = (await response.json().catch(() => ({}))) as RetrievalApiResponse;
  if (!response.ok) {
    throw new Error(wire.message ?? `atlas generate failed with status ${response.status}`);
  }
  if (wire.status !== "completed" || !Array.isArray(wire.data) || wire.data.length === 0) {
    return null;
  }
  const payload = normalizeAtlasTree({
    ...wire,
    data: wire.data.map(normalizeAtlasRetrievalImage),
  });
  return buildDraftResult({
    payload,
    prompt,
    variant,
    apiBase,
    imageLibraryDir: options.imageLibraryDir,
  });
}

export async function fetchBackendSettings(
  apiBase: string,
  signal?: AbortSignal,
): Promise<BackendSettingsResponse> {
  const response = await fetch(`${apiBase}/v1/settings`, { signal });
  if (!response.ok) {
    throw new Error(`settings request failed with status ${response.status}`);
  }
  return (await response.json()) as BackendSettingsResponse;
}

export async function saveBackendSettings(
  input: SaveBackendSettingsInput,
): Promise<BackendSettingsResponse> {
  const response = await fetch(`${input.apiBase ?? ""}/v1/settings`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      image_library_dir: input.imageLibraryDir,
      db_path: input.dbPath,
      process_image_width: input.processImageWidth,
      vision_profile_name: input.visionProfileName,
      query_profile_name: input.queryProfileName,
    }),
  });
  const payload = (await response.json().catch(() => ({}))) as BackendSettingsResponse & {
    message?: string;
  };

  if (!response.ok) {
    throw new Error(payload.message ?? `settings update failed with status ${response.status}`);
  }

  return payload;
}

export async function startBackendIndexing(input: {
  apiBase?: string;
  imageLibraryDir: string;
  dbPath?: string | null;
  model?: string | null;
  reindex?: boolean;
}): Promise<DesktopIndexingResult> {
  const response = await fetch(`${input.apiBase ?? ""}/v1/indexing/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      image_dir: input.imageLibraryDir,
      db_path: input.dbPath ?? undefined,
      model: input.model ?? undefined,
      reindex: Boolean(input.reindex),
      persist_to_server: true,
    }),
  });

  const payload = (await response.json().catch(() => ({}))) as IndexingApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `indexing request failed with status ${response.status}`);
  }
  if (!["completed", "partial", "failed", "empty"].includes(payload.status)) {
    throw new Error(payload.message ?? "Indexing returned an unexpected status.");
  }

  return {
    status: payload.status as DesktopIndexingResult["status"],
    folderPath: payload.meta?.image_dir ?? input.imageLibraryDir,
    dbPath: payload.meta?.db_path ?? input.dbPath ?? "",
    total:
      (payload.meta?.indexed_count ?? 0)
      + (payload.meta?.skipped_count ?? 0)
      + (payload.meta?.error_count ?? 0),
    indexed: payload.meta?.indexed_count ?? 0,
    skipped: payload.meta?.skipped_count ?? 0,
    failed: payload.meta?.error_count ?? 0,
    errors: Array.isArray(payload.errors)
      ? payload.errors
          .map((item) => (typeof item?.message === "string" ? item.message : ""))
          .filter((message) => message.trim().length > 0)
      : [],
  };
}
