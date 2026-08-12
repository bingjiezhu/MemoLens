import type { DraftResult, ToneVariant } from "../query/types";

export const DRAFT_GENERATION_TIMEOUT_MS = 90_000;
export const DRAFT_PIPELINE_LENGTH = 4;
export const NO_VISIBLE_DRAFT_ERROR =
  "No visible retrieval result came back from the local library. Make sure indexing has finished.";
export const GENERATION_UNAVAILABLE_ERROR =
  "Start or reconnect the local service before generating a draft.";

export type DraftGenerationPhase =
  | "idle"
  | "running"
  | "completed"
  | "cancelled"
  | "timed_out";

export type DraftGenerationAbortReason = "cancelled" | "timed_out";

export interface DraftGenerationProgressState {
  phase: DraftGenerationPhase;
  percent: number | null;
  stepIndex: number;
  title: string;
  detail: string;
}

export interface DraftGenerationState {
  activeVariant: ToneVariant;
  isGenerating: boolean;
  error: string | null;
  progress: DraftGenerationProgressState;
}

export interface DraftCopyUpdate {
  title?: string | null;
  caption?: string | null;
  notes?: string[] | null;
}

export type DraftGenerationEvent =
  | { type: "rejected"; message: string }
  | { type: "reset" }
  | { type: "started"; variant: ToneVariant }
  | { type: "reconnecting" }
  | { type: "completed" }
  | { type: "unavailable"; message: string }
  | { type: "aborted"; reason: DraftGenerationAbortReason };

const IDLE_PROGRESS: DraftGenerationProgressState = {
  phase: "idle",
  percent: 0,
  stepIndex: 0,
  title: "Waiting to start",
  detail:
    "Enter a prompt and MemoLens will interpret it, search the library, curate the set, and prepare a ready-to-use draft.",
};

const RUNNING_PROGRESS: DraftGenerationProgressState = {
  phase: "running",
  percent: null,
  stepIndex: 0,
  title: "Searching and curating",
  detail:
    "MemoLens is interpreting the request and retrieving a diverse set from the local library.",
};

const COMPLETED_PROGRESS: DraftGenerationProgressState = {
  phase: "completed",
  percent: 100,
  stepIndex: DRAFT_PIPELINE_LENGTH,
  title: "Draft ready",
  detail: "Your result is ready to review, copy, or refine again.",
};

const NO_RESULT_PROGRESS: DraftGenerationProgressState = {
  phase: "idle",
  percent: 0,
  stepIndex: 0,
  title: "No result available",
  detail: "Check whether local indexing has finished, or review the error message above.",
};

const ABORTED_PRESENTATION: Record<
  DraftGenerationAbortReason,
  { error: string; progress: DraftGenerationProgressState }
> = {
  cancelled: {
    error: "Draft generation cancelled. You can adjust the prompt and retry.",
    progress: {
      phase: "cancelled",
      percent: 0,
      stepIndex: 0,
      title: "Generation cancelled",
      detail: "No result was replaced. Start again whenever you are ready.",
    },
  },
  timed_out: {
    error:
      "Draft generation timed out after 90 seconds. The local service may still be busy; retry when ready.",
    progress: {
      phase: "timed_out",
      percent: 0,
      stepIndex: 0,
      title: "Generation timed out",
      detail: "No result was replaced. Retry when the local service is responsive.",
    },
  },
};

export function createInitialDraftGenerationState(): DraftGenerationState {
  return {
    activeVariant: "balanced",
    isGenerating: false,
    error: null,
    progress: { ...IDLE_PROGRESS },
  };
}

export function reduceDraftGenerationState(
  state: DraftGenerationState,
  event: DraftGenerationEvent,
): DraftGenerationState {
  switch (event.type) {
    case "rejected":
      return { ...state, error: event.message };
    case "reset":
      return {
        ...createInitialDraftGenerationState(),
        activeVariant: state.activeVariant,
      };
    case "started":
      return {
        activeVariant: event.variant,
        isGenerating: true,
        error: null,
        progress: { ...RUNNING_PROGRESS },
      };
    case "reconnecting":
      return {
        ...state,
        progress: {
          ...state.progress,
          title: "Reconnecting to backend",
          detail: "Network error detected. Attempting to restart the local service.",
        },
      };
    case "completed":
      return {
        ...state,
        isGenerating: false,
        error: null,
        progress: { ...COMPLETED_PROGRESS },
      };
    case "unavailable":
      return {
        ...state,
        isGenerating: false,
        error: event.message,
        progress: { ...NO_RESULT_PROGRESS },
      };
    case "aborted": {
      const presentation = ABORTED_PRESENTATION[event.reason];
      return {
        ...state,
        isGenerating: false,
        error: presentation.error,
        progress: { ...presentation.progress },
      };
    }
  }
}

export function buildDraftGenerationRequestKey(
  variant: ToneVariant,
  prompt: string,
  contextAssetIds: readonly string[],
): string {
  return [variant, prompt, ...contextAssetIds].join("\u001f");
}

export function isDraftGenerationNetworkError(error: unknown): boolean {
  return error instanceof TypeError && /fetch/i.test(error.message);
}

export function isDraftGenerationAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function classifyDraftGenerationFailure(
  error: unknown,
  signalAborted: boolean,
  abortReason: DraftGenerationAbortReason | null,
):
  | { kind: "aborted"; reason: DraftGenerationAbortReason }
  | { kind: "network" }
  | { kind: "failed"; message: string } {
  if (signalAborted || isDraftGenerationAbortError(error)) {
    return {
      kind: "aborted",
      reason: abortReason === "timed_out" ? "timed_out" : "cancelled",
    };
  }
  if (isDraftGenerationNetworkError(error)) {
    return { kind: "network" };
  }
  return {
    kind: "failed",
    message:
      error instanceof Error
        ? error.message
        : "Draft generation failed and no result could be loaded from the local library.",
  };
}

export function applyDraftCopyUpdate(
  current: DraftResult,
  update: DraftCopyUpdate,
): DraftResult {
  return {
    ...current,
    title:
      typeof update.title === "string" && update.title.trim().length > 0
        ? update.title
        : current.title,
    caption:
      typeof update.caption === "string" && update.caption.trim().length > 0
        ? update.caption
        : current.caption,
    notes: Array.isArray(update.notes) && update.notes.length > 0 ? update.notes : current.notes,
  };
}

export function getDraftGenerationPhaseLabel(phase: DraftGenerationPhase): string {
  switch (phase) {
    case "completed":
      return "Completed";
    case "running":
      return "Generating";
    case "cancelled":
      return "Cancelled";
    case "timed_out":
      return "Timed out";
    case "idle":
    default:
      return "Idle";
  }
}
