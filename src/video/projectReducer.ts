import type {
  CreativeAssetMatch,
  CreativeProject,
  CreativeTimeline,
  MediaJob,
  RenderJob,
  TimelineDiff,
  TimelineValidation,
  VideoCapabilityStatus,
  VideoSegmentDetail,
} from "./types";

export type AsyncPhase = "idle" | "loading" | "ready" | "empty" | "error";

export interface VideoWorkbenchState {
  scopeKey: string;
  capabilitiesPhase: AsyncPhase;
  capabilities: VideoCapabilityStatus | null;
  capabilitiesError: string | null;
  indexJob: MediaJob | null;
  indexJobs: MediaJob[];
  indexError: string | null;
  searchPhase: AsyncPhase;
  searchResults: CreativeAssetMatch[];
  searchError: string | null;
  selectedMatchId: string | null;
  segmentPhase: AsyncPhase;
  segment: VideoSegmentDetail | null;
  segmentError: string | null;
  projectPhase: AsyncPhase;
  project: CreativeProject | null;
  projectError: string | null;
  timelinePhase: AsyncPhase;
  timeline: CreativeTimeline | null;
  timelineError: string | null;
  timelineDiff: TimelineDiff[];
  validationPhase: AsyncPhase;
  validation: TimelineValidation | null;
  validationError: string | null;
  renderJob: RenderJob | null;
  renderError: string | null;
  saveMessage: string | null;
}

export type VideoWorkbenchAction =
  | { type: "reset_scope"; scopeKey: string }
  | { type: "capabilities_loading" }
  | { type: "capabilities_ready"; capabilities: VideoCapabilityStatus }
  | { type: "capabilities_error"; error: string }
  | { type: "index_job"; job: MediaJob }
  | { type: "index_error"; error: string }
  | { type: "search_loading" }
  | { type: "search_ready"; results: CreativeAssetMatch[] }
  | { type: "search_error"; error: string }
  | { type: "select_match"; matchId: string | null }
  | { type: "segment_loading" }
  | { type: "segment_ready"; segment: VideoSegmentDetail }
  | { type: "segment_error"; error: string }
  | { type: "project_loading" }
  | { type: "project_ready"; project: CreativeProject }
  | { type: "project_error"; error: string }
  | { type: "timeline_loading" }
  | { type: "timeline_ready"; timeline: CreativeTimeline; diff?: TimelineDiff[] }
  | { type: "timeline_error"; error: string }
  | { type: "validation_loading" }
  | { type: "validation_ready"; validation: TimelineValidation }
  | { type: "validation_error"; error: string }
  | { type: "render_job"; job: RenderJob }
  | { type: "render_error"; error: string }
  | { type: "save_message"; message: string | null };

export function initialVideoWorkbenchState(scopeKey: string): VideoWorkbenchState {
  return {
    scopeKey,
    capabilitiesPhase: "idle",
    capabilities: null,
    capabilitiesError: null,
    indexJob: null,
    indexJobs: [],
    indexError: null,
    searchPhase: "idle",
    searchResults: [],
    searchError: null,
    selectedMatchId: null,
    segmentPhase: "idle",
    segment: null,
    segmentError: null,
    projectPhase: "idle",
    project: null,
    projectError: null,
    timelinePhase: "idle",
    timeline: null,
    timelineError: null,
    timelineDiff: [],
    validationPhase: "idle",
    validation: null,
    validationError: null,
    renderJob: null,
    renderError: null,
    saveMessage: null,
  };
}

export function videoWorkbenchReducer(
  state: VideoWorkbenchState,
  action: VideoWorkbenchAction,
): VideoWorkbenchState {
  switch (action.type) {
    case "reset_scope":
      return initialVideoWorkbenchState(action.scopeKey);
    case "capabilities_loading":
      return { ...state, capabilitiesPhase: "loading", capabilitiesError: null };
    case "capabilities_ready":
      return { ...state, capabilitiesPhase: "ready", capabilities: action.capabilities, capabilitiesError: null };
    case "capabilities_error":
      return { ...state, capabilitiesPhase: "error", capabilities: null, capabilitiesError: action.error };
    case "index_job":
      return {
        ...state,
        indexJob: action.job,
        indexJobs: [
          ...state.indexJobs.filter((job) => job.id !== action.job.id),
          action.job,
        ],
        indexError: null,
      };
    case "index_error":
      return { ...state, indexError: action.error };
    case "search_loading":
      return { ...state, searchPhase: "loading", searchError: null };
    case "search_ready":
      return {
        ...state,
        searchPhase: action.results.length > 0 ? "ready" : "empty",
        searchResults: action.results,
        searchError: null,
        selectedMatchId: action.results.some((item) => item.id === state.selectedMatchId)
          ? state.selectedMatchId
          : action.results[0]?.id ?? null,
      };
    case "search_error":
      return { ...state, searchPhase: "error", searchResults: [], searchError: action.error };
    case "select_match":
      return { ...state, selectedMatchId: action.matchId, segment: null, segmentError: null, segmentPhase: "idle" };
    case "segment_loading":
      return { ...state, segmentPhase: "loading", segmentError: null };
    case "segment_ready":
      return { ...state, segmentPhase: "ready", segment: action.segment, segmentError: null };
    case "segment_error":
      return { ...state, segmentPhase: "error", segment: null, segmentError: action.error };
    case "project_loading":
      return { ...state, projectPhase: "loading", projectError: null };
    case "project_ready":
      return {
        ...state,
        projectPhase: "ready",
        project: action.project,
        projectError: null,
        ...(state.project?.id === action.project.id
          ? {}
          : {
              timelinePhase: "idle" as AsyncPhase,
              timeline: null,
              timelineError: null,
              timelineDiff: [],
              validationPhase: "idle" as AsyncPhase,
              validation: null,
              validationError: null,
              renderJob: null,
              renderError: null,
              saveMessage: null,
            }),
      };
    case "project_error":
      return { ...state, projectPhase: "error", projectError: action.error };
    case "timeline_loading":
      return { ...state, timelinePhase: "loading", timelineError: null };
    case "timeline_ready":
      return {
        ...state,
        timelinePhase: "ready",
        timeline: action.timeline,
        timelineError: null,
        timelineDiff: action.diff ?? [],
        validation: null,
        validationPhase: "idle",
        validationError: null,
        renderJob: null,
        renderError: null,
        saveMessage: null,
      };
    case "timeline_error":
      return { ...state, timelinePhase: "error", timelineError: action.error };
    case "validation_loading":
      return { ...state, validationPhase: "loading", validationError: null };
    case "validation_ready":
      return { ...state, validationPhase: "ready", validation: action.validation, validationError: null };
    case "validation_error":
      return { ...state, validationPhase: "error", validationError: action.error };
    case "render_job":
      return { ...state, renderJob: action.job, renderError: null };
    case "render_error":
      return { ...state, renderError: action.error };
    case "save_message":
      return { ...state, saveMessage: action.message };
    default:
      return state;
  }
}
