import type {
  AssetReview,
  InboxAsset,
  InboxMediaKind,
  InboxPage,
  InboxState,
  ReviewState,
  ReviewUpdate,
} from "./types";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function nullableText(value: unknown): string | null {
  const normalized = text(value);
  return normalized || null;
}

function nullableNumber(value: unknown): number | null {
  const numeric = Number(value);
  return value !== null && value !== undefined && Number.isFinite(numeric)
    ? numeric
    : null;
}

function reviewState(value: unknown): ReviewState {
  return value === "kept" || value === "archived" ? value : "inbox";
}

export function normalizeAssetReview(value: unknown): AssetReview {
  const raw = record(value);
  return {
    revision: Math.max(0, Math.round(nullableNumber(raw.revision) ?? 0)),
    inbox_state: reviewState(raw.inbox_state),
    favorite: raw.favorite === true || raw.favorite === 1,
    project_ready: raw.project_ready === true || raw.project_ready === 1,
    note: nullableText(raw.note),
    created_at: nullableText(raw.created_at),
  };
}

export function normalizeInboxAsset(value: unknown): InboxAsset | null {
  const raw = record(value);
  const id = text(raw.id);
  const filename = text(raw.filename);
  const kind: InboxMediaKind | null = raw.kind === "image" || raw.kind === "video"
    ? raw.kind
    : null;
  if (!id || !filename || !kind) return null;
  return {
    id,
    kind,
    filename,
    captured_at: nullableText(raw.captured_at),
    width: nullableNumber(raw.width),
    height: nullableNumber(raw.height),
    duration_ms: nullableNumber(raw.duration_ms),
    thumbnail_url: nullableText(raw.thumbnail_url),
    review: normalizeAssetReview(raw.review),
  };
}

export function normalizeInboxPage(value: unknown): InboxPage {
  const raw = record(value);
  const summary = record(raw.summary);
  const entries = Array.isArray(raw.data)
    ? raw.data
    : Array.isArray(raw.items)
      ? raw.items
      : [];
  return {
    items: entries.flatMap((entry) => {
      const asset = normalizeInboxAsset(entry);
      return asset ? [asset] : [];
    }),
    summary: {
      inbox: Math.max(0, nullableNumber(summary.inbox) ?? 0),
      kept: Math.max(0, nullableNumber(summary.kept) ?? 0),
      archived: Math.max(0, nullableNumber(summary.archived) ?? 0),
      all: Math.max(0, nullableNumber(summary.all) ?? 0),
    },
    next_cursor: nullableText(raw.next_cursor),
    has_more: Boolean(raw.has_more),
  };
}

export function applyReviewUpdate(review: AssetReview, update: ReviewUpdate): AssetReview {
  return {
    ...review,
    ...update,
    note: update.note === undefined ? review.note : update.note,
  };
}

export function isAssetVisible(asset: InboxAsset, state: InboxState): boolean {
  return state === "all" || asset.review.inbox_state === state;
}

export function transitionInboxSummary(
  summary: InboxPage["summary"],
  from: ReviewState,
  to: ReviewState,
): InboxPage["summary"] {
  if (from === to) return summary;
  return {
    ...summary,
    [from]: Math.max(0, summary[from] - 1),
    [to]: summary[to] + 1,
  };
}

export function mergeInboxAssets(current: InboxAsset[], incoming: InboxAsset[]): InboxAsset[] {
  const merged = new Map(current.map((asset) => [asset.id, asset]));
  for (const asset of incoming) merged.set(asset.id, asset);
  return [...merged.values()];
}

export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable
    || ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(target.tagName);
}

export function nextAssetId(items: InboxAsset[], currentId: string | null): string | null {
  if (items.length === 0) return null;
  const currentIndex = items.findIndex((asset) => asset.id === currentId);
  return items[(currentIndex + 1 + items.length) % items.length]?.id ?? items[0]?.id ?? null;
}

export function resolveLoadedActiveAssetId(
  currentId: string | null,
  incoming: InboxAsset[],
  append: boolean,
): string | null {
  if (append) return currentId ?? incoming[0]?.id ?? null;
  return currentId && incoming.some((asset) => asset.id === currentId)
    ? currentId
    : incoming[0]?.id ?? null;
}

export function formatAssetDuration(durationMs: number | null): string | null {
  if (durationMs === null) return null;
  const seconds = Math.max(0, Math.round(durationMs / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function formatCapturedDate(value: string | null): string {
  if (!value) return "Date unavailable";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  }).format(parsed);
}

export interface InboxMoment {
  key: string;
  label: string;
  items: InboxAsset[];
}

function capturedDay(value: string | null): { key: string; label: string } {
  const match = value?.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return { key: "unknown", label: "Date unknown" };
  const [, year, month, day] = match;
  const date = new Date(`${year}-${month}-${day}T00:00:00Z`);
  if (Number.isNaN(date.valueOf())) return { key: "unknown", label: "Date unknown" };
  return {
    key: `${year}-${month}-${day}`,
    label: new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
      timeZone: "UTC",
    }).format(date),
  };
}

export function groupInboxMoments(items: InboxAsset[]): InboxMoment[] {
  const groups = new Map<string, InboxMoment>();
  for (const asset of items) {
    const day = capturedDay(asset.captured_at);
    const existing = groups.get(day.key);
    if (existing) existing.items.push(asset);
    else groups.set(day.key, { ...day, items: [asset] });
  }
  return [...groups.values()];
}
