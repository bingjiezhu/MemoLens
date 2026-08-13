export type InboxState = "all" | "inbox" | "kept" | "archived";
export type ReviewState = Exclude<InboxState, "all">;
export type InboxMediaKind = "image" | "video";

export interface AssetReview {
  revision: number;
  inbox_state: ReviewState;
  favorite: boolean;
  project_ready: boolean;
  note: string | null;
  created_at: string | null;
}

export interface InboxAsset {
  id: string;
  kind: InboxMediaKind;
  filename: string;
  captured_at: string | null;
  width: number | null;
  height: number | null;
  duration_ms: number | null;
  thumbnail_url: string | null;
  review: AssetReview;
}

export interface InboxPage {
  items: InboxAsset[];
  summary: Record<ReviewState | "all", number>;
  next_cursor: string | null;
  has_more: boolean;
}

export interface ReviewUpdate {
  inbox_state?: ReviewState;
  favorite?: boolean;
  project_ready?: boolean;
  note?: string | null;
}
