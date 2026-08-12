import type { DesktopBackendStatus, DraftResult, ToneVariant } from "../query/types";
import {
  DRAFT_GENERATION_TIMEOUT_MS,
  GENERATION_UNAVAILABLE_ERROR,
  NO_VISIBLE_DRAFT_ERROR,
  buildDraftGenerationRequestKey,
  classifyDraftGenerationFailure,
  createInitialDraftGenerationState,
  reduceDraftGenerationState,
} from "./model";
import type {
  DraftCopyUpdate,
  DraftGenerationAbortReason,
  DraftGenerationState,
} from "./model";

export interface DraftFetchRequest {
  prompt: string;
  variant: ToneVariant;
  contextAssetIds: readonly string[];
  signal: AbortSignal;
  onCopyUpdate: (update: DraftCopyUpdate) => void;
  shouldApplyCopyUpdate: () => boolean;
}

export interface DraftGenerationRunInput {
  canGenerate: boolean;
  connectionState: "checking" | "connected" | "mock" | "offline";
  desktopRuntime: boolean;
  prompt: string;
  fallbackPrompt: string;
  variant: ToneVariant;
  contextAssetIds: readonly string[];
  fetchDraft: (request: DraftFetchRequest) => Promise<DraftResult | null>;
  createMockDraft: (prompt: string, variant: ToneVariant, seed: number) => DraftResult;
  restartBackend: () => Promise<DesktopBackendStatus | null>;
  onStarted?: () => void;
  onCopyUpdate?: (update: DraftCopyUpdate) => void;
  onResult?: (draft: DraftResult) => void;
  onBackendRestarted?: (status: DesktopBackendStatus) => void;
}

export interface DraftGenerationClock {
  setTimeout: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>;
  clearTimeout: (timeoutId: ReturnType<typeof setTimeout>) => void;
}

interface ActiveRun {
  id: number;
  requestKey: string;
  controller: AbortController;
  abortReason: DraftGenerationAbortReason | "superseded" | null;
}

export interface DraftGenerationController {
  getState: () => DraftGenerationState;
  subscribe: (listener: (state: DraftGenerationState) => void) => () => void;
  run: (input: DraftGenerationRunInput) => Promise<void>;
  cancel: () => void;
  reset: () => void;
  dispose: () => void;
}

const DEFAULT_CLOCK: DraftGenerationClock = {
  setTimeout: (callback, delayMs) => setTimeout(callback, delayMs),
  clearTimeout: (timeoutId) => clearTimeout(timeoutId),
};

const BACKEND_RESTART_FAILURE =
  "Failed to fetch: the local backend is offline and could not be restarted. Check Python environment in settings.";

export function createDraftGenerationController(
  clock: DraftGenerationClock = DEFAULT_CLOCK,
  timeoutMs = DRAFT_GENERATION_TIMEOUT_MS,
): DraftGenerationController {
  let state = createInitialDraftGenerationState();
  let activeRun: ActiveRun | null = null;
  let latestRunId = 0;
  let seed = 1;
  const listeners = new Set<(state: DraftGenerationState) => void>();

  const publish = (event: Parameters<typeof reduceDraftGenerationState>[1]) => {
    state = reduceDraftGenerationState(state, event);
    for (const listener of listeners) {
      listener(state);
    }
  };

  const isLatest = (run: ActiveRun): boolean =>
    latestRunId === run.id && !run.controller.signal.aborted;

  const publishAbort = (run: ActiveRun): void => {
    if (latestRunId !== run.id) {
      return;
    }
    publish({
      type: "aborted",
      reason: run.abortReason === "timed_out" ? "timed_out" : "cancelled",
    });
  };

  const executeFetch = async (
    run: ActiveRun,
    input: DraftGenerationRunInput,
    prompt: string,
  ): Promise<DraftResult | null> =>
    input.fetchDraft({
      prompt,
      variant: input.variant,
      contextAssetIds: input.contextAssetIds,
      signal: run.controller.signal,
      shouldApplyCopyUpdate: () => isLatest(run),
      onCopyUpdate: (update) => {
        if (isLatest(run)) {
          input.onCopyUpdate?.(update);
        }
      },
    });

  const run = async (input: DraftGenerationRunInput): Promise<void> => {
    if (!input.canGenerate) {
      publish({ type: "rejected", message: GENERATION_UNAVAILABLE_ERROR });
      return;
    }

    const prompt = input.prompt.trim() || input.fallbackPrompt;
    const requestKey = buildDraftGenerationRequestKey(
      input.variant,
      prompt,
      input.contextAssetIds,
    );
    if (
      activeRun
      && !activeRun.controller.signal.aborted
      && activeRun.requestKey === requestKey
    ) {
      return;
    }

    if (activeRun && !activeRun.controller.signal.aborted) {
      activeRun.abortReason = "superseded";
      activeRun.controller.abort();
    }

    const currentRun: ActiveRun = {
      id: latestRunId + 1,
      requestKey,
      controller: new AbortController(),
      abortReason: null,
    };
    latestRunId = currentRun.id;
    activeRun = currentRun;
    seed += 1;
    publish({ type: "started", variant: input.variant });
    input.onStarted?.();

    const timeoutId = clock.setTimeout(() => {
      if (activeRun === currentRun && !currentRun.controller.signal.aborted) {
        currentRun.abortReason = "timed_out";
        currentRun.controller.abort();
      }
    }, timeoutMs);

    let terminalStatePublished = false;
    let nextDraft: DraftResult | null = null;
    let failureMessage: string | null = null;

    try {
      if (input.connectionState === "connected") {
        try {
          nextDraft = await executeFetch(currentRun, input, prompt);
          if (nextDraft === null) {
            failureMessage = NO_VISIBLE_DRAFT_ERROR;
          }
        } catch (error) {
          const failure = classifyDraftGenerationFailure(
            error,
            currentRun.controller.signal.aborted,
            currentRun.abortReason === "superseded" ? null : currentRun.abortReason,
          );
          if (failure.kind === "aborted") {
            if (currentRun.abortReason === "superseded") {
              return;
            }
            publishAbort(currentRun);
            terminalStatePublished = true;
            return;
          }

          if (failure.kind === "network" && input.desktopRuntime) {
            publish({ type: "reconnecting" });
            const status = await input.restartBackend().catch(() => null);
            if (currentRun.controller.signal.aborted || latestRunId !== currentRun.id) {
              return;
            }
            if (status && (status.state === "connected" || status.state === "started")) {
              input.onBackendRestarted?.(status);
              try {
                nextDraft = await executeFetch(currentRun, input, prompt);
                if (nextDraft === null) {
                  failureMessage = NO_VISIBLE_DRAFT_ERROR;
                }
              } catch (retryError) {
                const retryFailure = classifyDraftGenerationFailure(
                  retryError,
                  currentRun.controller.signal.aborted,
                  currentRun.abortReason === "superseded" ? null : currentRun.abortReason,
                );
                if (retryFailure.kind === "aborted") {
                  if (currentRun.abortReason === "superseded") {
                    return;
                  }
                  publishAbort(currentRun);
                  terminalStatePublished = true;
                  return;
                }
                failureMessage =
                  retryFailure.kind === "failed"
                    ? retryFailure.message
                    : retryError instanceof Error
                      ? retryError.message
                      : "Draft generation failed after retry.";
              }
            } else {
              failureMessage = BACKEND_RESTART_FAILURE;
            }
          } else {
            failureMessage =
              failure.kind === "failed"
                ? failure.message
                : error instanceof Error
                  ? error.message
                  : "Draft generation failed and no result could be loaded from the local library.";
          }
        }
      } else if (input.connectionState === "mock") {
        nextDraft = input.createMockDraft(prompt, input.variant, seed);
      }

      if (!isLatest(currentRun)) {
        return;
      }

      if (nextDraft === null && input.connectionState === "mock") {
        nextDraft = input.createMockDraft(prompt, input.variant, seed);
      }

      if (nextDraft === null) {
        publish({
          type: "unavailable",
          message: failureMessage ?? NO_VISIBLE_DRAFT_ERROR,
        });
        terminalStatePublished = true;
        return;
      }

      publish({ type: "completed" });
      terminalStatePublished = true;
      input.onResult?.(nextDraft);
    } finally {
      clock.clearTimeout(timeoutId);
      if (
        currentRun.controller.signal.aborted
        && latestRunId === currentRun.id
        && currentRun.abortReason !== "superseded"
        && !terminalStatePublished
      ) {
        publishAbort(currentRun);
      }
      if (activeRun === currentRun) {
        activeRun = null;
      }
    }
  };

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      listener(state);
      return () => listeners.delete(listener);
    },
    run,
    cancel() {
      if (!activeRun || activeRun.controller.signal.aborted) {
        return;
      }
      activeRun.abortReason = "cancelled";
      activeRun.controller.abort();
    },
    reset() {
      if (activeRun && !activeRun.controller.signal.aborted) {
        activeRun.abortReason = "superseded";
        activeRun.controller.abort();
      }
      publish({ type: "reset" });
    },
    dispose() {
      if (activeRun && !activeRun.controller.signal.aborted) {
        activeRun.abortReason = "cancelled";
        activeRun.controller.abort();
      }
      listeners.clear();
    },
  };
}
