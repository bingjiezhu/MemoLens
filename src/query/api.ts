import { analyzePrompt } from "./studio";
import type {
  AtlasBasket,
  AtlasAsset,
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
  ToneVariant,
} from "./types";

interface RetrievalApiImage {
  id: string;
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
  selectedMemoryIds?: string[];
  inspirationId?: string | null;
  previewWidth?: number;
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

const HAN_TEXT_PATTERN = /[\u3400-\u9fff]/u;

function toEnglishText(value: string | null | undefined, fallback: string): string {
  let cleaned = String(value ?? "").replace(/\s+/g, " ").trim();
  return cleaned && !HAN_TEXT_PATTERN.test(cleaned) ? cleaned : fallback;
}

function toEnglishTags(tags: string[]): string[] {
  return tags
    .map((tag) => toEnglishText(tag, ""))
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
  const location = toEnglishText(
    [image.place_name, image.country].filter(Boolean).join(" · "),
    "Local library",
  );
  const imageUrl = buildPreviewImageUrl(apiBase, image.relative_path, imageLibraryDir, 1100);
  const title = toEnglishText(
    image.filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "),
    "Photo",
  );
  const description = toEnglishText(image.description, "Local library photo");
  const tags = toEnglishTags(image.tags);

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
    locationText: toEnglishText(parsedQuery.location_text, "") || null,
    descriptiveQuery: toEnglishText(parsedQuery.descriptive_query, "") || null,
    requiredTerms: toEnglishTags(parsedQuery.required_terms),
    optionalTerms: toEnglishTags(parsedQuery.optional_terms),
    excludedTerms: toEnglishTags(parsedQuery.excluded_terms),
  };
}

function fallbackNotes(images: RetrievalApiImage[]): string[] {
  if (images.length === 0) {
    return [];
  }

  const first = images[0];
  const title = toEnglishText(first.filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "), "the lead photo");
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
  const resolvedTitle = toEnglishText(payload.title ?? generatedCopy?.title ?? "", "");
  const resolvedCaption = toEnglishText(payload.caption ?? generatedCopy?.body ?? "", "");
  const resolvedNotes = (payload.notes ?? generatedCopy?.highlights ?? [])
    .map((note) => toEnglishText(note, ""))
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
  });

  const payload = (await response.json().catch(() => ({}))) as RetrievalCopyApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `retrieval copy failed with status ${response.status}`);
  }

  const generatedCopy = payload.generated_copy ?? null;
  const notes = (payload.notes ?? generatedCopy?.highlights ?? [])
    .map((note) => toEnglishText(note, ""))
    .filter(Boolean);
  const title = toEnglishText(payload.title ?? generatedCopy?.title ?? "", "");
  const caption = toEnglishText(payload.caption ?? generatedCopy?.body ?? "", "");

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
  });

  const payload = (await response.json().catch(() => ({}))) as RetrievalApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `retrieval query failed with status ${response.status}`);
  }

  if (payload.status !== "completed" || !Array.isArray(payload.data) || payload.data.length === 0) {
    return null;
  }

  if (options.onCopyUpdate) {
    void fetchGeneratedCopyFromBackend({
      apiBase,
      prompt,
      imageLibraryDir: options.imageLibraryDir,
      images: payload.data,
    })
      .then((copyUpdate) => {
        if (copyUpdate && (!options.shouldApplyCopyUpdate || options.shouldApplyCopyUpdate())) {
          options.onCopyUpdate?.(copyUpdate);
        }
      })
      .catch(() => {});
  }

  return buildDraftResult({
    payload,
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
  const response = await fetch(`${apiBase}/v1/atlas/status${suffix}`);
  const payload = (await response.json().catch(() => ({}))) as AtlasStatus & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas status failed with status ${response.status}`);
  }
  return payload;
}

export async function fetchAiInspirations(
  apiBase: string,
  dbPath?: string | null,
  contextAssetIds: string[] = [],
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
  });
  const payload = (await response.json().catch(() => ({}))) as InspirationApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `inspiration request failed with status ${response.status}`);
  }
  return Array.isArray(payload.suggestions)
    ? payload.suggestions
        .map((suggestion) => toEnglishText(String(suggestion || "").trim(), ""))
        .filter((suggestion) => suggestion.length > 0)
    : [];
}

export function atlasAssetToPhotoAsset(
  asset: AtlasAsset,
  index: number,
  apiBase: string,
  imageLibraryDir: string | null | undefined,
): PhotoAsset {
  return toPhotoAsset(
    {
      id: asset.id,
      filename: asset.filename,
      relative_path: asset.relative_path,
      taken_at: asset.taken_at,
      place_name: asset.place_name,
      country: asset.country,
      description: asset.description,
      tags: asset.tags,
      score: asset.quality_score,
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
  const payload = (await response.json().catch(() => ({}))) as AtlasOverview & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas overview failed with status ${response.status}`);
  }
  return payload;
}

export async function fetchAtlasWorkbench(
  options: AtlasRequestOptions = {},
): Promise<AtlasWorkbench> {
  const apiBase = options.apiBase ?? "";
  const params = new URLSearchParams();
  appendAtlasSearchParams(params, options);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const response = await fetch(`${apiBase}/v1/atlas/workbench${suffix}`);
  const payload = (await response.json().catch(() => ({}))) as AtlasWorkbench & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas workbench failed with status ${response.status}`);
  }
  return payload;
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
  const payload = (await response.json().catch(() => ({}))) as AtlasMemoryDetail & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas memory failed with status ${response.status}`);
  }
  return payload;
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
  const payload = (await response.json().catch(() => ({}))) as AtlasWorkbench["cleanup"] & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas cleanup failed with status ${response.status}`);
  }
  return payload;
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
  const payload = (await response.json().catch(() => ({}))) as AtlasOverview & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas search failed with status ${response.status}`);
  }
  return payload;
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
  const payload = (await response.json().catch(() => ({}))) as AtlasOverview & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas select failed with status ${response.status}`);
  }
  return payload;
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
  const payload = (await response.json().catch(() => ({}))) as AtlasQueryPreview & { message?: string };
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas query preview failed with status ${response.status}`);
  }
  return payload;
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
  });
  const payload = (await response.json().catch(() => ({}))) as { message?: string; basket?: AtlasBasket };
  if (!response.ok || !payload.basket) {
    throw new Error(payload.message ?? `atlas basket failed with status ${response.status}`);
  }
  return payload.basket;
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
  const response = await fetch(`${apiBase}/v1/atlas/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      ...buildAtlasPayload(options),
      text: prompt,
      top_k: 9,
      include_copy: true,
    }),
  });
  const payload = (await response.json().catch(() => ({}))) as RetrievalApiResponse;
  if (!response.ok) {
    throw new Error(payload.message ?? `atlas generate failed with status ${response.status}`);
  }
  if (payload.status !== "completed" || !Array.isArray(payload.data) || payload.data.length === 0) {
    return null;
  }
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
): Promise<BackendSettingsResponse> {
  const response = await fetch(`${apiBase}/v1/settings`);
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
  if (payload.status !== "completed") {
    throw new Error(payload.message ?? "Indexing did not complete successfully.");
  }

  return {
    status: "completed",
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
