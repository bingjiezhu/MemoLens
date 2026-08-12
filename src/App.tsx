import { Suspense, lazy, useCallback, useDeferredValue, useEffect, useRef, useState } from "react";
import type { MouseEvent } from "react";

import { usePersistedAtlasBasket } from "./basket/usePersistedAtlasBasket";
import {
  DRAFT_PIPELINE_LENGTH,
  applyDraftCopyUpdate,
  getDraftGenerationPhaseLabel,
} from "./generation/model";
import { useDraftGeneration } from "./generation/useDraftGeneration";
import {
  fetchAiInspirations,
  fetchScopedIndexStatus,
  fetchBackendSettings,
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
  AtlasInspirationCard,
  AtlasStoryline,
  ScopedIndexStatusResponse,
  LocalModelRuntimeSummary,
  VlmProfileCatalogEntry,
} from "./query/types";

const AtlasView = lazy(() => import("./AtlasView"));
const VideoWorkbench = lazy(() => import("./VideoWorkbench"));

const LOCAL_BACKEND_URL = "http://127.0.0.1:5519";

function normalizeUiText(value: string | null | undefined, fallback: string): string {
  const cleaned = String(value ?? "").replace(/\s+/g, " ").trim();
  return cleaned || fallback;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function normalizeScopePath(value: string | null | undefined): string | null {
  const normalized = value?.trim().replace(/\/+$/, "") ?? "";
  return normalized.length > 0 ? normalized : null;
}

function preferredScrollBehavior(): ScrollBehavior {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
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

function formatProviderName(provider: string): string {
  const normalized = provider.trim().toLowerCase();
  if (normalized === "vertex") return "Google Vertex AI";
  if (normalized === "openai") return "OpenAI";
  if (normalized === "dashscope") return "DashScope";
  if (normalized === "minimax") return "MiniMax";
  if (normalized === "ollama") return "Ollama";
  return provider.trim() || "the configured provider";
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
  const [isBasketOpen, setIsBasketOpen] = useState(false);
  const apiBase = import.meta.env.VITE_BACKEND_BASE_URL ?? LOCAL_BACKEND_URL;
  const [draft, setDraft] = useState<DraftResult>(() => createDraft(INITIAL_PROMPT));
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
  const [isEnsuringBackend, setIsEnsuringBackend] = useState(false);
  const [healthRefreshKey, setHealthRefreshKey] = useState(0);
  const [atlasRefreshKey, setAtlasRefreshKey] = useState(0);
  const [scopedIndexStatus, setScopedIndexStatus] = useState<ScopedIndexStatusResponse | null>(null);
  const [scopedIndexStatusError, setScopedIndexStatusError] = useState<string | null>(null);
  const [scopedIndexStatusRetryKey, setScopedIndexStatusRetryKey] = useState(0);
  const [isIndexing, setIsIndexing] = useState(false);
  const [indexingProgress, setIndexingProgress] = useState<DesktopIndexingProgress | null>(null);
  const [indexingResult, setIndexingResult] = useState<DesktopIndexingResult | null>(null);
  const [indexingError, setIndexingError] = useState<string | null>(null);
  const [hasCompletedGeneration, setHasCompletedGeneration] = useState(false);
  const [isIndexingControlPending, setIsIndexingControlPending] = useState(false);
  const seedRef = useRef(1);
  const hasUserNavigatedRef = useRef(false);
  const basketScope = normalizeScopePath(selectedDbPath ?? health.dbPath);
  const {
    items: basketItems,
    assetIds: basketAssetIds,
    toggle: toggleBasketItem,
    addMany: addBasketItems,
    remove: removeBasketItem,
    clear: clearBasketItems,
    retry: retryBasketPersistence,
    persistence: {
      phase: basketPersistencePhase,
      error: basketPersistenceError,
      isHydrated: isBasketHydrated,
    },
  } = usePersistedAtlasBasket({
    apiBase,
    scope: basketScope,
    selectedImageLibraryDir: selectedFolderPath,
    fallbackImageLibraryDir: health.imageLibraryDir,
    connectionState: health.state,
  });
  const {
    activeVariant,
    isGenerating,
    error: generationError,
    progress: generationProgress,
    run: runGeneration,
    cancel: handleCancelGeneration,
    reset: resetGeneration,
  } = useDraftGeneration({
    apiBase,
    prompt,
    contextAssetIds: basketAssetIds,
    health,
    selectedImageLibraryDir: selectedFolderPath,
    selectedDbPath,
    desktopRuntime,
    onStarted: () => {
      seedRef.current += 1;
      setCopyState("idle");
    },
    onCopyUpdate: (copyUpdate) => {
      setDraft((currentDraft) => applyDraftCopyUpdate(currentDraft, copyUpdate));
    },
    onResult: (nextDraft) => {
      setHasCompletedGeneration(true);
      setDraft(nextDraft);
      setActivePhotoId(nextDraft.selected[0]?.id ?? null);
    },
    onBackendRestarted: (status) => {
      setBackendStatus(status);
      setHealth((currentHealth) => ({
        ...currentHealth,
        state: "connected",
        message: status.message,
      }));
      setHealthRefreshKey((current) => current + 1);
    },
  });
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
  const pipeline = createPipelineSteps(
    null,
    generationProgress.phase === "completed" ? DRAFT_PIPELINE_LENGTH : 0,
  );
  const currentDbScope = normalizeScopePath(selectedDbPath ?? health.dbPath);
  const parsedQueryChips = buildParsedQueryChips(activeResultDraft?.parsedQuery ?? null);
  const scopedIndexStatusMatchesCurrent = Boolean(
    currentDbScope
      && normalizeScopePath(scopedIndexStatus?.db_path) === currentDbScope
      && scopedIndexStatus,
  );
  const indexStats = scopedIndexStatusMatchesCurrent
    ? {
        totalRecords: scopedIndexStatus?.index_stats.total_records ?? 0,
        fallbackRecords: scopedIndexStatus?.index_stats.fallback_records ?? 0,
        fallbackRatio: scopedIndexStatus?.index_stats.fallback_ratio ?? 0,
        needsReindex: Boolean(scopedIndexStatus?.index_stats.needs_reindex),
      }
    : null;
  const hasStaleIndex = Boolean(indexStats?.needsReindex);
  const canDescribeScopedIndexHealth = scopedIndexStatusMatchesCurrent;
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
  const indexingPrivacyMessage = selectedVisionProfile
    ? selectedVisionProfile.execution === "local"
      ? `Vision stays on this device using ${selectedVisionProfile.label}. If local vision is unavailable, MemoLens keeps a metadata-only local fallback.`
      : `Processed copies are sent to ${formatProviderName(selectedVisionProfile.provider)} for vision using the selected API profile. If credentials are unavailable or vision fails, MemoLens keeps a metadata-only local fallback.`
    : "Vision routing is not available to verify yet. If vision cannot run, MemoLens indexes a metadata-only fallback locally.";
  const indexStatusLabel = scopedIndexStatusMatchesCurrent
    ? (scopedIndexStatus?.index_stats.total_records ?? 0) > 0
      ? hasStaleIndex
        ? "Index needs rebuild"
        : `Index ready · ${scopedIndexStatus?.index_stats.total_records ?? 0} photos`
      : "Index empty"
    : "Index status pending";

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
    let disposed = false;
    const timeoutId = window.setTimeout(() => controller.abort(), 7500);

    async function loadHealth(): Promise<void> {
      try {
        const response = await fetch(`${apiBase}/healthz`, {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`unexpected status ${response.status}`);
        }

        const payload = (await response.json()) as {
          service?: string;
          api_version?: string;
          desktop_session_authenticated?: boolean;
        };

        if (payload.service !== "memolens-backend" || payload.api_version !== "1") {
          throw new Error("unexpected or incompatible service on the MemoLens port");
        }

        setHealth({
          state: "connected",
          message: `Local service online · ${apiBase}`,
        });
        try {
          const nextBackendSettings = await fetchBackendSettings(apiBase, controller.signal);
          if (disposed) {
            return;
          }
          setBackendSettings(nextBackendSettings);
          setHealth({
            state: "connected",
            message: `Local service online · ${apiBase}`,
            imageLibraryDir: nextBackendSettings.effective.image_library_dir,
            dbPath: nextBackendSettings.effective.db_path,
            visionProfile: nextBackendSettings.effective.vision_profile_name,
            queryProfile: nextBackendSettings.effective.query_profile_name,
            embeddingBackend: nextBackendSettings.effective.embedding_backend,
            indexStats: nextBackendSettings.index_stats
              ? {
                  totalRecords: nextBackendSettings.index_stats.total_records,
                  fallbackRecords: nextBackendSettings.index_stats.fallback_records,
                  fallbackRatio: nextBackendSettings.index_stats.fallback_ratio,
                  needsReindex: nextBackendSettings.index_stats.needs_reindex,
                }
              : undefined,
          });
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
        if (disposed) {
          return;
        }
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
    return () => {
      disposed = true;
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [apiBase, desktopSettings?.defaultDbPath, desktopSettings?.defaultLibraryDir, healthRefreshKey]);

  useEffect(() => {
    const unsubscribe = subscribeToIndexingProgress((progress) => {
      setIndexingProgress(progress);
      setSelectedFolderPath(progress.folderPath);
      setSelectedDbPath(progress.dbPath);
      if (progress.phase === "completed") {
        setIsIndexing(false);
        setScopedIndexStatus(null);
        setHealthRefreshKey((current) => current + 1);
        setAtlasRefreshKey((current) => current + 1);
      }
    });

    return () => {
      unsubscribe?.();
    };
  }, []);

  useEffect(() => {
    setScopedIndexStatus(null);
    setScopedIndexStatusError(null);
    if (health.state !== "connected" || !currentDbScope) {
      return;
    }

    const scope = currentDbScope;
    const controller = new AbortController();
    let disposed = false;
    let timedOut = false;
    const timeoutId = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 7_500);
    void fetchScopedIndexStatus({ apiBase, dbPath: scope, signal: controller.signal })
      .then((status) => {
        if (disposed || controller.signal.aborted) {
          return;
        }
        if (normalizeScopePath(status.db_path) !== scope) {
          throw new Error("Index status returned a different SQLite scope.");
        }
        setScopedIndexStatus(status);
      })
      .catch((error) => {
        if (disposed) {
          return;
        }
        if (timedOut) {
          setScopedIndexStatusError("Index status timed out. Retry before relying on this library state.");
          return;
        }
        setScopedIndexStatusError(
          error instanceof Error ? error.message : "Index status could not be loaded.",
        );
      })
      .finally(() => window.clearTimeout(timeoutId));

    return () => {
      disposed = true;
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [apiBase, atlasRefreshKey, currentDbScope, health.state, scopedIndexStatusRetryKey]);

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

  function handleAddAllResultsToBasket(): void {
    if (!activeResultDraft) {
      return;
    }
    addBasketItems(activeResultDraft.selected);
  }

  function handleClearBasket(): void {
    clearBasketItems();
    setIsBasketOpen(false);
  }

  function scrollToSection(sectionId: string): void {
    hasUserNavigatedRef.current = true;
    document.getElementById(sectionId)?.scrollIntoView({
      block: "start",
      behavior: preferredScrollBehavior(),
    });
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
    if (isEnsuringBackend) {
      return;
    }
    setSettingsMessage(null);
    setIsEnsuringBackend(true);
    try {
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
      if (status.state === "unavailable") {
        setHealth((current) => ({
          ...current,
          state: "offline",
          message: status.message,
        }));
      } else {
        setHealth((current) => ({
          ...current,
          state: "checking",
          message: "Verifying MemoLens local service",
        }));
      }
      setHealthRefreshKey((current) => current + 1);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Local service could not be started.";
      setSettingsMessage(message);
      setHealth((current) => ({
        ...current,
        state: "offline",
        message,
      }));
    } finally {
      setIsEnsuringBackend(false);
    }
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
    setScopedIndexStatus(null);
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
      setScopedIndexStatus(null);
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
      setScopedIndexStatus(null);
      setHealth((currentHealth) => ({
        ...currentHealth,
        imageLibraryDir: backendSettings.effective.image_library_dir,
        dbPath: backendSettings.effective.db_path,
      }));
      setSettingsMessage("Using the local library path.");
      setIndexingResult(null);
      setIndexingProgress(null);
      resetGeneration();
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
    setScopedIndexStatus(null);
    setHealth((currentHealth) => ({
      ...currentHealth,
      imageLibraryDir: selection.folderPath,
      dbPath: selection.dbPath,
    }));
    setIndexingResult(null);
    setIndexingProgress(null);
    resetGeneration();
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
    resetGeneration();
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
      setScopedIndexStatus(null);
      setHealth((currentHealth) => ({
        ...currentHealth,
        imageLibraryDir: resolvedResult.folderPath,
        dbPath: resolvedResult.dbPath,
      }));
      setHealthRefreshKey((current) => current + 1);
      setAtlasRefreshKey((current) => current + 1);
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
  const hasIndexedLibrary = scopedIndexStatusMatchesCurrent
    ? (scopedIndexStatus?.index_stats.total_records ?? 0) > 0
    : false;
  const scopedIndexCount = scopedIndexStatusMatchesCurrent
    ? scopedIndexStatus?.index_stats.total_records ?? 0
    : 0;
  const isScopedIndexStatusPending = Boolean(
    health.state === "connected"
      && currentDbScope
      && !scopedIndexStatusMatchesCurrent
      && !scopedIndexStatusError,
  );
  const journeySteps = [
    {
      label: "Local service",
      detail: health.state === "connected" ? "Verified and private" : "Connect MemoLens",
      complete: health.state === "connected",
    },
    {
      label: "Media library",
      detail: selectedFolderPath ? "Folder selected" : "Choose a folder",
      complete: Boolean(selectedFolderPath),
    },
    {
      label: "Memory layer",
      detail: isScopedIndexStatusPending
        ? "Checking this SQLite library"
        : hasIndexedLibrary
          ? `${scopedIndexCount} photos ready`
          : "Build the index",
      complete: hasIndexedLibrary,
    },
  ];
  const showControlGrid = Boolean(desktopSettings) || Boolean(backendSettings);
  const runtimeHeading = desktopRuntime ? "Desktop runtime" : "Local runtime";
  const handleAtlasInspirationChange = useCallback(
    (cards: AtlasInspirationCard[], storylines: AtlasStoryline[], suggestedQueries: string[]) => {
      setAtlasInspirationCards(cards.map((card) => ({
        ...card,
        title: normalizeUiText(card.title, "Photo idea"),
        summary: normalizeUiText(card.summary, "A useful photo set from your library."),
        prompt: normalizeUiText(card.prompt, "Find 9 strong photos with low repetition"),
        top_concepts: card.top_concepts.map((term) => normalizeUiText(term, "")).filter(Boolean),
      })));
      setAtlasStorylines(storylines.map((storyline) => ({
        ...storyline,
        title: normalizeUiText(storyline.title, "Storyline"),
        summary: normalizeUiText(storyline.summary, "A story-ready group from your library."),
        prompt: normalizeUiText(storyline.prompt, "Pick 9 photos for a natural storyline"),
        top_concepts: storyline.top_concepts.map((term) => normalizeUiText(term, "")).filter(Boolean),
      })));
      setAtlasSuggestedQueries(
        suggestedQueries
          .map((query) => normalizeUiText(query, ""))
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
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 30_000);
    try {
      const suggestions = await fetchAiInspirations(
        apiBase,
        selectedDbPath ?? health.dbPath ?? null,
        basketAssetIds,
        controller.signal,
      );
      setAiSuggestions(suggestions);
      if (suggestions.length === 0) {
        setAiInspirationError("No AI suggestions came back. Try after the library map finishes loading.");
      }
    } catch (error) {
      setAiInspirationError(
        isAbortError(error)
          ? "AI inspiration timed out after 30 seconds. Retry when the local model is responsive."
          : error instanceof Error
            ? error.message
            : "AI inspiration failed.",
      );
    } finally {
      window.clearTimeout(timeoutId);
      setIsGeneratingInspirations(false);
    }
  }

  function handlePrimaryJourneyAction(): void {
    if (health.state !== "connected") {
      void handleEnsureBackend();
      return;
    }
    if (!selectedFolderPath && desktopRuntime) {
      void handlePickFolder();
      return;
    }
    if (!selectedFolderPath) {
      const advancedSettings = document.querySelector<HTMLDetailsElement>(".advanced-settings");
      if (advancedSettings) {
        advancedSettings.open = true;
      }
      scrollToSection("control");
      return;
    }
    if (!hasIndexedLibrary) {
      if (scopedIndexStatusError) {
        setScopedIndexStatusRetryKey((current) => current + 1);
        return;
      }
      scrollToSection("library");
      if (canStartIndexing) {
        void handleStartIndexing();
      }
      return;
    }
    scrollToSection("compose");
  }

  const primaryJourneyLabel =
    health.state !== "connected"
      ? desktopRuntime
        ? "Start local service"
        : "Retry connection"
      : !selectedFolderPath
        ? desktopRuntime
          ? "Choose media folder"
          : "Set library path"
        : !hasIndexedLibrary
          ? scopedIndexStatusError
            ? "Retry library status"
            : isScopedIndexStatusPending
              ? "Checking library…"
              : isIndexing
                ? "Indexing library…"
                : indexingActionLabel
          : "Compose from my library";

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
            <small>Local Media Agent</small>
          </span>
        </a>

        <div className="nav-links-shell">
        <nav className="nav-links" aria-label="Primary">
          <a href="#control" onClick={(event) => handleSectionNav(event, "control")}>
            Setup
          </a>
          <a href="#library" onClick={(event) => handleSectionNav(event, "library")}>
            Library
          </a>
          <a href="#atlas" onClick={(event) => handleSectionNav(event, "atlas")}>
            Workbench
          </a>
          <a href="#video-studio" onClick={(event) => handleSectionNav(event, "video-studio")}>
            Create video
          </a>
          <a href="#compose" onClick={(event) => handleSectionNav(event, "compose")}>
            Photo compose
          </a>
          <a href="#result" onClick={(event) => handleSectionNav(event, "result")}>
            Result
          </a>
        </nav>
        <span className="nav-scroll-hint" aria-hidden="true">More →</span>
        </div>

        <div className="nav-status" role="status" aria-live="polite">
          <span className={`status-pill status-${health.state}`} title={health.message}>
            {health.message}
          </span>
          <span className="status-pill">{runtimeLabel}</span>
        </div>
      </header>

      <main className="page-shell">
        <section className="hero-section" id="hero">
          <div className="hero-copy">
            <p className="eyebrow">Local Media Agent</p>
            <h1>
              Ask your media library
              <span> to find, shape, and cut a story.</span>
            </h1>
            <p className="hero-lede">
              Keep the originals on your machine. MemoLens builds a searchable memory layer,
              explains every selection, and turns an idea into a grounded photo story or video first cut.
            </p>
            <div className="action-row hero-actions">
              <button
                className="primary-button"
                type="button"
                onClick={handlePrimaryJourneyAction}
                disabled={health.state === "checking" || isIndexing || isScopedIndexStatusPending}
              >
                {primaryJourneyLabel}
              </button>
              <button
                className="secondary-button"
                type="button"
                onClick={() => scrollToSection("atlas")}
                disabled={!hasIndexedLibrary}
              >
                Explore memories
              </button>
            </div>
            <div className="hero-chip-row">
              <span className="status-pill">No model key required</span>
              <span className="status-pill">Originals stay untouched</span>
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

        <section className="journey-panel" aria-labelledby="journey-title">
          <div className="journey-copy">
            <p className="eyebrow">Your next step</p>
            <h2 id="journey-title">
              {hasIndexedLibrary
                ? "Your memory layer is ready."
                : health.state !== "connected"
                  ? "Connect the private local service."
                  : selectedFolderPath
                    ? "Turn this folder into memories."
                    : "Choose the library you want to understand."}
            </h2>
            <p>
              {hasIndexedLibrary
                ? "Describe a set, explore a memory, or use Codex to work with the same local index."
                : "Three visible steps take you from a folder to a trustworthy, searchable photo library."}
            </p>
          </div>
          <ol className="journey-steps">
            {journeySteps.map((step, index) => (
              <li className={step.complete ? "complete" : "pending"} key={step.label}>
                <span aria-hidden="true">{step.complete ? "✓" : index + 1}</span>
                <div>
                  <strong>{step.label}</strong>
                  <small>{step.detail}</small>
                </div>
              </li>
            ))}
          </ol>
          <button
            className="primary-button journey-action"
            type="button"
            onClick={handlePrimaryJourneyAction}
            disabled={health.state === "checking" || isIndexing || isScopedIndexStatusPending}
          >
            {primaryJourneyLabel}
          </button>
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

          {health.state === "offline" ? (
            <div className="offline-recovery" role="alert">
              <div>
                <strong>MemoLens local service is offline</strong>
                <span>
                  {desktopRuntime
                    ? "Start it again, then MemoLens will verify that the service on the local port is the expected one."
                    : "Start the local backend, then retry the connection from this page."}
                </span>
              </div>
              <button
                className="primary-button compact-button"
                type="button"
                onClick={() => void handleEnsureBackend()}
                disabled={isEnsuringBackend}
              >
                {isEnsuringBackend
                  ? "Connecting..."
                  : desktopRuntime
                    ? "Start / retry service"
                    : "Retry connection"}
              </button>
            </div>
          ) : null}

          {indexStats && canDescribeScopedIndexHealth ? (
            <p className={hasStaleIndex ? "inline-error" : "inline-note"}>
              {hasStaleIndex
                ? `Current SQLite index looks stale: ${formatPercent(indexStats.fallbackRatio)} of the ${indexStats.totalRecords} records still use filename-only fallback metadata. Rebuild the library once so Vertex can analyze the images again.`
                : `Current SQLite index looks healthy: ${indexStats.totalRecords} records are available for retrieval.`}
            </p>
          ) : null}
          {scopedIndexStatusError ? (
            <p className="inline-error basket-persistence-error" role="alert">
              <span>Current library status is unavailable: {scopedIndexStatusError}</span>
              <button
                type="button"
                className="inline-action-button"
                onClick={() => setScopedIndexStatusRetryKey((current) => current + 1)}
              >
                Retry status
              </button>
            </p>
          ) : null}

          <details className="advanced-settings">
            <summary>
              <span>
                <strong>Advanced settings</strong>
                <small>Model routing, Python runtime, database paths, and diagnostics</small>
              </span>
              <span aria-hidden="true">＋</span>
            </summary>

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

            {settingsMessage ? (
              <p className="inline-note" role="status" aria-live="polite">
                {settingsMessage}
              </p>
            ) : null}
          </details>
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

          <p
            className={`indexing-privacy-note${selectedVisionProfile?.execution === "api" ? " external" : ""}`}
            role="note"
          >
            <strong>Before indexing:</strong> {indexingPrivacyMessage}
          </p>

          <div className="meta-pills">
            <span className="meta-pill path-pill" title={libraryFolderLabel}>
              {libraryFolderLabel}
            </span>
            <span className="meta-pill path-pill" title={libraryDbLabel}>
              {libraryDbLabel}
            </span>
          </div>

          {indexingProgress ? (
            <section className="progress-card" aria-live="polite">
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
              <div
                className="progress-bar"
                role="progressbar"
                aria-label="Library indexing progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={indexingProgress.percent}
              >
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
            <p
              className={indexingResult.status === "failed" ? "inline-error" : "inline-note"}
              role={indexingResult.status === "failed" ? "alert" : "status"}
            >
              {indexingResult.status === "empty"
                ? "No supported images were found in this folder. Choose a folder with JPG, PNG, HEIC, WebP, GIF, BMP, or TIFF files."
                : indexingResult.status === "failed"
                  ? `All ${indexingResult.failed} candidate images failed. Review the errors below and retry.`
                  : indexingResult.status === "partial"
                    ? `Indexed ${indexingResult.indexed}, skipped ${indexingResult.skipped}, and failed ${indexingResult.failed} of ${indexingResult.total} images.`
                    : `Indexed ${indexingResult.indexed} and skipped ${indexingResult.skipped} existing images in the active SQLite library.`}
            </p>
          ) : null}

          {indexingResult?.errors.length ? (
            <details className="indexing-errors">
              <summary>Review {indexingResult.errors.length} indexing error(s)</summary>
              <ul>
                {indexingResult.errors.slice(0, 20).map((message, index) => (
                  <li key={`${index}-${message}`}>{message}</li>
                ))}
              </ul>
            </details>
          ) : null}

          {indexingError ? (
            <p className="inline-error" role="alert">
              {indexingError}
            </p>
          ) : null}
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
            refreshKey={atlasRefreshKey}
            onInspirationChange={handleAtlasInspirationChange}
            basketAssetIds={basketAssetIds}
            onBasketToggle={toggleBasketItem}
            onBasketAddMany={addBasketItems}
          />
        </Suspense>

        <Suspense
          fallback={
            <section className="section-block video-workbench" id="video-studio" aria-labelledby="video-studio-loading-title">
              <p className="eyebrow">Video Creative Workbench</p>
              <h2 id="video-studio-loading-title">Loading the local editing workspace…</h2>
            </section>
          }
        >
          <VideoWorkbench
            apiBase={apiBase}
            imageLibraryDir={selectedFolderPath ?? health.imageLibraryDir ?? null}
            dbPath={selectedDbPath ?? health.dbPath ?? null}
            canUseBackend={health.state === "connected"}
            desktopRuntime={desktopRuntime}
            indexedAssetCount={scopedIndexCount}
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

          {basketPersistenceError ? (
            <p className="inline-error basket-persistence-error" role="alert">
              <span>Selection was not saved: {basketPersistenceError}</span>
              <button
                type="button"
                className="inline-action-button"
                onClick={retryBasketPersistence}
              >
                {isBasketHydrated ? "Retry save" : "Retry sync"}
              </button>
            </p>
          ) : basketItems.length > 0 && isBasketHydrated ? (
            <p className="basket-save-note" role="status" aria-live="polite">
              {basketPersistencePhase === "saving"
                ? "Saving selection..."
                : basketPersistencePhase === "saved"
                  ? "Selection saved locally."
                  : "Selection is synced with this library."}
            </p>
          ) : null}

          <div className="composer-ai-row">
            <label className="composer-field">
              <span className="sr-only">Prompt input</span>
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    (event.metaKey || event.ctrlKey)
                    && event.key === "Enter"
                    && !isGenerating
                    && canGenerateDraft
                  ) {
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
          {aiInspirationError ? (
            <p className="inline-error" role="alert">
              {aiInspirationError}
            </p>
          ) : null}

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
              {isGenerating ? (
                <button
                  className="secondary-button cancel-generation-button"
                  type="button"
                  onClick={handleCancelGeneration}
                >
                  Cancel
                </button>
              ) : null}
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
            <p className="inline-error" role="alert">
              Start the local service before generating a draft from your local library.
            </p>
          ) : null}
          {generationError ? (
            <p className="inline-error basket-persistence-error" role="alert">
              <span>{generationError}</span>
              {canGenerateDraft && !isGenerating ? (
                <button
                  type="button"
                  className="inline-action-button"
                  onClick={() => void runGeneration(activeVariant)}
                >
                  Retry draft
                </button>
              ) : null}
            </p>
          ) : null}

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

          <section className="progress-card live-progress-card" aria-live="polite">
            <div className="progress-head">
              <div>
                <p className="eyebrow">Live progress</p>
                <h3>{generationProgress.title}</h3>
              </div>
              <div className="meta-pills">
                <span className="status-pill">{getDraftGenerationPhaseLabel(generationProgress.phase)}</span>
                <span className="status-pill">
                  {generationProgress.percent === null
                    ? "In progress"
                    : `${generationProgress.percent}%`}
                  </span>
                {isGenerating ? (
                  <button
                    className="secondary-button compact-button cancel-generation-button"
                    type="button"
                    onClick={handleCancelGeneration}
                  >
                    Cancel generation
                  </button>
                ) : null}
              </div>
            </div>
            <div
              className={`progress-bar${generationProgress.percent === null ? " indeterminate" : ""}`}
              role="progressbar"
              aria-label="Draft generation progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={generationProgress.percent ?? undefined}
            >
              <div
                className="progress-bar-fill"
                style={
                  generationProgress.percent === null
                    ? undefined
                    : { width: `${generationProgress.percent}%` }
                }
              />
            </div>
            <p className="progress-caption">
              {generationProgress.phase === "running"
                ? generationProgress.detail
                : generationProgress.stepIndex > 0
                ? `Step ${generationProgress.stepIndex} / ${DRAFT_PIPELINE_LENGTH}`
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
                    <button className="secondary-button" type="button" onClick={() => addBasketItems([activePhoto])}>
                      Add active
                    </button>
                    <button className="secondary-button" type="button" onClick={handleAddAllResultsToBasket}>
                      Add all {activeResultDraft.selectedCount}
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
                      onClick={() => addBasketItems([photo])}
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
        <div className="floating-state" role="status" aria-live="polite">
          <span>
            {isIndexing
              ? "indexing local library..."
              : generationProgress.percent === null
                ? generationProgress.title
                : `${generationProgress.title} · ${generationProgress.percent}%`}
          </span>
          {isGenerating ? (
            <button type="button" onClick={handleCancelGeneration}>
              Cancel
            </button>
          ) : null}
        </div>
      )}

      {basketItems.length > 0 ? (
        <div className={`floating-basket${isBasketOpen ? " open" : ""}`}>
          <button
            type="button"
            className="floating-basket-pill"
            onClick={() => setIsBasketOpen((current) => !current)}
            aria-expanded={isBasketOpen}
            aria-controls="floating-basket-sheet"
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
            <div className="floating-basket-sheet" id="floating-basket-sheet">
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
                    <button type="button" onClick={() => removeBasketItem(item.id)}>
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
