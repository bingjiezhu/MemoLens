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

export type AtlasMode =
  | "semantic"
  | "time"
  | "place"
  | "event"
  | "people"
  | "quality"
  | "duplicates";

export type AtlasLens =
  | "explore"
  | "story"
  | "map"
  | "people"
  | "cleanup"
  | "similar";

export interface AtlasAsset {
  object: "atlas.asset";
  id: string;
  filename: string;
  relative_path: string;
  title: string;
  taken_at: string | null;
  place_name: string | null;
  country: string | null;
  description: string;
  tags: string[];
  combined_text: string;
  embedding_backend: string;
  x: number;
  y: number;
  base_x: number;
  base_y: number;
  cluster_id: string;
  cluster_label: string;
  mode_cluster_id: string;
  mode_cluster_label: string;
  event_id: string;
  duplicate_group_id: string | null;
  neighbor_ids: string[];
  quality_score: number;
  technical_quality_score: number | null;
  people_risk: number;
  lat: number | null;
  lon: number | null;
  layout_version: string;
}

export interface AtlasCluster {
  object: "atlas.cluster";
  id: string;
  mode: AtlasMode;
  label: string;
  count: number;
  representative_asset_id: string;
  top_concepts: string[];
  place_label: string | null;
  time_label: string | null;
  x: number;
  y: number;
  bounds: {
    min_x: number;
    max_x: number;
    min_y: number;
    max_y: number;
  };
}

export interface AtlasEdge {
  object: "atlas.edge";
  source: string;
  target: string;
  kind: string;
  weight: number;
}

export interface AtlasStats {
  quality_avg: number;
  people_risk_count: number;
  duplicate_group_count: number;
  top_concepts: string[];
}

export interface AtlasOverview {
  object: "atlas.overview" | "atlas.search" | "atlas.selection";
  status: string;
  mode: AtlasMode;
  layout_version: string;
  asset_count: number;
  filtered_count: number;
  visible_count: number;
  clusters: AtlasCluster[];
  assets: AtlasAsset[];
  edges: AtlasEdge[];
  stats: AtlasStats;
  index_health?: AtlasStatus;
}

export interface AtlasStatus {
  object: "atlas.status";
  status: string;
  layout_version: string;
  image_count: number;
  asset_count: number;
  cluster_count: number;
  edge_count: number;
  needs_rebuild: boolean;
  last_run: Record<string, unknown> | null;
}

export interface AtlasStack {
  object: "atlas.stack";
  id: string;
  kind: "duplicate" | "similar";
  asset_ids: string[];
  assets: AtlasAsset[];
  representative_asset_id: string | null;
  best_asset_id: string | null;
  best_asset: AtlasAsset | null;
  score: number;
  reason: string;
  count: number;
}

export interface AtlasMemory {
  object: "atlas.memory";
  id: string;
  kind: string;
  label: string;
  asset_count: number;
  asset_ids: string[];
  representative_asset_ids: string[];
  representative_assets: AtlasAsset[];
  top_concepts: string[];
  place_label: string | null;
  time_label: string | null;
  x: number;
  y: number;
  score: number;
  people_risk: number;
  duplicate_count: number;
  chapter_count: number;
  best_assets: AtlasAsset[];
}

export interface AtlasCleanupSummary {
  duplicate_stack_count: number;
  similar_stack_count: number;
  low_quality_count: number;
  missing_time_count: number;
  missing_place_count: number;
  people_review_count: number;
  stacks: AtlasStack[];
  low_quality_assets: AtlasAsset[];
  missing_time_assets: AtlasAsset[];
  missing_place_assets: AtlasAsset[];
  people_review_assets: AtlasAsset[];
}

export interface AtlasBasket {
  object: "atlas.basket";
  id: string | null;
  name: string;
  asset_ids: string[];
  assets: AtlasAsset[];
  created_at: string | null;
  updated_at: string | null;
}

export interface AtlasLensSummary {
  id: AtlasLens;
  label: string;
  count: number;
  summary: string;
}

export interface AtlasLibrarySummary {
  object: "atlas.library_summary";
  asset_count: number;
  memory_count: number;
  top_concepts: string[];
  places: string[];
  time_range: {
    start: string | null;
    end: string | null;
  };
  people_risk_count: number;
  duplicate_stack_count: number;
  quality_avg: number;
  strongest_memory: string | null;
  summary: string;
  index_health?: AtlasStatus;
}

export interface AtlasInspirationCard {
  object: "atlas.inspiration_card";
  id: string;
  kind: string;
  title: string;
  summary: string;
  prompt: string;
  memory_ids: string[];
  asset_ids: string[];
  top_concepts: string[];
  confidence: number;
  source: "local_index" | string;
}

export interface AtlasStoryline {
  object: "atlas.storyline";
  id: string;
  title: string;
  summary: string;
  prompt: string;
  memory_ids: string[];
  asset_count: number;
  chapter_count: number;
  top_concepts: string[];
}

export interface AtlasWorkbench {
  object: "atlas.workbench";
  status: string;
  lens: AtlasLens;
  mode: AtlasMode;
  lenses: AtlasLensSummary[];
  overview: AtlasOverview;
  memories: AtlasMemory[];
  featured_memory: AtlasMemory | null;
  cleanup: AtlasCleanupSummary;
  basket: AtlasBasket;
  basket_ready: AtlasAsset[];
  library_summary: AtlasLibrarySummary;
  inspiration_cards: AtlasInspirationCard[];
  suggested_queries: string[];
  storylines: AtlasStoryline[];
  index_health?: AtlasStatus;
}

export interface AtlasQueryIntent {
  object: "atlas.intent";
  kind: "find_set" | string;
  query_text: string;
  target_count: number;
  required_terms: string[];
  excluded_terms: string[];
  no_people_requested: boolean;
  diversity_requested: boolean;
  output_goal: string;
  style: string | null;
}

export interface AtlasEvidenceAsset {
  asset: AtlasAsset;
  rank: number;
  relevance_score: number;
  quality_score: number;
  diversity_score: number;
  people_risk: number;
  duplicate_penalty: number;
  reasons: string[];
  warnings: string[];
}

export interface AtlasQueryPreview {
  object: "atlas.query_preview";
  status: string;
  intent: AtlasQueryIntent;
  mode: AtlasMode;
  lens: AtlasLens;
  candidate_count: number;
  evidence: AtlasEvidenceAsset[];
  memories: AtlasMemory[];
  warnings: string[];
  suggested_queries: string[];
  index_health?: AtlasStatus;
}

export interface AtlasMemoryDetail {
  object: "atlas.memory";
  status: string;
  memory: AtlasMemory;
  assets: AtlasAsset[];
  chapters: Array<{
    id: string;
    label: string;
    asset_count: number;
    representative_assets: AtlasAsset[];
    time_label: string | null;
    place_label: string | null;
  }>;
  roles: Record<string, Array<{
    image_id: string;
    confidence: number;
    reason: string;
  }>>;
  stacks: AtlasStack[];
  suggestions: string[];
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

export interface VlmProfileCatalogEntry {
  name: string;
  label: string;
  provider: string;
  model: string;
  execution: "local" | "api" | string;
  capabilities: string[];
  family: string | null;
  summary: string | null;
}

export interface LocalMachineInfo {
  platform: string;
  architecture: string;
  model_name: string | null;
  chip: string | null;
  memory_gb: number | null;
}

export interface LocalModelRuntimeSummary {
  machine: LocalMachineInfo;
  ollama_installed: boolean;
  ollama_binary: string | null;
  ollama_reachable: boolean;
  recommended_query_profile_name: string | null;
  recommended_vision_profile_name: string | null;
  summary: string;
  recommendation_basis: string;
  commands: string[];
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
  vlm_profile_catalog: VlmProfileCatalogEntry[];
  local_model_runtime: LocalModelRuntimeSummary;
}
