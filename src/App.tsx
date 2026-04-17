import { useDeferredValue, useEffect, useRef, useState } from "react";

import {
  fetchBackendSettings,
  fetchDraftFromBackend,
  saveBackendSettings,
  startBackendIndexing,
} from "./query/api";
import {
  ensureDesktopBackend,
  getDesktopSettings,
  isElectronShell,
  isDesktopRuntime,
  pickLocalImageFolder,
  pauseLocalIndexing,
  resumeLocalIndexing,
  saveDesktopSettings,
  startLocalIndexing,
  subscribeToIndexingProgress,
} from "./query/desktop";
import { INITIAL_PROMPT, PROMPT_PRESETS } from "./query/mockLibrary";
import { analyzePrompt, createDraft, createPipelineSteps } from "./query/studio";
import type {
  BackendHealth,
  BackendSettingsResponse,
  DesktopBackendStatus,
  DesktopIndexingPhase,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopSettings,
  DraftResult,
  PipelineStep,
  ToneVariant,
} from "./query/types";

const PIPELINE_LENGTH = 4;
const GENERATION_STEP_TARGETS = [14, 38, 66, 86];

type DraftGenerationPhase = "idle" | "running" | "completed";

interface DraftGenerationProgressState {
  phase: DraftGenerationPhase;
  percent: number;
  stepIndex: number;
  title: string;
  detail: string;
}

const IDLE_GENERATION_PROGRESS: DraftGenerationProgressState = {
  phase: "idle",
  percent: 0,
  stepIndex: 0,
  title: "Waiting to start",
  detail: "Enter a prompt and MemoLens will interpret it, search the library, curate the set, and prepare a ready-to-use draft.",
};

function sleep(duration: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, duration);
  });
}

function getIndexingPhaseLabel(phase: DesktopIndexingPhase): string {
  switch (phase) {
    case "pausing":
      return "Pausing";
    case "paused":
      return "Paused";
    case "finalizing":
      return "Finalizing";
    case "completed":
      return "Completed";
    case "running":
    default:
      return "Running";
  }
}

function getIndexingPhaseMessage(progress: DesktopIndexingProgress): string | null {
  switch (progress.phase) {
    case "pausing":
      return "The job will pause after the current image finishes.";
    case "paused":
      return "The job is paused and will continue from the next image.";
    case "finalizing":
      return "All images are processed. Writing the final result now.";
    default:
      return null;
  }
}

function getGenerationPhaseLabel(phase: DraftGenerationPhase): string {
  switch (phase) {
    case "completed":
      return "Completed";
    case "running":
      return "Generating";
    case "idle":
    default:
      return "Idle";
  }
}

function hasVisibleText(value: string): boolean {
  return value.trim().length > 0;
}

function formatPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function buildParsedQueryChips(
  parsedQuery: DraftResult["parsedQuery"],
): string[] {
  if (!parsedQuery) {
    return [];
  }

  const chips: string[] = [];
  if (parsedQuery.locationText) {
    chips.push(`location: ${parsedQuery.locationText}`);
  }
  if (parsedQuery.dateFrom || parsedQuery.dateTo) {
    chips.push(
      `time: ${parsedQuery.dateFrom?.slice(0, 10) ?? "any"} → ${parsedQuery.dateTo?.slice(0, 10) ?? "any"}`,
    );
  }
  if (parsedQuery.requiredTerms.length > 0) {
    chips.push(`must: ${parsedQuery.requiredTerms.join(", ")}`);
  }
  if (parsedQuery.excludedTerms.length > 0) {
    chips.push(`exclude: ${parsedQuery.excludedTerms.join(", ")}`);
  }
  return chips;
}

function normalizeDraftForDisplay(
  draft: DraftResult,
  fallbackDraft: DraftResult,
): DraftResult {
  const selected = draft.selected.length > 0 ? draft.selected : fallbackDraft.selected;
  const notes = draft.notes.length > 0 ? draft.notes : fallbackDraft.notes;

  return {
    ...draft,
    candidateCount: draft.candidateCount > 0 ? draft.candidateCount : fallbackDraft.candidateCount,
    title: hasVisibleText(draft.title) ? draft.title : fallbackDraft.title,
    caption: hasVisibleText(draft.caption) ? draft.caption : fallbackDraft.caption,
    selected,
    selectedCount: selected.length,
    notes,
  };
}

function buildExportContent(draft: DraftResult): string {
  const photoLines = draft.selected
    .map(
      (photo, index) =>
        `${index + 1}. ${photo.title} | ${photo.location} | ${photo.takenAt}`,
    )
    .join("\n");

  return [
    `Title: ${draft.title}`,
    "",
    `Caption: ${draft.caption}`,
    "",
    `Prompt: ${draft.prompt}`,
    "",
    "Selected Photos:",
    photoLines,
  ].join("\n");
}

function downloadDraft(draft: DraftResult): void {
  const blob = new Blob([buildExportContent(draft)], {
    type: "text/plain;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `memolens-${draft.id}.txt`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function App() {
  const desktopRuntime = isDesktopRuntime();
  const electronShell = isElectronShell();
  const initialApiBase =
    import.meta.env.VITE_BACKEND_BASE_URL ??
    (electronShell ? "http://127.0.0.1:5519" : "");
  const [prompt, setPrompt] = useState(INITIAL_PROMPT);
  const [apiBase, setApiBase] = useState(initialApiBase);
  const [draft, setDraft] = useState<DraftResult>(() => createDraft(INITIAL_PROMPT));
  const [pipeline, setPipeline] = useState<PipelineStep[]>(() =>
    createPipelineSteps(null, 0),
  );
  const [activeVariant, setActiveVariant] = useState<ToneVariant>("balanced");
  const [isGenerating, setIsGenerating] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [activePhotoId, setActivePhotoId] = useState<string | null>(draft.selected[0]?.id ?? null);
  const [health, setHealth] = useState<BackendHealth>({
    state: "checking",
    message: "Checking local backend",
  });
  const [selectedFolderPath, setSelectedFolderPath] = useState<string | null>(null);
  const [selectedDbPath, setSelectedDbPath] = useState<string | null>(null);
  const [desktopSettings, setDesktopSettings] = useState<DesktopSettings | null>(null);
  const [backendSettings, setBackendSettings] = useState<BackendSettingsResponse | null>(null);
  const [backendStatus, setBackendStatus] = useState<DesktopBackendStatus | null>(null);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [isSavingSettings, setIsSavingSettings] = useState(false);
  const [isSavingBackendSettings, setIsSavingBackendSettings] = useState(false);
  const [isEnsuringBackend, setIsEnsuringBackend] = useState(false);
  const [healthRefreshKey, setHealthRefreshKey] = useState(0);
  const [isIndexing, setIsIndexing] = useState(false);
  const [indexingProgress, setIndexingProgress] = useState<DesktopIndexingProgress | null>(null);
  const [indexingResult, setIndexingResult] = useState<DesktopIndexingResult | null>(null);
  const [indexingError, setIndexingError] = useState<string | null>(null);
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [hasCompletedGeneration, setHasCompletedGeneration] = useState(false);
  const [generationProgress, setGenerationProgress] = useState<DraftGenerationProgressState>(
    IDLE_GENERATION_PROGRESS,
  );
  const [isIndexingControlPending, setIsIndexingControlPending] = useState(false);
  const runIdRef = useRef(0);
  const seedRef = useRef(1);
  const generationProgressTimerRef = useRef<number | null>(null);
  const deferredPrompt = useDeferredValue(prompt);
  const previewAnalysis = analyzePrompt(deferredPrompt || INITIAL_PROMPT);
  const canUseMockMode = health.state === "mock";
  const displayDraft = normalizeDraftForDisplay(
    draft,
    createDraft(prompt.trim() || INITIAL_PROMPT, activeVariant, seedRef.current),
  );
  const activeResultDraft =
    canUseMockMode || hasCompletedGeneration ? displayDraft : null;
  const activePhoto =
    activeResultDraft?.selected.find((photo) => photo.id === activePhotoId) ??
    activeResultDraft?.selected[0] ??
    null;
  const previewPhotos = activeResultDraft?.selected.slice(0, 3) ?? [];
  const libraryFolderLabel = selectedFolderPath ?? health.imageLibraryDir ?? "No folder selected";
  const libraryDbLabel = selectedDbPath ?? health.dbPath ?? "No database yet";
  const runtimeLabel = desktopRuntime ? "Desktop" : electronShell ? "Shell" : "Browser";
  const heroSignals = [previewAnalysis.focus, previewAnalysis.toneLabel, previewAnalysis.timeHint];
  const canGenerateDraft = health.state === "connected" || canUseMockMode;
  const parsedQueryChips = buildParsedQueryChips(activeResultDraft?.parsedQuery ?? null);
  const indexStats = health.indexStats ?? null;
  const hasStaleIndex = Boolean(indexStats?.needsReindex);
  const indexStatusLabel = indexStats
    ? indexStats.totalRecords > 0
      ? hasStaleIndex
        ? `Index needs rebuild · ${indexStats.fallbackRecords}/${indexStats.totalRecords} fallback`
        : `Index ready · ${indexStats.totalRecords} photos`
      : "Index empty"
    : "Index pending";

  function clearGenerationProgressTimer(): void {
    if (generationProgressTimerRef.current !== null) {
      window.clearInterval(generationProgressTimerRef.current);
      generationProgressTimerRef.current = null;
    }
  }

  function startGenerationProgressDrift(runId: number): void {
    clearGenerationProgressTimer();
    generationProgressTimerRef.current = window.setInterval(() => {
      if (runIdRef.current !== runId) {
        clearGenerationProgressTimer();
        return;
      }

      setGenerationProgress((current) => {
        if (current.phase !== "running" || current.percent >= 94) {
          clearGenerationProgressTimer();
          return current;
        }

        const nextPercent = Math.min(current.percent + (current.percent < 90 ? 2 : 1), 94);
        if (nextPercent >= 94) {
          clearGenerationProgressTimer();
        }

        return {
          ...current,
          percent: nextPercent,
          detail: "Refining the candidate set into a cleaner sequence. Final result is almost ready.",
        };
      });
    }, 280);
  }

  useEffect(() => {
    if (!desktopRuntime) {
      return;
    }

    let disposed = false;

    async function initializeDesktop(): Promise<void> {
      const settings = await getDesktopSettings();
      if (disposed || settings === null) {
        return;
      }

      setDesktopSettings(settings);
      setApiBase(settings.backendUrl);
      setSelectedFolderPath(settings.defaultLibraryDir);
      setSelectedDbPath(settings.defaultDbPath);

      if (settings.autoStartBackend) {
        setIsEnsuringBackend(true);
        setHealth({
          state: "checking",
          message: "Starting local backend",
          imageLibraryDir: settings.defaultLibraryDir ?? undefined,
          dbPath: settings.defaultDbPath ?? undefined,
        });
        const status = await ensureDesktopBackend();
        if (!disposed && status !== null) {
          setBackendStatus(status);
          setApiBase(status.url);
        }
        if (!disposed) {
          setIsEnsuringBackend(false);
        }
      }

      if (!disposed) {
        setHealthRefreshKey((current) => current + 1);
      }
    }

    void initializeDesktop();
    return () => {
      disposed = true;
    };
  }, [desktopRuntime]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadHealth(): Promise<void> {
      if (!apiBase) {
        setHealth({
          state: "mock",
          message: "Mock library mode",
        });
        return;
      }

      try {
        const response = await fetch(`${apiBase}/healthz`, {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`unexpected status ${response.status}`);
        }

        const payload = (await response.json()) as {
          image_library_dir?: string;
          db_path?: string;
          vision_profile?: string;
          query_profile?: string;
          embedding_backend?: string;
          index_stats?: {
            total_records?: number;
            fallback_records?: number;
            fallback_ratio?: number;
            needs_reindex?: boolean;
          };
        };

        setHealth({
          state: "connected",
          message: apiBase ? `Backend online · ${apiBase}` : "Local library online",
          imageLibraryDir: payload.image_library_dir,
          dbPath: payload.db_path,
          visionProfile: payload.vision_profile,
          queryProfile: payload.query_profile,
          embeddingBackend: payload.embedding_backend,
          indexStats: payload.index_stats
            ? {
                totalRecords: payload.index_stats.total_records ?? 0,
                fallbackRecords: payload.index_stats.fallback_records ?? 0,
                fallbackRatio: payload.index_stats.fallback_ratio ?? 0,
                needsReindex: Boolean(payload.index_stats.needs_reindex),
              }
            : undefined,
        });
        try {
          const nextBackendSettings = await fetchBackendSettings(apiBase);
          setBackendSettings(nextBackendSettings);
          setSelectedFolderPath((current) => current ?? nextBackendSettings.effective.image_library_dir);
          setSelectedDbPath((current) => current ?? nextBackendSettings.effective.db_path);
        } catch {
          setBackendSettings(null);
        }
      } catch (error) {
        const reason =
          error instanceof Error && error.message.trim().length > 0
            ? error.message
            : "backend unreachable";
        setHealth({
          state: apiBase ? "offline" : "mock",
          message: apiBase ? `Backend unavailable · ${reason}` : "Mock library mode",
          imageLibraryDir: desktopSettings?.defaultLibraryDir ?? undefined,
          dbPath: desktopSettings?.defaultDbPath ?? undefined,
        });
      }
    }

    void loadHealth();
    return () => controller.abort();
  }, [apiBase, desktopSettings?.defaultDbPath, desktopSettings?.defaultLibraryDir, healthRefreshKey]);

  useEffect(() => {
    const unsubscribe = subscribeToIndexingProgress((progress) => {
      setIndexingProgress(progress);
      setSelectedFolderPath(progress.folderPath);
      setSelectedDbPath(progress.dbPath);
      if (progress.phase === "completed") {
        setIsIndexing(false);
      }
    });

    return () => {
      unsubscribe?.();
    };
  }, []);

  useEffect(() => () => clearGenerationProgressTimer(), []);

  useEffect(() => {
    if (!activeResultDraft?.selected.length) {
      setActivePhotoId(null);
      return;
    }
    setActivePhotoId((current) => {
      if (current && activeResultDraft.selected.some((photo) => photo.id === current)) {
        return current;
      }
      return activeResultDraft.selected[0].id;
    });
  }, [activeResultDraft?.id, activeResultDraft?.selected.length]);

  async function runGeneration(variant: ToneVariant): Promise<void> {
    const normalizedPrompt = prompt.trim() || INITIAL_PROMPT;
    const runId = runIdRef.current + 1;
    runIdRef.current = runId;
    clearGenerationProgressTimer();

    setIsGenerating(true);
    setActiveVariant(variant);
    setCopyState("idle");
    setGenerationError(null);

    for (let index = 0; index < PIPELINE_LENGTH; index += 1) {
      const nextPipeline = createPipelineSteps(index, index);
      const activeStep = nextPipeline.find((step) => step.status === "active") ?? nextPipeline[index];
      setPipeline(nextPipeline);
      setGenerationProgress({
        phase: "running",
        percent: GENERATION_STEP_TARGETS[index] ?? 86,
        stepIndex: index + 1,
        title: activeStep.title,
        detail: activeStep.detail,
      });
      await sleep(index === 0 ? 360 : 520);
      if (runIdRef.current !== runId) {
        clearGenerationProgressTimer();
        return;
      }
    }

    seedRef.current += 1;
    startGenerationProgressDrift(runId);
    let nextDraft: DraftResult | null = null;
    if (health.state === "connected") {
      try {
        nextDraft = await fetchDraftFromBackend(normalizedPrompt, variant, {
          apiBase,
          imageLibraryDir: selectedFolderPath ?? health.imageLibraryDir ?? null,
          dbPath: selectedDbPath ?? health.dbPath ?? null,
          onCopyUpdate: (copyUpdate) => {
            if (runIdRef.current !== runId) {
              return;
            }
            setDraft((currentDraft) => ({
              ...currentDraft,
              title:
                typeof copyUpdate.title === "string" && hasVisibleText(copyUpdate.title)
                  ? copyUpdate.title
                  : currentDraft.title,
              caption:
                typeof copyUpdate.caption === "string" && hasVisibleText(copyUpdate.caption)
                  ? copyUpdate.caption
                  : currentDraft.caption,
              notes:
                Array.isArray(copyUpdate.notes) && copyUpdate.notes.length > 0
                  ? copyUpdate.notes
                  : currentDraft.notes,
            }));
          },
        });
        if (nextDraft === null) {
          setGenerationError("No visible retrieval result came back from the local library. Make sure indexing has finished.");
        }
      } catch (error) {
        setGenerationError(
          error instanceof Error ? error.message : "Draft generation failed and no result could be loaded from the local library.",
        );
        nextDraft = null;
      }
    }

    if (runIdRef.current !== runId) {
      clearGenerationProgressTimer();
      return;
    }

    if (nextDraft === null && canUseMockMode) {
      nextDraft = createDraft(normalizedPrompt, variant, seedRef.current);
    }

    if (nextDraft === null) {
      clearGenerationProgressTimer();
      setGenerationProgress({
        phase: "idle",
        percent: 0,
        stepIndex: 0,
        title: "No result available",
        detail: "Check whether local indexing has finished, or review the error message above.",
      });
      setPipeline(createPipelineSteps(null, 0));
      setIsGenerating(false);
      return;
    }

    clearGenerationProgressTimer();
    setGenerationProgress({
      phase: "completed",
      percent: 100,
      stepIndex: PIPELINE_LENGTH,
      title: "Draft ready",
      detail: "Your result is ready to review, copy, or refine again.",
    });
    setHasCompletedGeneration(true);
    setDraft(nextDraft);
    setActivePhotoId(nextDraft.selected[0]?.id ?? null);
    setPipeline(createPipelineSteps(null));
    setIsGenerating(false);
  }

  async function handleCopyCaption(): Promise<void> {
    if (!activeResultDraft) {
      return;
    }
    try {
      await navigator.clipboard.writeText(activeResultDraft.caption);
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1600);
    } catch {
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 1600);
    }
  }

  function appendPreset(query: string): void {
    setPrompt((currentPrompt) => {
      if (!currentPrompt.trim()) {
        return query;
      }
      if (currentPrompt.includes(query)) {
        return currentPrompt;
      }
      const trimmed = currentPrompt.trim().replace(/[。.!?？]+$/, "");
      return `${trimmed}, ${query}`;
    });
  }

  async function handleEnsureBackend(): Promise<void> {
    setSettingsMessage(null);
    setIsEnsuringBackend(true);
    const status = await ensureDesktopBackend();
    if (status === null) {
      setSettingsMessage("Desktop backend supervision is only available in the Electron app.");
      setIsEnsuringBackend(false);
      return;
    }

    setBackendStatus(status);
    setApiBase(status.url);
    setSettingsMessage(status.message);
    setHealthRefreshKey((current) => current + 1);
    setIsEnsuringBackend(false);
  }

  async function handleSaveSettings(): Promise<void> {
    if (desktopSettings === null) {
      setSettingsMessage("Desktop settings are only available in the Electron app.");
      return;
    }

    setSettingsMessage(null);
    setIsSavingSettings(true);
    const savedSettings = await saveDesktopSettings(desktopSettings);
    if (savedSettings === null) {
      setSettingsMessage("Desktop settings are only available in the Electron app.");
      setIsSavingSettings(false);
      return;
    }

    setDesktopSettings(savedSettings);
    setApiBase(savedSettings.backendUrl);
    setSelectedFolderPath(savedSettings.defaultLibraryDir);
    setSelectedDbPath(savedSettings.defaultDbPath);
    setSettingsMessage("Desktop settings saved.");
    setHealthRefreshKey((current) => current + 1);

    if (savedSettings.autoStartBackend) {
      await handleEnsureBackend();
    }

    setIsSavingSettings(false);
  }

  async function handleChooseDefaultFolder(): Promise<void> {
    if (!desktopRuntime) {
      setSettingsMessage("Choose a default folder from the Electron desktop app.");
      return;
    }

    const selection = await pickLocalImageFolder();
    if (!selection) {
      return;
    }

    setDesktopSettings((current) =>
      current
        ? {
            ...current,
            defaultLibraryDir: selection.folderPath,
            defaultDbPath: selection.dbPath,
          }
        : null,
    );
    setSettingsMessage("Default library updated. Save settings to persist it.");
  }

  function handleUseCurrentLibraryInSettings(): void {
    if (desktopSettings === null || !selectedFolderPath) {
      setSettingsMessage("Pick or index a library first, then copy it into the desktop settings.");
      return;
    }

    setDesktopSettings({
      ...desktopSettings,
      defaultLibraryDir: selectedFolderPath,
      defaultDbPath: selectedDbPath ?? desktopSettings.defaultDbPath,
    });
    setSettingsMessage("Current library copied into the desktop settings. Save to persist it.");
  }

  async function handleSaveBackendSettings(): Promise<void> {
    if (!backendSettings) {
      setSettingsMessage("Backend settings are unavailable until the local backend is online.");
      return;
    }

    setSettingsMessage(null);
    setIsSavingBackendSettings(true);
    try {
      const saved = await saveBackendSettings({
        apiBase,
        imageLibraryDir: backendSettings.effective.image_library_dir,
        dbPath: backendSettings.effective.db_path,
        processImageWidth: backendSettings.effective.process_image_width,
        visionProfileName: backendSettings.effective.vision_profile_name,
        queryProfileName: backendSettings.effective.query_profile_name,
      });
      setBackendSettings(saved);
      setSelectedFolderPath(saved.effective.image_library_dir);
      setSelectedDbPath(saved.effective.db_path);
      setHealth((current) => ({
        ...current,
        imageLibraryDir: saved.effective.image_library_dir,
        dbPath: saved.effective.db_path,
        visionProfile: saved.effective.vision_profile_name,
        queryProfile: saved.effective.query_profile_name,
      }));
      setSettingsMessage("Backend settings saved and reloaded.");
      setHealthRefreshKey((current) => current + 1);
    } catch (error) {
      setSettingsMessage(error instanceof Error ? error.message : "Saving backend settings failed.");
    } finally {
      setIsSavingBackendSettings(false);
    }
  }

  function handleUseCurrentLibraryInBackendSettings(): void {
    if (!backendSettings || !selectedFolderPath || !selectedDbPath) {
      setSettingsMessage("Pick a current library first, then copy it into backend settings.");
      return;
    }

    setBackendSettings({
      ...backendSettings,
      effective: {
        ...backendSettings.effective,
        image_library_dir: selectedFolderPath,
        db_path: selectedDbPath,
      },
      persisted: {
        ...backendSettings.persisted,
        image_library_dir: selectedFolderPath,
        db_path: selectedDbPath,
      },
    });
    setSettingsMessage("Current library copied into backend settings. Save to persist it.");
  }

  async function handlePickFolder(): Promise<void> {
    if (!desktopRuntime) {
      setSettingsMessage("Electron is unavailable here. Set the backend photo library path in Control and save it.");
      return;
    }

    setIndexingError(null);
    setSettingsMessage(null);
    const selection = await pickLocalImageFolder();
    if (!selection) {
      return;
    }
    setSelectedFolderPath(selection.folderPath);
    setSelectedDbPath(selection.dbPath);
    setHealth((currentHealth) => ({
      ...currentHealth,
      imageLibraryDir: selection.folderPath,
      dbPath: selection.dbPath,
    }));
    setIndexingResult(null);
    setIndexingProgress(null);
    setGenerationError(null);
    setHasCompletedGeneration(false);
  }

  async function handleStartIndexing(): Promise<void> {
    if (!selectedFolderPath) {
      setIndexingError(
        desktopRuntime
          ? "Pick a local image folder first."
          : "Set the backend photo library path in Control, save settings, then start indexing.",
      );
      return;
    }

    setIsIndexing(true);
    setIsIndexingControlPending(false);
    setIndexingError(null);
    setIndexingProgress(null);
    setIndexingResult(null);
    setGenerationError(null);
    setHasCompletedGeneration(false);

    try {
      const result = desktopRuntime
        ? await startLocalIndexing({
            folderPath: selectedFolderPath,
            dbPath: selectedDbPath ?? undefined,
            apiBase: apiBase || "http://127.0.0.1:5519",
            reindex: hasStaleIndex,
          })
        : await startBackendIndexing({
            apiBase: apiBase || "http://127.0.0.1:5519",
            imageLibraryDir: selectedFolderPath,
            dbPath: selectedDbPath ?? undefined,
            reindex: hasStaleIndex,
          });
      if (desktopRuntime && result === null) {
        setIndexingError("This browser mode cannot write local SQLite. Please run the Electron app.");
        setIsIndexing(false);
        return;
      }
      const resolvedResult = result;
      if (resolvedResult === null) {
        setIndexingError("Indexing could not start.");
        setIsIndexing(false);
        return;
      }
      setIndexingResult(resolvedResult);
      setSelectedFolderPath(resolvedResult.folderPath);
      setSelectedDbPath(resolvedResult.dbPath);
      setHealth((currentHealth) => ({
        ...currentHealth,
        imageLibraryDir: resolvedResult.folderPath,
        dbPath: resolvedResult.dbPath,
      }));
      setHealthRefreshKey((current) => current + 1);
      if (!desktopRuntime) {
        setIsIndexing(false);
      }
    } catch (error) {
      setIndexingError(error instanceof Error ? error.message : "Local indexing failed.");
      setIsIndexing(false);
    }
  }

  async function handlePauseIndexing(): Promise<void> {
    setIndexingError(null);
    setIsIndexingControlPending(true);
    try {
      const paused = await pauseLocalIndexing();
      if (paused === null) {
        setIndexingError("This browser mode cannot pause local indexing. Please run the Electron app.");
      }
    } catch (error) {
      setIndexingError(error instanceof Error ? error.message : "Pausing indexing failed.");
    } finally {
      setIsIndexingControlPending(false);
    }
  }

  async function handleResumeIndexing(): Promise<void> {
    setIndexingError(null);
    setIsIndexingControlPending(true);
    try {
      const resumed = await resumeLocalIndexing();
      if (resumed === null) {
        setIndexingError("This browser mode cannot resume local indexing. Please run the Electron app.");
      }
    } catch (error) {
      setIndexingError(error instanceof Error ? error.message : "Resuming indexing failed.");
    } finally {
      setIsIndexingControlPending(false);
    }
  }

  const indexingPhase = indexingProgress?.phase ?? null;
  const canPauseIndexing = indexingPhase === "running";
  const canResumeIndexing = indexingPhase === "paused" || indexingPhase === "pausing";
  const canControlIndexing = canPauseIndexing || canResumeIndexing;
  const indexingPhaseMessage = indexingProgress ? getIndexingPhaseMessage(indexingProgress) : null;
  const canStartIndexing =
    Boolean(selectedFolderPath) && !isIndexing && (desktopRuntime || health.state === "connected");
  const indexingActionLabel = hasStaleIndex ? "Rebuild index" : "Start indexing";

  return (
    <div className="app-shell">
      <header className="top-nav">
        <a className="brand" href="#hero" aria-label="MemoLens home">
          <span className="brand-mark">M</span>
          <span className="brand-text">
            MemoLens
            <small>Local Photo Agent</small>
          </span>
        </a>

        <nav className="nav-links" aria-label="Primary">
          <a href="#control">Control</a>
          <a href="#library">Library</a>
          <a href="#compose">Compose</a>
          <a href="#process">Process</a>
          <a href="#result">Result</a>
        </nav>

        <div className="nav-status">
          <span className={`status-pill status-${health.state}`}>{health.message}</span>
          <span className="status-pill">{runtimeLabel}</span>
        </div>
      </header>

      <main className="page-shell">
        <section className="hero-section" id="hero">
          <div className="hero-copy">
            <p className="eyebrow">Local Photo Agent</p>
            <h1>
              Ask your photo library
              <span> to find, filter, and shape a set.</span>
            </h1>
            <div className="hero-chip-row">
              {heroSignals.map((signal) => (
                <span key={signal} className="status-pill">
                  {signal}
                </span>
              ))}
            </div>
          </div>

          <aside className="hero-preview-card">
            <div className="hero-preview-header">
              <div>
                <p className="eyebrow">Current draft</p>
                <h2>{activeResultDraft?.title ?? "Waiting for the first draft"}</h2>
              </div>
              <span className="status-pill">
                {activeResultDraft ? `${activeResultDraft.selectedCount} selected` : "0 selected"}
              </span>
            </div>

            {previewPhotos.length > 0 ? (
              <div className="hero-preview-stack">
                {previewPhotos.map((photo, index) => (
                  <div
                    key={photo.id}
                    className="mini-frame"
                    style={{
                      rotate: `${(index - 1) * 4}deg`,
                      translate: `${index * 14}px ${index * 8}px`,
                      zIndex: previewPhotos.length - index,
                      backgroundColor: photo.surfaceTint,
                    }}
                  >
                    <img src={photo.imageUrl} alt={photo.title} />
                    <span>{photo.title}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-card hero-empty-card">
                <strong>Pick a library, then generate a draft.</strong>
                <span>Your local assistant preview will appear here.</span>
              </div>
            )}
          </aside>
        </section>

        <section className="section-block control-section" id="control">
          <div className="section-heading compact-heading">
            <p className="eyebrow">Control</p>
            <h2>Desktop runtime</h2>
          </div>

          <div className="meta-pills">
            <span className={`status-pill status-${health.state}`}>{health.message}</span>
            <span className="meta-pill">
              Vision {health.visionProfile ?? "pending"}
            </span>
            <span className="meta-pill">
              Query {health.queryProfile ?? "pending"}
            </span>
            <span className="meta-pill">
              Embeddings {health.embeddingBackend ?? "pending"}
            </span>
            <span className={`meta-pill${hasStaleIndex ? " status-offline" : ""}`}>
              {indexStatusLabel}
            </span>
            <span className="meta-pill">
              {backendStatus?.startedByApp ? "Desktop managed" : "External or pending"}
            </span>
          </div>

          {indexStats ? (
            <p className={hasStaleIndex ? "inline-error" : "inline-note"}>
              {hasStaleIndex
                ? `Current SQLite index looks stale: ${formatPercent(indexStats.fallbackRatio)} of the ${indexStats.totalRecords} records still use filename-only fallback metadata. Rebuild the library once so Vertex can analyze the images again.`
                : `Current SQLite index looks healthy: ${indexStats.totalRecords} records are available for retrieval.`}
            </p>
          ) : null}

          {desktopSettings ? (
            <div className="control-grid">
              <article className="control-card">
                <label className="settings-field">
                  <span>Backend URL</span>
                  <input
                    className="settings-input"
                    type="text"
                    value={desktopSettings.backendUrl}
                    onChange={(event) =>
                      setDesktopSettings({
                        ...desktopSettings,
                        backendUrl: event.target.value,
                      })
                    }
                  />
                </label>

                <label className="settings-field">
                  <span>Python command</span>
                  <input
                    className="settings-input"
                    type="text"
                    value={desktopSettings.pythonCommand}
                    onChange={(event) =>
                      setDesktopSettings({
                        ...desktopSettings,
                        pythonCommand: event.target.value,
                      })
                    }
                  />
                </label>

                <label className="toggle-field">
                  <input
                    type="checkbox"
                    checked={desktopSettings.autoStartBackend}
                    onChange={(event) =>
                      setDesktopSettings({
                        ...desktopSettings,
                        autoStartBackend: event.target.checked,
                      })
                    }
                  />
                  <span>Auto-start the local backend when the desktop app opens</span>
                </label>

                <label className="settings-field">
                  <span>Desktop default library</span>
                  <input
                    className="settings-input"
                    type="text"
                    value={desktopSettings.defaultLibraryDir ?? ""}
                    onChange={(event) =>
                      setDesktopSettings({
                        ...desktopSettings,
                        defaultLibraryDir: event.target.value,
                      })
                    }
                  />
                </label>

                <label className="settings-field">
                  <span>Desktop default SQLite</span>
                  <input
                    className="settings-input"
                    type="text"
                    value={desktopSettings.defaultDbPath ?? ""}
                    onChange={(event) =>
                      setDesktopSettings({
                        ...desktopSettings,
                        defaultDbPath: event.target.value,
                      })
                    }
                  />
                </label>
              </article>

              <article className="control-card">
                {backendSettings ? (
                  <>
                    <label className="settings-field">
                      <span>Backend photo library</span>
                      <input
                        className="settings-input"
                        type="text"
                        value={backendSettings.effective.image_library_dir}
                        onChange={(event) =>
                          setBackendSettings({
                            ...backendSettings,
                            effective: {
                              ...backendSettings.effective,
                              image_library_dir: event.target.value,
                            },
                          })
                        }
                      />
                    </label>

                    <label className="settings-field">
                      <span>Backend SQLite path</span>
                      <input
                        className="settings-input"
                        type="text"
                        value={backendSettings.effective.db_path}
                        onChange={(event) =>
                          setBackendSettings({
                            ...backendSettings,
                            effective: {
                              ...backendSettings.effective,
                              db_path: event.target.value,
                            },
                          })
                        }
                      />
                    </label>

                    <label className="settings-field">
                      <span>Process image width</span>
                      <input
                        className="settings-input"
                        type="number"
                        min={128}
                        step={32}
                        value={backendSettings.effective.process_image_width}
                        onChange={(event) =>
                          setBackendSettings({
                            ...backendSettings,
                            effective: {
                              ...backendSettings.effective,
                              process_image_width: Number(event.target.value) || 512,
                            },
                          })
                        }
                      />
                    </label>

                    <label className="settings-field">
                      <span>Vision profile</span>
                      <select
                        className="settings-input"
                        value={backendSettings.effective.vision_profile_name}
                        onChange={(event) =>
                          setBackendSettings({
                            ...backendSettings,
                            effective: {
                              ...backendSettings.effective,
                              vision_profile_name: event.target.value,
                            },
                          })
                        }
                      >
                        {backendSettings.available_vlm_profiles.map((profileName) => (
                          <option key={profileName} value={profileName}>
                            {profileName}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label className="settings-field">
                      <span>Query profile</span>
                      <select
                        className="settings-input"
                        value={backendSettings.effective.query_profile_name}
                        onChange={(event) =>
                          setBackendSettings({
                            ...backendSettings,
                            effective: {
                              ...backendSettings.effective,
                              query_profile_name: event.target.value,
                            },
                          })
                        }
                      >
                        {backendSettings.available_vlm_profiles.map((profileName) => (
                          <option key={profileName} value={profileName}>
                            {profileName}
                          </option>
                        ))}
                      </select>
                    </label>

                    <p className="settings-help">
                      Backend app state lives in {backendSettings.effective.app_state_dir}. The
                      persisted settings file is {backendSettings.effective.settings_path}.
                    </p>
                  </>
                ) : (
                  <p className="settings-help">
                    Backend settings load after the local backend becomes reachable.
                  </p>
                )}
              </article>
            </div>
          ) : (
            <div className="inline-note">
              Run the Electron desktop shell to get persisted settings and backend supervision.
            </div>
          )}

          <div className="toolbar-row">
            <button
              className="primary-button"
              type="button"
              onClick={() => void handleSaveSettings()}
              disabled={!desktopSettings || isSavingSettings}
            >
              {isSavingSettings ? "Saving..." : "Save settings"}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void handleSaveBackendSettings()}
              disabled={!backendSettings || isSavingBackendSettings}
            >
              {isSavingBackendSettings ? "Applying..." : "Save backend settings"}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void handleEnsureBackend()}
              disabled={!desktopRuntime || isEnsuringBackend}
            >
              {isEnsuringBackend ? "Connecting..." : "Reconnect backend"}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void handleChooseDefaultFolder()}
              disabled={!desktopRuntime || !desktopSettings}
            >
              Choose default folder
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => handleUseCurrentLibraryInSettings()}
              disabled={!desktopSettings || !selectedFolderPath}
            >
              Use current in desktop
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => handleUseCurrentLibraryInBackendSettings()}
              disabled={!backendSettings || !selectedFolderPath || !selectedDbPath}
            >
              Use current in backend
            </button>
          </div>

          {settingsMessage ? <p className="inline-note">{settingsMessage}</p> : null}
        </section>

        <section className="section-block library-section" id="library">
          <div className="section-heading compact-heading">
            <p className="eyebrow">Library</p>
            <h2>Local library</h2>
          </div>

          <div className="toolbar-row">
            <button className="primary-button" type="button" onClick={() => void handlePickFolder()}>
              {desktopRuntime ? "Choose folder" : "Use backend path"}
            </button>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void handleStartIndexing()}
              disabled={!canStartIndexing}
            >
              {isIndexing ? `${indexingActionLabel}...` : indexingActionLabel}
            </button>
            {isIndexing && indexingProgress && canControlIndexing ? (
              <button
                className="secondary-button"
                type="button"
                onClick={() =>
                  void (canResumeIndexing ? handleResumeIndexing() : handlePauseIndexing())
                }
                disabled={isIndexingControlPending}
              >
                {canResumeIndexing ? "Resume" : "Pause"}
              </button>
            ) : null}
          </div>

          <div className="meta-pills">
            <span className="meta-pill path-pill" title={libraryFolderLabel}>
              {libraryFolderLabel}
            </span>
            <span className="meta-pill path-pill" title={libraryDbLabel}>
              {libraryDbLabel}
            </span>
          </div>

          {indexingProgress ? (
            <section className="progress-card">
              <div className="progress-head">
                <div>
                  <p className="eyebrow">Indexing</p>
                  <h3>{indexingProgress.completed} / {indexingProgress.total}</h3>
                </div>
                <div className="meta-pills">
                  <span className="status-pill">{getIndexingPhaseLabel(indexingProgress.phase)}</span>
                  <span className="status-pill">{indexingProgress.percent}%</span>
                </div>
              </div>
              <div className="progress-bar">
                <div
                  className="progress-bar-fill"
                  style={{ width: `${indexingProgress.percent}%` }}
                />
              </div>
              <div className="progress-meta-row">
                <span>indexed {indexingProgress.indexed}</span>
                <span>skipped {indexingProgress.skipped}</span>
                <span>failed {indexingProgress.failed}</span>
              </div>
              <p className="progress-caption">
                {indexingProgress.currentFile ?? "Preparing"}
              </p>
              {indexingPhaseMessage ? <p className="progress-caption">{indexingPhaseMessage}</p> : null}
            </section>
          ) : null}

          {indexingResult ? (
            <div className="inline-note">
              Indexed {indexingResult.total} images into the active SQLite library.
            </div>
          ) : null}

          {indexingError ? <p className="inline-error">{indexingError}</p> : null}
        </section>

        <section id="compose" className="section-block compose-card">
          <div className="section-heading">
            <p className="eyebrow">Compose</p>
            <h2>Describe the set you want.</h2>
          </div>

          <div className="composer-suggestions">
            {PROMPT_PRESETS.map((preset) => (
              <button
                key={preset.label}
                className="chip-button"
                type="button"
                onClick={() => appendPreset(preset.query)}
              >
                {preset.label}
              </button>
            ))}
          </div>

          <label className="composer-field">
            <span className="sr-only">Prompt input</span>
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
                  void runGeneration(activeVariant);
                }
              }}
              placeholder="For example: pick a gentle, post-ready set from my recent library."
            />
          </label>

          <div className="composer-footer">
            <div className="action-row">
              <button
                className="primary-button"
                type="button"
                onClick={() => void runGeneration("balanced")}
                disabled={isGenerating || !canGenerateDraft}
              >
                {isGenerating && activeVariant === "balanced" ? "Generating..." : "Generate draft"}
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => void runGeneration("soft")}
                disabled={isGenerating || !canGenerateDraft}
              >
                {isGenerating && activeVariant === "soft" ? "Refining..." : "Make it softer"}
              </button>
            </div>

            <div className="meta-pills">
              <span className="meta-pill">
                {activeResultDraft
                  ? `${activeResultDraft.candidateCount} → ${activeResultDraft.selectedCount}`
                  : "Waiting for a real result"}
              </span>
              <span className="meta-pill">Cmd/Ctrl + Enter</span>
            </div>
          </div>

          {health.state === "offline" ? (
            <p className="inline-error">
              Reconnect the backend in Control before generating a draft from your local library.
            </p>
          ) : null}
          {generationError ? <p className="inline-error">{generationError}</p> : null}

          <div className="signal-row">
            {previewAnalysis.tokens.slice(0, 4).map((token) => (
              <span className="status-pill" key={token}>
                {token}
              </span>
            ))}
          </div>
        </section>

        <section id="process" className="section-block process-panel">
          <div className="section-heading compact-heading">
            <p className="eyebrow">Process</p>
            <h2>Visible progress</h2>
          </div>

          <section className="progress-card live-progress-card">
            <div className="progress-head">
              <div>
                <p className="eyebrow">Live progress</p>
                <h3>{generationProgress.title}</h3>
              </div>
              <div className="meta-pills">
                <span className="status-pill">{getGenerationPhaseLabel(generationProgress.phase)}</span>
                <span className="status-pill">{generationProgress.percent}%</span>
              </div>
            </div>
            <div className="progress-bar">
              <div
                className="progress-bar-fill"
                style={{ width: `${generationProgress.percent}%` }}
              />
            </div>
            <p className="progress-caption">
              {generationProgress.stepIndex > 0
                ? `Step ${generationProgress.stepIndex} / ${PIPELINE_LENGTH}`
                : "Waiting to start"}
            </p>
          </section>

          {activeResultDraft?.parsedQuery ? (
            <section className="progress-card">
              <div className="progress-head">
                <div>
                  <p className="eyebrow">Structured query</p>
                  <h3>{activeResultDraft.parsedQuery.descriptiveQuery ?? "Planner output"}</h3>
                </div>
                <div className="meta-pills">
                  <span className="status-pill">
                    top {activeResultDraft.parsedQuery.topK}
                  </span>
                </div>
              </div>
              <div className="meta-pills">
                {parsedQueryChips.map((chip) => (
                  <span key={chip} className="meta-pill">
                    {chip}
                  </span>
                ))}
              </div>
            </section>
          ) : null}

          <div className="process-grid">
            {pipeline.map((step) => {
              const state =
                step.status === "done" ? "complete" : step.status === "active" ? "active" : "idle";
              return (
                <article className={`process-card state-${state}`} key={step.id}>
                  <span className="process-index">{String(step.index).padStart(2, "0")}</span>
                  <h3>{step.title}</h3>
                  <p>{step.detail}</p>
                </article>
              );
            })}
          </div>
        </section>

        <section id="result" className="section-block curated-stage">
          <div className="section-heading">
            <p className="eyebrow">Result</p>
            <h2>{activeResultDraft?.title ?? "Results appear here after generation"}</h2>
          </div>

          {activeResultDraft && activePhoto ? (
            <>
              <div className="gallery-stage">
                <article className="lead-stage">
                  <div className="photo-stage" style={{ backgroundColor: activePhoto.surfaceTint }}>
                    <img src={activePhoto.imageUrl} alt={activePhoto.title} />
                    <div className="photo-overlay">
                      <span className="photo-badge">{activePhoto.slot}</span>
                      <div className="photo-copy">
                        <h3>{activePhoto.title}</h3>
                        <p>{activePhoto.location}</p>
                        <small>{activePhoto.takenAt}</small>
                      </div>
                    </div>
                  </div>
                </article>

                <aside className="curation-read">
                  <div className="highlight-row">
                    <span className="highlight-chip">{activeResultDraft.analysis.toneLabel}</span>
                    <span className="highlight-chip">{activeResultDraft.analysis.focus}</span>
                    <span className="highlight-chip">{activeResultDraft.analysis.timeHint}</span>
                  </div>

                  <p className="story-body">{activeResultDraft.caption}</p>

                  <div className="action-row">
                    <button className="secondary-button" type="button" onClick={() => void handleCopyCaption()}>
                      {copyState === "copied"
                        ? "Copied"
                        : copyState === "failed"
                          ? "Copy failed"
                          : "Copy caption"}
                    </button>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => void runGeneration("soft")}
                      disabled={isGenerating || !canGenerateDraft}
                    >
                      Make it softer
                    </button>
                    <button
                      className="secondary-button"
                      type="button"
                      onClick={() => downloadDraft(activeResultDraft)}
                    >
                      Export
                    </button>
                  </div>
                </aside>
              </div>

              <div className="thumbnail-grid">
                {activeResultDraft.selected.map((photo, index) => (
                  <button
                    key={photo.id}
                    type="button"
                    className={`thumbnail-card ${photo.id === activePhoto.id ? "active" : ""}`}
                    onClick={() => setActivePhotoId(photo.id)}
                  >
                    <span className="thumbnail-art" style={{ backgroundColor: photo.surfaceTint }}>
                      <img src={photo.imageUrl} alt={photo.title} />
                      <small>{String(index + 1).padStart(2, "0")}</small>
                    </span>
                    <span className="thumbnail-copy">
                      <strong>{photo.title}</strong>
                      <em>{photo.slot}</em>
                    </span>
                  </button>
                ))}
              </div>
            </>
          ) : (
            <div className="empty-card">
              <strong>Generate one draft first</strong>
              <span>Once indexing is done, real local photos will appear here.</span>
            </div>
          )}
        </section>
      </main>

      {(isGenerating || isIndexing) && (
        <div className="floating-state">
          {isIndexing
            ? "indexing local library..."
            : `${generationProgress.title} · ${generationProgress.percent}%`}
        </div>
      )}
    </div>
  );
}

export default App;
