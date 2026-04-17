import { analyzePrompt } from "./studio";
import type {
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
  onCopyUpdate?: (update: DraftCopyUpdate) => void;
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

function encodeRelativePath(relativePath: string): string {
  return relativePath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
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
  const location = [image.place_name, image.country].filter(Boolean).join(" · ") || "Local library";
  const encodedRelativePath = encodeRelativePath(image.relative_path);
  const rootPathQuery =
    imageLibraryDir && imageLibraryDir.trim().length > 0
      ? `?root_path=${encodeURIComponent(imageLibraryDir)}`
      : "";
  const imageUrl = `${apiBase}/v1/library/files/${encodedRelativePath}${rootPathQuery}`;

  return {
    id: image.id,
    title: image.filename.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " "),
    summary: image.description,
    location,
    takenAt: image.taken_at?.slice(0, 10) ?? "unknown",
    slot: inferSlot(image, index),
    concepts: image.tags,
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
    locationText: parsedQuery.location_text,
    descriptiveQuery: parsedQuery.descriptive_query,
    requiredTerms: parsedQuery.required_terms,
    optionalTerms: parsedQuery.optional_terms,
    excludedTerms: parsedQuery.excluded_terms,
  };
}

function fallbackNotes(images: RetrievalApiImage[]): string[] {
  if (images.length === 0) {
    return [];
  }

  const first = images[0];
  return [
    `The set opens with a stronger lead frame like ${first.filename} to establish the theme quickly.`,
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
  const resolvedTitle = payload.title ?? generatedCopy?.title ?? null;
  const resolvedCaption = payload.caption ?? generatedCopy?.body ?? null;
  const resolvedNotes = payload.notes ?? generatedCopy?.highlights ?? null;

  return {
    id: payload.id,
    prompt,
    title:
      resolvedTitle ??
      (variant === "soft" ? "Make the ordinary feel lighter" : "Recent life, arranged with intent"),
    caption:
      resolvedCaption ??
      "Reordering recent photos into a sequence makes the mood and pacing feel much clearer.",
    candidateCount: payload.candidate_count ?? payload.data.length,
    selectedCount: selected.length,
    selected,
    analysis,
    parsedQuery: toParsedQueryPreview(payload.parsed_query),
    notes:
      resolvedNotes && resolvedNotes.length > 0
        ? resolvedNotes
        : fallbackNotes(payload.data),
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
  const notes = payload.notes ?? generatedCopy?.highlights ?? null;
  const title = payload.title ?? generatedCopy?.title ?? null;
  const caption = payload.caption ?? generatedCopy?.body ?? null;

  if (!title && !caption && (!notes || notes.length === 0)) {
    return null;
  }

  return {
    title,
    caption,
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
        if (copyUpdate) {
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

  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as { message?: string };
    throw new Error(payload.message ?? `settings update failed with status ${response.status}`);
  }

  return (await response.json()) as BackendSettingsResponse;
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
