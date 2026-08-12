import { useCallback, useEffect, useRef, useState } from "react";

import { ensureDesktopBackend } from "../query/desktop";
import { fetchAtlasDraftFromBackend, fetchDraftFromBackend } from "../query/api";
import { INITIAL_PROMPT } from "../query/mockLibrary";
import { createDraft } from "../query/studio";
import type { BackendHealth, DesktopBackendStatus, DraftResult, ToneVariant } from "../query/types";
import { createDraftGenerationController } from "./controller";
import type { DraftGenerationRunInput } from "./controller";
import { createInitialDraftGenerationState } from "./model";
import type { DraftCopyUpdate } from "./model";

export interface UseDraftGenerationOptions {
  apiBase: string;
  prompt: string;
  contextAssetIds: readonly string[];
  health: BackendHealth;
  selectedImageLibraryDir: string | null;
  selectedDbPath: string | null;
  desktopRuntime: boolean;
  onStarted?: () => void;
  onCopyUpdate?: (update: DraftCopyUpdate) => void;
  onResult?: (draft: DraftResult) => void;
  onBackendRestarted?: (status: DesktopBackendStatus) => void;
}

export function useDraftGeneration(options: UseDraftGenerationOptions) {
  const [state, setState] = useState(createInitialDraftGenerationState);
  const controllerRef = useRef<ReturnType<typeof createDraftGenerationController> | null>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  if (controllerRef.current === null) {
    controllerRef.current = createDraftGenerationController();
  }

  useEffect(() => {
    const controller = controllerRef.current;
    if (controller === null) {
      return;
    }
    const unsubscribe = controller.subscribe(setState);
    return () => {
      unsubscribe();
      controller.dispose();
    };
  }, []);

  const run = useCallback((variant: ToneVariant): Promise<void> => {
    const current = optionsRef.current;
    const imageLibraryDir = current.selectedImageLibraryDir ?? current.health.imageLibraryDir ?? null;
    const dbPath = current.selectedDbPath ?? current.health.dbPath ?? null;
    const contextAssetIds = [...current.contextAssetIds];

    const input: DraftGenerationRunInput = {
      canGenerate: current.health.state === "connected" || current.health.state === "mock",
      connectionState: current.health.state,
      desktopRuntime: current.desktopRuntime,
      prompt: current.prompt,
      fallbackPrompt: INITIAL_PROMPT,
      variant,
      contextAssetIds,
      fetchDraft: (request) =>
        contextAssetIds.length > 0
          ? fetchAtlasDraftFromBackend(request.prompt, request.variant, {
              apiBase: current.apiBase,
              imageLibraryDir,
              dbPath,
              assetIds: contextAssetIds,
              showDuplicates: false,
              signal: request.signal,
            })
          : fetchDraftFromBackend(request.prompt, request.variant, {
              apiBase: current.apiBase,
              imageLibraryDir,
              dbPath,
              contextAssetIds,
              signal: request.signal,
              shouldApplyCopyUpdate: request.shouldApplyCopyUpdate,
              onCopyUpdate: request.onCopyUpdate,
            }),
      createMockDraft: createDraft,
      restartBackend: ensureDesktopBackend,
      onStarted: current.onStarted,
      onCopyUpdate: current.onCopyUpdate,
      onResult: current.onResult,
      onBackendRestarted: current.onBackendRestarted,
    };
    return controllerRef.current?.run(input) ?? Promise.resolve();
  }, []);

  const cancel = useCallback(() => {
    controllerRef.current?.cancel();
  }, []);

  const reset = useCallback(() => {
    controllerRef.current?.reset();
  }, []);

  return {
    ...state,
    run,
    cancel,
    reset,
  };
}
