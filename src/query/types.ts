export type ToneVariant = "balanced" | "soft";
export type PipelineStatus = "pending" | "active" | "done";
export type DesktopIndexingPhase =
  | "running"
  | "pausing"
  | "paused"
  | "finalizing"
  | "completed";

export interface PromptPreset {
  label: string;
  query: string;
}

export interface PhotoAsset {
  id: string;
  title: string;
  summary: string;
  location: string;
  takenAt: string;
  slot: string;
  concepts: string[];
  surfaceTint: string;
  imageUrl: string;
  score?: number;
  matchedTerms?: string[];
}

export interface ParsedQueryPreview {
  topK: number;
  dateFrom: string | null;
  dateTo: string | null;
  locationText: string | null;
  descriptiveQuery: string | null;
  requiredTerms: string[];
  optionalTerms: string[];
  excludedTerms: string[];
}

export interface PipelineStep {
  id: string;
  index: number;
  title: string;
  detail: string;
  metric: string;
  status: PipelineStatus;
}

export interface DraftAnalysis {
  focus: string;
  toneLabel: string;
  timeHint: string;
  useCase: string;
  locationLabel: string;
  tokens: string[];
}

export interface DraftResult {
  id: string;
  prompt: string;
  title: string;
  caption: string;
  candidateCount: number;
  selectedCount: number;
  selected: PhotoAsset[];
  analysis: DraftAnalysis;
  notes: string[];
  parsedQuery?: ParsedQueryPreview | null;
}

export interface BackendHealth {
  state: "checking" | "connected" | "mock" | "offline";
  message: string;
  imageLibraryDir?: string;
  dbPath?: string;
  visionProfile?: string;
  queryProfile?: string;
  embeddingBackend?: string;
  indexStats?: {
    totalRecords: number;
    fallbackRecords: number;
    fallbackRatio: number;
    needsReindex: boolean;
  };
}

export interface DesktopFolderSelection {
  folderPath: string;
  dbPath: string;
}

export interface DesktopIndexingStartOptions {
  folderPath: string;
  dbPath?: string;
  apiBase?: string;
  model?: string | null;
  reindex?: boolean;
}

export interface DesktopIndexingProgress {
  phase: DesktopIndexingPhase;
  total: number;
  completed: number;
  indexed: number;
  skipped: number;
  failed: number;
  currentFile: string | null;
  folderPath: string;
  dbPath: string;
  percent: number;
}

export interface DesktopIndexingResult {
  status: "completed";
  folderPath: string;
  dbPath: string;
  total: number;
  indexed: number;
  skipped: number;
  failed: number;
  errors: string[];
}

export interface DesktopSettings {
  backendUrl: string;
  pythonCommand: string;
  autoStartBackend: boolean;
  defaultLibraryDir: string | null;
  defaultDbPath: string | null;
}

export interface DesktopBackendStatus {
  state: "connected" | "started" | "unavailable";
  message: string;
  url: string;
  startedByApp: boolean;
}

export interface BackendSettingsEffective {
  image_library_dir: string;
  db_path: string;
  app_state_dir: string;
  settings_path: string;
  process_image_width: number;
  vision_profile_name: string;
  query_profile_name: string;
  embedding_backend: string;
}

export interface BackendSettingsResponse {
  object: string;
  effective: BackendSettingsEffective;
  persisted: Partial<
    Pick<
      BackendSettingsEffective,
      "image_library_dir" | "db_path" | "process_image_width" | "vision_profile_name" | "query_profile_name"
    >
  >;
  available_vlm_profiles: string[];
}
