import { Suspense, lazy, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";

import {
  atlasAssetToPhotoAsset,
  fetchAiInspirations,
  fetchAtlasDraftFromBackend,
  fetchBackendSettings,
  fetchDraftFromBackend,
  saveAtlasBasket,
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
  AtlasAsset,
  AtlasInspirationCard,
  AtlasStoryline,
  LocalModelRuntimeSummary,
  PhotoAsset,
  PipelineStep,
  ToneVariant,
  VlmProfileCatalogEntry,
} from "./query/types";

const AtlasView = lazy(() => import("./AtlasView"));

const PIPELINE_LENGTH = 4;
const GENERATION_STEP_TARGETS = [14, 38, 66, 86];
const LOCAL_BACKEND_URL = "http://127.0.0.1:5519";
const HAN_TEXT_PATTERN = /[\u3400-\u9fff]/u;

type DraftGenerationPhase = "idle" | "running" | "completed";

interface DraftGenerationProgressState {
  phase: DraftGenerationPhase;
  percent: number;
  stepIndex: number;
  title: string;
  detail: string;
}

interface BasketItem {
  id: string;
  title: string;
  subtitle: string;
  imageUrl: string;
}

const IDLE_GENERATION_PROGRESS: DraftGenerationProgressState = {
  phase: "idle",
  percent: 0,
  stepIndex: 0,
  title: "Waiting to start",
  detail: "Enter a prompt and MemoLens will interpret it, search the library, curate the set, and prepare a ready-to-use draft.",
};

function toEnglishUiText(value: string | null | undefined, fallback: string): string {
  let cleaned = String(value ?? "").replace(/\s+/g, " ").trim();
  return cleaned && !HAN_TEXT_PATTERN.test(cleaned) ? cleaned : fallback;
}

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

function findProfileEntry(
  backendSettings: BackendSettingsResponse | null,
  profileName: string | null | undefined,
): VlmProfileCatalogEntry | null {
  if (!backendSettings || !profileName) {
    return null;
  }
  return (
    backendSettings.vlm_profile_catalog.find((entry) => entry.name === profileName) ?? null
  );
}

function formatProfileOptionLabel(entry: VlmProfileCatalogEntry): string {
  const execution = entry.execution === "local" ? "Local" : "API";
  return `${entry.label} · ${execution}`;
}

function formatRecommendedMachine(localRuntime: LocalModelRuntimeSummary | null | undefined): string {
  if (!localRuntime) {
    return "Machine profile unavailable";
  }
  const parts = [
    localRuntime.machine.model_name,
    localRuntime.machine.chip,
    localRuntime.machine.memory_gb ? `${localRuntime.machine.memory_gb} GB` : null,
  ].filter((value): value is string => Boolean(value));
  return parts.join(" · ") || `${localRuntime.machine.platform} · ${localRuntime.machine.architecture}`;
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

function basketItemFromPhotoAsset(photo: PhotoAsset): BasketItem {
  return {
    id: photo.id,
    title: photo.title,
    subtitle: [photo.location, photo.takenAt].filter(Boolean).join(" · "),
    imageUrl: photo.imageUrl,
  };
}

function basketItemFromAtlasAsset(
  asset: AtlasAsset,
  index: number,
  apiBase: string,
  imageLibraryDir: string | null | undefined,
): BasketItem {
  const photo = atlasAssetToPhotoAsset(asset, index, apiBase, imageLibraryDir);
  return basketItemFromPhotoAsset(photo);
}

function App() {
  const desktopRuntime = isDesktopRuntime();
  const electronShell = isElectronShell();
  const [prompt, setPrompt] = useState(INITIAL_PROMPT);
  const [atlasInspirationCards, setAtlasInspirationCards] = useState<AtlasInspirationCard[]>([]);
  const [atlasStorylines, setAtlasStorylines] = useState<AtlasStoryline[]>([]);
  const [atlasSuggestedQueries, setAtlasSuggestedQueries] = useState<string[]>([]);
  const [aiSuggestions, setAiSuggestions] = useState<string[]>([]);
  const [isGeneratingInspirations, setIsGeneratingInspirations] = useState(false);
  const [aiInspirationError, setAiInspirationError] = useState<string | null>(null);
  const [basketItems, setBasketItems] = useState<BasketItem[]>([]);
  const [isBasketOpen, setIsBasketOpen] = useState(false);
  const apiBase = import.meta.env.VITE_BACKEND_BASE_URL ?? LOCAL_BACKEND_URL;
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
    message: "Checking local service",
  });
  const [selectedFolderPath, setSelectedFolderPath] = useState<string | null>(null);
  const [selectedDbPath, setSelectedDbPath] = useState<string | null>(null);
  const [desktopSettings, setDesktopSettings] = useState<DesktopSettings | null>(null);
  const [backendSettings, setBackendSettings] = useState<BackendSettingsResponse | null>(null);
  const [backendStatus, setBackendStatus] = useState<DesktopBackendStatus | null>(null);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [isSavingSettings, setIsSavingSettings] = useState(false);
  const [isSavingBackendSettings, setIsSavingBackendSettings] = useState(false);
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
  const hasUserNavigatedRef = useRef(false);
  const hasInitializedBasketPersistenceRef = useRef(false);
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
  const basketAssetIds = useMemo(() => basketItems.map((item) => item.id), [basketItems]);
  const parsedQueryChips = buildParsedQueryChips(activeResultDraft?.parsedQuery ?? null);
  const indexStats = health.indexStats ?? null;
  const hasStaleIndex = Boolean(indexStats?.needsReindex);
  const localModelRuntime = backendSettings?.local_model_runtime ?? null;
  const recommendedQueryProfile = findProfileEntry(
    backendSettings,
    localModelRuntime?.recommended_query_profile_name,
  );
  const recommendedVisionProfile = findProfileEntry(
    backendSettings,
    localModelRuntime?.recommended_vision_profile_name,
  );
  const selectedVisionProfile = findProfileEntry(
    backendSettings,
    backendSettings?.effective.vision_profile_name,
  );
  const selectedQueryProfile = findProfileEntry(
    backendSettings,
    backendSettings?.effective.query_profile_name,
  );
  const indexStatusLabel = indexStats
    ? indexStats.totalRecords > 0
      ? hasStaleIndex
        ? `Index needs rebuild · ${indexStats.fallbackRecords}/${indexStats.totalRecords} fallback`
        : `Index ready · ${indexStats.totalRecords} photos`
      : "Index empty"
    : "Index pending";

  useEffect(() => {
    if ("scrollRestoration" in window.history) {
      window.history.scrollRestoration = "manual";
    }
    const resetScroll = () => {
      if (hasUserNavigatedRef.current) {
        return;
      }
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    };
    const markUserNavigated = () => {
      hasUserNavigatedRef.current = true;
    };
    window.addEventListener("wheel", markUserNavigated, { passive: true });
    window.addEventListener("touchstart", markUserNavigated, { passive: true });
    window.addEventListener("keydown", markUserNavigated);
    resetScroll();
    window.requestAnimationFrame(resetScroll);
    const shortTimer = window.setTimeout(resetScroll, 120);
    const restoreTimer = window.setTimeout(resetScroll, 520);
    const lateRestoreTimer = window.setTimeout(resetScroll, 1400);
    const finalRestoreTimer = window.setTimeout(resetScroll, 2600);
    return () => {
      window.clearTimeout(shortTimer);
      window.clearTimeout(restoreTimer);
      window.clearTimeout(lateRestoreTimer);
      window.clearTimeout(finalRestoreTimer);
      window.removeEventListener("wheel", markUserNavigated);
      window.removeEventListener("touchstart", markUserNavigated);
      window.removeEventListener("keydown", markUserNavigated);
    };
  }, []);

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

      if (settings.autoStartBackend) {
        setHealth({
          state: "checking",
          message: "Starting local service",
          imageLibraryDir: settings.defaultLibraryDir ?? undefined,
          dbPath: settings.defaultDbPath ?? undefined,
        });
        const status = await ensureDesktopBackend();
        if (!disposed && status !== null) {
          setBackendStatus(status);
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
          message: `Local service online · ${apiBase}`,
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
          if (desktopRuntime) {
            setSelectedFolderPath((current) => current ?? nextBackendSettings.effective.image_library_dir);
            setSelectedDbPath((current) => current ?? nextBackendSettings.effective.db_path);
          } else {
            setSelectedFolderPath(nextBackendSettings.effective.image_library_dir);
            setSelectedDbPath(nextBackendSettings.effective.db_path);
          }
        } catch {
          setBackendSettings(null);
        }
      } catch (error) {
        const reason =
          error instanceof Error && error.message.trim().length > 0
            ? error.message
            : "service unreachable";
        setHealth({
          state: "offline",
          message: `Local service unavailable · ${reason}`,
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

  useEffect(() => {
    if (!hasInitializedBasketPersistenceRef.current) {
      hasInitializedBasketPersistenceRef.current = true;
      return;
    }
    if (health.state !== "connected") {
      return;
    }
    void saveAtlasBasket({
      apiBase,
      dbPath: selectedDbPath ?? health.dbPath ?? null,
      assetIds: basketAssetIds,
      name: "Current selection",
    }).catch(() => {});
  }, [apiBase, basketAssetIds, health.dbPath, health.state, selectedDbPath]);

  function mergeBasketItems(currentItems: BasketItem[], nextItems: BasketItem[]): BasketItem[] {
    const byId = new Map(currentItems.map((item) => [item.id, item]));
    for (const item of nextItems) {
      byId.set(item.id, item);
    }
    return [...byId.values()].slice(0, 240);
  }

  function handleToggleAtlasBasketAsset(asset: AtlasAsset): void {
    const imageLibraryDir = selectedFolderPath ?? health.imageLibraryDir ?? null;
    const nextItem = basketItemFromAtlasAsset(asset, basketItems.length, apiBase, imageLibraryDir);
    setBasketItems((currentItems) => {
      if (currentItems.some((item) => item.id === asset.id)) {
        return currentItems.filter((item) => item.id !== asset.id);
      }
      return mergeBasketItems(currentItems, [nextItem]);
    });
  }

  function handleAddAtlasBasketAssets(assets: AtlasAsset[]): void {
    if (assets.length === 0) {
      return;
    }
    const imageLibraryDir = selectedFolderPath ?? health.imageLibraryDir ?? null;
    setBasketItems((currentItems) =>
      mergeBasketItems(
        currentItems,
        assets.map((asset, index) =>
          basketItemFromAtlasAsset(asset, currentItems.length + index, apiBase, imageLibraryDir),
        ),
      ),
    );
  }

  function handleAddPhotoToBasket(photo: PhotoAsset): void {
    setBasketItems((currentItems) =>
      mergeBasketItems(currentItems, [basketItemFromPhotoAsset(photo)]),
    );
  }

  function handleAddAllResultsToBasket(): void {
    if (!activeResultDraft) {
      return;
    }
    setBasketItems((currentItems) =>
      mergeBasketItems(currentItems, activeResultDraft.selected.map(basketItemFromPhotoAsset)),
    );
  }

  function handleRemoveBasketItem(assetId: string): void {
    setBasketItems((currentItems) => currentItems.filter((item) => item.id !== assetId));
  }

  function handleClearBasket(): void {
    setBasketItems([]);
    setIsBasketOpen(false);
  }

  function scrollToSection(sectionId: string): void {
    hasUserNavigatedRef.current = true;
    document.getElementById(sectionId)?.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function handleUseBasketInCompose(): void {
    scrollToSection("compose");
  }

  function handleRefineFromResult(): void {
    if (!activeResultDraft) {
      return;
    }
    const conceptTerms = Array.from(
      new Set(activeResultDraft.selected.flatMap((photo) => photo.concepts).filter(Boolean)),
    ).slice(0, 5);
    const locations = Array.from(
      new Set(activeResultDraft.selected.map((photo) => photo.location).filter(Boolean)),
    ).slice(0, 2);
    const nextPrompt = [
      "Refine the current result set",
      activeResultDraft.analysis.focus,
      conceptTerms.length > 0 ? `Keep the feeling of ${conceptTerms.join(", ")}` : null,
      locations.length > 0 ? `Use ${locations.join(" / ")} as context` : null,
      "Low similarity, prioritize stronger photos",
    ].filter(Boolean).join(", ");
    setPrompt(nextPrompt);
    handleAddAllResultsToBasket();
    scrollToSection("compose");
  }

  async function runGeneration(variant: ToneVariant): Promise<void> {
    const normalizedPrompt = prompt.trim() || INITIAL_PROMPT;
    const contextAssetIds = [...basketAssetIds];
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

    // If backend is offline, try to restart it before querying.
    let effectiveHealthState = health.state;
    if (effectiveHealthState !== "connected" && desktopRuntime) {
      setGenerationProgress((current) => ({
        ...current,
        title: "Reconnecting to backend",
        detail: "The local service was offline. Attempting to restart it now.",
      }));
      const status = await ensureDesktopBackend();
      if (status !== null) {
        setBackendStatus(status);
        if (status.state === "connected" || status.state === "started") {
          effectiveHealthState = "connected";
          setHealth((currentHealth) => ({
            ...currentHealth,
            state: "connected",
            message: status.message,
          }));
          setHealthRefreshKey((current) => current + 1);
        }
      }
    }

    if (effectiveHealthState === "connected") {
      const makeDraftFetchOptions = () => ({
        apiBase,
        imageLibraryDir: selectedFolderPath ?? health.imageLibraryDir ?? null,
        dbPath: selectedDbPath ?? health.dbPath ?? null,
        contextAssetIds,
        shouldApplyCopyUpdate: () => runIdRef.current === runId,
        onCopyUpdate: (copyUpdate: { title?: string | null; caption?: string | null; notes?: string[] | null }) => {
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
      const fetchCurrentDraft = () =>
        contextAssetIds.length > 0
          ? fetchAtlasDraftFromBackend(normalizedPrompt, variant, {
              apiBase,
              imageLibraryDir: selectedFolderPath ?? health.imageLibraryDir ?? null,
              dbPath: selectedDbPath ?? health.dbPath ?? null,
              assetIds: contextAssetIds,
              showDuplicates: false,
            })
          : fetchDraftFromBackend(normalizedPrompt, variant, makeDraftFetchOptions());

      try {
        nextDraft = await fetchCurrentDraft();
        if (nextDraft === null) {
          setGenerationError("No visible retrieval result came back from the local library. Make sure indexing has finished.");
        }
      } catch (error) {
        const isNetworkError =
          error instanceof TypeError && /fetch/i.test(error.message);

        // If it's a network error, try to restart the backend and retry once.
        if (isNetworkError && desktopRuntime) {
          setGenerationProgress((current) => ({
            ...current,
            title: "Reconnecting to backend",
            detail: "Network error detected. Attempting to restart the local service.",
          }));
          const retryStatus = await ensureDesktopBackend();
          if (retryStatus !== null && (retryStatus.state === "connected" || retryStatus.state === "started")) {
            setBackendStatus(retryStatus);
            setHealth((currentHealth) => ({
              ...currentHealth,
              state: "connected",
              message: retryStatus.message,
            }));
            setHealthRefreshKey((current) => current + 1);

            try {
              nextDraft = await fetchCurrentDraft();
              if (nextDraft === null) {
                setGenerationError("No visible retrieval result came back from the local library. Make sure indexing has finished.");
              }
            } catch (retryError) {
              setGenerationError(
                retryError instanceof Error ? retryError.message : "Draft generation failed after retry.",
              );
              nextDraft = null;
            }
          } else {
            setGenerationError(
              "Failed to fetch: the local backend is offline and could not be restarted. Check Python environment in settings.",
            );
            nextDraft = null;
          }
        } else {
          setGenerationError(
            error instanceof Error ? error.message : "Draft generation failed and no result could be loaded from the local library.",
          );
          nextDraft = null;
        }
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
    if (!desktopRuntime) {
      setHealth({
        state: "checking",
        message: `Checking local service · ${apiBase}`,
        imageLibraryDir: selectedFolderPath ?? health.imageLibraryDir,
        dbPath: selectedDbPath ?? health.dbPath,
      });
      setHealthRefreshKey((current) => current + 1);
      setSettingsMessage("Refreshing local service status.");
      return;
    }

    const status = await ensureDesktopBackend();
    if (status === null) {
      setSettingsMessage("Desktop backend supervision is only available in the Electron app.");
      return;
    }

    setBackendStatus(status);
    setSettingsMessage(status.message);
    setHealthRefreshKey((current) => current + 1);
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
      setSettingsMessage("Local settings are unavailable until the local service is online.");
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
      setSettingsMessage("Local settings saved and reloaded.");
      setHealthRefreshKey((current) => current + 1);
    } catch (error) {
      setSettingsMessage(error instanceof Error ? error.message : "Saving local settings failed.");
    } finally {
      setIsSavingBackendSettings(false);
    }
  }

  function handleUseCurrentLibraryInBackendSettings(): void {
    if (!backendSettings || !selectedFolderPath || !selectedDbPath) {
      setSettingsMessage("Pick a current library first, then copy it into local settings.");
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
    setSettingsMessage("Current library copied into local settings. Save to persist it.");
  }

  function handleApplyRecommendedLocalQuery(): void {
    if (!backendSettings || !localModelRuntime?.recommended_query_profile_name) {
      setSettingsMessage("No recommended local query profile is available on this machine.");
      return;
    }

    setBackendSettings({
      ...backendSettings,
      effective: {
        ...backendSettings.effective,
        query_profile_name: localModelRuntime.recommended_query_profile_name,
      },
    });
    setSettingsMessage(
      `Query profile switched to ${localModelRuntime.recommended_query_profile_name}. Save local settings to apply it.`,
    );
  }

  function handleApplyRecommendedLocalSetup(): void {
    if (
      !backendSettings
      || !localModelRuntime?.recommended_query_profile_name
      || !localModelRuntime.recommended_vision_profile_name
    ) {
      setSettingsMessage("No recommended all-local setup is available on this machine.");
      return;
    }

    setBackendSettings({
      ...backendSettings,
      effective: {
        ...backendSettings.effective,
        query_profile_name: localModelRuntime.recommended_query_profile_name,
        vision_profile_name: localModelRuntime.recommended_vision_profile_name,
      },
    });
    setSettingsMessage(
      "Recommended local query and vision profiles staged. Save local settings to apply them.",
    );
  }

  async function handlePickFolder(): Promise<void> {
    if (!desktopRuntime) {
      if (!backendSettings) {
        setSettingsMessage("Local settings are unavailable until the local service is online.");
        return;
      }

      setSelectedFolderPath(backendSettings.effective.image_library_dir);
      setSelectedDbPath(backendSettings.effective.db_path);
      setHealth((currentHealth) => ({
        ...currentHealth,
        imageLibraryDir: backendSettings.effective.image_library_dir,
        dbPath: backendSettings.effective.db_path,
      }));
      setSettingsMessage("Using the local library path.");
      setIndexingResult(null);
      setIndexingProgress(null);
      setGenerationError(null);
      setHasCompletedGeneration(false);
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
          : "Set the local photo library path in Control, save settings, then start indexing.",
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
            reindex: hasStaleIndex,
          })
        : await startBackendIndexing({
            apiBase,
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
  const showControlGrid = Boolean(desktopSettings) || Boolean(backendSettings);
  const runtimeHeading = desktopRuntime ? "Desktop runtime" : "Local runtime";
  const handleAtlasInspirationChange = useCallback(
    (cards: AtlasInspirationCard[], storylines: AtlasStoryline[], suggestedQueries: string[]) => {
      setAtlasInspirationCards(cards.map((card) => ({
        ...card,
        title: toEnglishUiText(card.title, "Photo idea"),
        summary: toEnglishUiText(card.summary, "A useful photo set from your library."),
        prompt: toEnglishUiText(card.prompt, "Find 9 strong photos with low repetition"),
        top_concepts: card.top_concepts.map((term) => toEnglishUiText(term, "")).filter(Boolean),
      })));
      setAtlasStorylines(storylines.map((storyline) => ({
        ...storyline,
        title: toEnglishUiText(storyline.title, "Storyline"),
        summary: toEnglishUiText(storyline.summary, "A story-ready group from your library."),
        prompt: toEnglishUiText(storyline.prompt, "Pick 9 photos for a natural storyline"),
        top_concepts: storyline.top_concepts.map((term) => toEnglishUiText(term, "")).filter(Boolean),
      })));
      setAtlasSuggestedQueries(
        suggestedQueries
          .map((query) => toEnglishUiText(query, ""))
          .filter((query) => query.length > 0),
      );
    },
    [],
  );
  const visibleAtlasInspiration = atlasInspirationCards.slice(0, 5);
  const visibleStorylinePrompts = atlasStorylines.slice(0, 3);
  const visibleSuggestedQueries = atlasSuggestedQueries.slice(0, 4);

  function handleSectionNav(event: MouseEvent<HTMLAnchorElement>, sectionId: string): void {
    event.preventDefault();
    hasUserNavigatedRef.current = true;
    const target = document.getElementById(sectionId);
    if (!target) {
      return;
    }
    window.history.replaceState(null, "", `#${sectionId}`);
    const scrollToTarget = () => target.scrollIntoView({ block: "start", behavior: "auto" });
    scrollToTarget();
    window.setTimeout(scrollToTarget, 80);
    window.setTimeout(scrollToTarget, 360);
  }

  async function handleGenerateInspirations(): Promise<void> {
    if (health.state !== "connected") {
      setAiInspirationError("Start the local service before asking AI for search ideas.");
      return;
    }
    setIsGeneratingInspirations(true);
    setAiInspirationError(null);
    try {
      const suggestions = await fetchAiInspirations(
        apiBase,
        selectedDbPath ?? health.dbPath ?? null,
        basketAssetIds,
      );
      setAiSuggestions(suggestions);
      if (suggestions.length === 0) {
        setAiInspirationError("No AI suggestions came back. Try after the library map finishes loading.");
      }
    } catch (error) {
      setAiInspirationError(error instanceof Error ? error.message : "AI inspiration failed.");
    } finally {
      setIsGeneratingInspirations(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="top-nav">
        <a
          className="brand"
          href="#hero"
          aria-label="MemoLens home"
          onClick={(event) => handleSectionNav(event, "hero")}
        >
          <span className="brand-mark">M</span>
          <span className="brand-text">
            MemoLens
            <small>Local Photo Agent</small>
          </span>
        </a>

        <nav className="nav-links" aria-label="Primary">
          <a href="#control" onClick={(event) => handleSectionNav(event, "control")}>
            Control
          </a>
          <a href="#library" onClick={(event) => handleSectionNav(event, "library")}>
            Library
          </a>
          <a href="#atlas" onClick={(event) => handleSectionNav(event, "atlas")}>
            Workbench
          </a>
          <a href="#compose" onClick={(event) => handleSectionNav(event, "compose")}>
            Compose
          </a>
          <a href="#process" onClick={(event) => handleSectionNav(event, "process")}>
            Process
          </a>
          <a href="#result" onClick={(event) => handleSectionNav(event, "result")}>
            Result
          </a>
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
            <h2>{runtimeHeading}</h2>
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
              {backendStatus?.startedByApp ? "Desktop managed" : "Local service"}
            </span>
          </div>

          {indexStats ? (
            <p className={hasStaleIndex ? "inline-error" : "inline-note"}>
              {hasStaleIndex
                ? `Current SQLite index looks stale: ${formatPercent(indexStats.fallbackRatio)} of the ${indexStats.totalRecords} records still use filename-only fallback metadata. Rebuild the library once so Vertex can analyze the images again.`
                : `Current SQLite index looks healthy: ${indexStats.totalRecords} records are available for retrieval.`}
            </p>
          ) : null}

          {showControlGrid ? (
            <div className="control-grid">
              {desktopSettings ? (
                <article className="control-card">
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
                    <span>Auto-start the local service when the desktop app opens</span>
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
              ) : null}

              <article className="control-card">
                {backendSettings ? (
                  <>
                    <label className="settings-field">
                      <span>Photo library</span>
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
                      <span>SQLite path</span>
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
                        {backendSettings.vlm_profile_catalog.map((profileEntry) => (
                          <option key={profileEntry.name} value={profileEntry.name}>
                            {formatProfileOptionLabel(profileEntry)}
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
                        {backendSettings.vlm_profile_catalog.map((profileEntry) => (
                          <option key={profileEntry.name} value={profileEntry.name}>
                            {formatProfileOptionLabel(profileEntry)}
                          </option>
                        ))}
                      </select>
                    </label>

                    {selectedVisionProfile || selectedQueryProfile ? (
                      <div className="inline-note">
                        <strong>Current model routing</strong>
                        <br />
                        Vision: {selectedVisionProfile?.label ?? backendSettings.effective.vision_profile_name}
                        {selectedVisionProfile?.summary ? ` — ${selectedVisionProfile.summary}` : ""}
                        <br />
                        Query: {selectedQueryProfile?.label ?? backendSettings.effective.query_profile_name}
                        {selectedQueryProfile?.summary ? ` — ${selectedQueryProfile.summary}` : ""}
                      </div>
                    ) : null}

                    {localModelRuntime ? (
                      <div className="inline-note">
                        <strong>Local model recommendation</strong>
                        <br />
                        {formatRecommendedMachine(localModelRuntime)}
                        <br />
                        {localModelRuntime.summary}
                        <br />
                        {localModelRuntime.recommendation_basis}
                        <br />
                        Ollama: {localModelRuntime.ollama_installed ? "installed" : "not found"}
                        {localModelRuntime.ollama_reachable ? " and running" : " but not reachable on localhost:11434"}
                        {recommendedQueryProfile ? (
                          <>
                            <br />
                            Recommended local query: {recommendedQueryProfile.label}
                          </>
                        ) : null}
                        {recommendedVisionProfile ? (
                          <>
                            <br />
                            Recommended local vision: {recommendedVisionProfile.label}
                          </>
                        ) : null}
                        <br />
                        Hybrid mode is supported: you can keep an API vision profile for indexing and switch only the query profile to local Gemma 4.
                        {localModelRuntime.commands.length > 0 ? (
                          <>
                            <br />
                            Suggested commands: {localModelRuntime.commands.join("  |  ")}
                          </>
                        ) : null}
                      </div>
                    ) : null}

                    <div className="toolbar-row">
                      <button
                        className="secondary-button"
                        onClick={() => handleApplyRecommendedLocalQuery()}
                        disabled={!localModelRuntime?.recommended_query_profile_name}
                      >
                        Use recommended local query
                      </button>
                      <button
                        className="secondary-button"
                        onClick={() => handleApplyRecommendedLocalSetup()}
                        disabled={
                          !localModelRuntime?.recommended_query_profile_name
                          || !localModelRuntime.recommended_vision_profile_name
                        }
                      >
                        Use recommended all-local setup
                      </button>
                    </div>

                    <p className="settings-help">
                      Local app state lives in {backendSettings.effective.app_state_dir}. The
                      persisted settings file is {backendSettings.effective.settings_path}.
                    </p>
                  </>
                ) : (
                  <p className="settings-help">
                    Local settings load after the local service becomes reachable.
                  </p>
                )}
              </article>
            </div>
          ) : (
            <div className="inline-note">
              Local settings will appear as soon as the local service is online.
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
              {isSavingBackendSettings ? "Applying..." : "Save local settings"}
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
              Use current locally
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
            {desktopRuntime ? (
              <button className="primary-button" type="button" onClick={() => void handlePickFolder()}>
                Choose folder
              </button>
            ) : null}
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

        <Suspense
          fallback={
            <section className="section-block atlas-section" id="atlas">
              <p className="eyebrow">Memory Workbench</p>
              <div className="inline-note">Loading library map.</div>
            </section>
          }
        >
          <AtlasView
            apiBase={apiBase}
            imageLibraryDir={selectedFolderPath ?? health.imageLibraryDir ?? null}
            dbPath={selectedDbPath ?? health.dbPath ?? null}
            canUseBackend={health.state === "connected"}
            onInspirationChange={handleAtlasInspirationChange}
            basketAssetIds={basketAssetIds}
            onBasketToggle={handleToggleAtlasBasketAsset}
            onBasketAddMany={handleAddAtlasBasketAssets}
          />
        </Suspense>

        <section id="compose" className="section-block compose-card">
          <div className="section-heading">
            <p className="eyebrow">Compose</p>
            <h2>Describe the set you want.</h2>
          </div>

          <div className="compose-inspiration-panel">
            <div className="compose-inspiration-head">
              <div>
                <p className="eyebrow">AI Inspiration</p>
                <h3>Start from what your library already contains.</h3>
              </div>
              <span className="meta-pill">from Memory Workbench</span>
            </div>

            <div className="compose-inspiration-grid">
              {visibleAtlasInspiration.map((card) => (
                <button
                  key={card.id}
                  className="inspiration-card-button"
                  type="button"
                  onClick={() => setPrompt(card.prompt)}
                >
                  <strong>{card.title}</strong>
                  <span>{card.summary}</span>
                </button>
              ))}
              {visibleStorylinePrompts.map((storyline) => (
                <button
                  key={storyline.id}
                  className="inspiration-card-button"
                  type="button"
                  onClick={() => setPrompt(storyline.prompt)}
                >
                  <strong>{storyline.title}</strong>
                  <span>{storyline.summary}</span>
                </button>
              ))}
            </div>

            {visibleAtlasInspiration.length === 0 && visibleStorylinePrompts.length === 0 ? (
              <p className="inline-note">AI Inspiration will appear after the local library map loads.</p>
            ) : null}

            {visibleSuggestedQueries.length > 0 ? (
              <div className="composer-suggestions compact-suggestions">
                {visibleSuggestedQueries.map((query) => (
                  <button
                    key={query}
                    className="chip-button"
                    type="button"
                    onClick={() => setPrompt(query)}
                  >
                    {query}
                  </button>
                ))}
              </div>
            ) : null}
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

          {basketItems.length > 0 ? (
            <div className="compose-context-panel">
              <div>
                <p className="eyebrow">Visual context</p>
                <strong>{basketItems.length} selected photos will guide the next search</strong>
              </div>
              <div className="compose-context-strip" aria-label="Selected visual context">
                {basketItems.slice(0, 8).map((item) => (
                  <img key={item.id} src={item.imageUrl} alt={item.title} />
                ))}
                {basketItems.length > 8 ? <span>+{basketItems.length - 8}</span> : null}
              </div>
              <button className="secondary-button compact-button" type="button" onClick={handleClearBasket}>
                Clear
              </button>
            </div>
          ) : null}

          <div className="composer-ai-row">
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
            <button
              className="ai-inspire-button"
              type="button"
              onClick={() => void handleGenerateInspirations()}
              disabled={isGeneratingInspirations || health.state !== "connected"}
            >
              {isGeneratingInspirations ? "✨ Thinking..." : "✨ AI Inspire"}
            </button>
          </div>

          {aiSuggestions.length > 0 ? (
            <div className="ai-suggestions-panel">
              <div className="compose-inspiration-head">
                <div>
                  <p className="eyebrow">AI Generated Queries</p>
                  <h3>
                    {basketItems.length > 0
                      ? "Fresh ideas from your selected visual context."
                      : "Fresh ideas from your current library map."}
                  </h3>
                </div>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => void handleGenerateInspirations()}
                  disabled={isGeneratingInspirations || health.state !== "connected"}
                >
                  ✨ Refresh
                </button>
              </div>
              <div className="composer-suggestions compact-suggestions">
                {aiSuggestions.map((suggestion) => (
                  <button
                    key={suggestion}
                    className="chip-button ai-chip"
                    type="button"
                    onClick={() => setPrompt(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {aiInspirationError ? <p className="inline-error">{aiInspirationError}</p> : null}

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
              {basketItems.length > 0 ? (
                <span className="meta-pill">{basketItems.length} reference photos</span>
              ) : null}
              <span className="meta-pill">Cmd/Ctrl + Enter</span>
            </div>
          </div>

          {health.state === "offline" ? (
            <p className="inline-error">
              Start the local service before generating a draft from your local library.
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
                    <button className="secondary-button" type="button" onClick={() => handleAddPhotoToBasket(activePhoto)}>
                      Add active
                    </button>
                    <button className="secondary-button" type="button" onClick={handleAddAllResultsToBasket}>
                      Add all 9
                    </button>
                    <button className="secondary-button" type="button" onClick={handleRefineFromResult}>
                      Refine
                    </button>
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
                  <article
                    key={photo.id}
                    className={`thumbnail-card ${photo.id === activePhoto.id ? "active" : ""}`}
                  >
                    <button
                      type="button"
                      className="thumbnail-select-button"
                      onClick={() => setActivePhotoId(photo.id)}
                    >
                      <span className="thumbnail-art" style={{ backgroundColor: photo.surfaceTint }}>
                        <img src={photo.imageUrl} alt={photo.title} />
                        <small>{String(index + 1).padStart(2, "0")}</small>
                      </span>
                    </button>
                    <span className="thumbnail-copy">
                      <strong>{photo.title}</strong>
                      <em>{photo.slot}</em>
                    </span>
                    <button
                      type="button"
                      className="thumbnail-add-button"
                      onClick={() => handleAddPhotoToBasket(photo)}
                    >
                      Add
                    </button>
                  </article>
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

      {basketItems.length > 0 ? (
        <div className={`floating-basket${isBasketOpen ? " open" : ""}`}>
          <button
            type="button"
            className="floating-basket-pill"
            onClick={() => setIsBasketOpen((current) => !current)}
          >
            <span className="basket-thumb-stack">
              {basketItems.slice(0, 5).map((item) => (
                <img key={item.id} src={item.imageUrl} alt="" />
              ))}
              {basketItems.length > 5 ? <em>+{basketItems.length - 5}</em> : null}
            </span>
            <strong>{basketItems.length} selected</strong>
          </button>
          <div className="floating-basket-actions">
            <button type="button" className="secondary-button compact-button" onClick={handleUseBasketInCompose}>
              Use in Compose
            </button>
            <button
              type="button"
              className="primary-button compact-button"
              onClick={() => void runGeneration(activeVariant)}
              disabled={isGenerating || !canGenerateDraft}
            >
              Generate
            </button>
          </div>
          {isBasketOpen ? (
            <div className="floating-basket-sheet">
              <div className="compose-inspiration-head">
                <div>
                  <p className="eyebrow">Basket</p>
                  <h3>Selected visual context</h3>
                </div>
                <button type="button" className="secondary-button compact-button" onClick={handleClearBasket}>
                  Clear all
                </button>
              </div>
              <div className="basket-sheet-grid">
                {basketItems.map((item) => (
                  <article key={item.id}>
                    <img src={item.imageUrl} alt={item.title} />
                    <div>
                      <strong>{item.title}</strong>
                      <span>{item.subtitle}</span>
                    </div>
                    <button type="button" onClick={() => handleRemoveBasketItem(item.id)}>
                      Remove
                    </button>
                  </article>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default App;
