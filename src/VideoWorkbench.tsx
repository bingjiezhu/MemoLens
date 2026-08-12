import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import {
  cancelRenderJob,
  applyTimelineInstruction,
  changeMediaJob,
  createCreativeBrief,
  createTimeline,
  fetchCreativeProject,
  fetchMediaJob,
  fetchRecentMediaJobs,
  fetchRecentRenderJobs,
  fetchRenderJob,
  fetchTimeline,
  fetchVideoCapabilities,
  fetchVideoSegment,
  importVideoAssets,
  previewTimelineInstruction,
  renderDownloadUrl,
  resolveVideoResourceUrl,
  reviseTimeline,
  searchMixedAssets,
  startRender,
  validateTimeline,
  VideoApiError,
} from "./video/api";
import {
  editableTimelineClips,
  editableTimelineTrack,
  formatMilliseconds,
  parseTimelineInstruction,
} from "./video/commands";
import { saveVideoArtifactOnDesktop } from "./video/desktop";
import {
  isAmbiguousVideoMutationOutcome,
  shouldReconcileTimelineMutation,
  VideoMutationLedger,
  videoMutationOutcomeFromError,
  type MutationJson,
  type MutationLease,
  type MutationOutcome,
} from "./video/mutationLedger";
import {
  defaultPreviewFilename,
  formatJobStage,
  formatMediaScore,
  isActiveJobStatus,
  isCancellableJobStatus,
  isSuccessfulRenderStatus,
  isUsableJobStatus,
  summarizeMediaJobs,
} from "./video/jobModel";
import {
  initialVideoWorkbenchState,
  videoWorkbenchReducer,
} from "./video/projectReducer";
import {
  createVideoScopeKey,
  persistVideoSession,
  readPersistedVideoSession,
} from "./video/session";
import {
  deriveVideoWorkflow,
  type VideoWorkflowId,
} from "./video/workflow";
import type {
  CreativeBriefInput,
  CreativeTimeline,
  RenderJob,
  RenderKind,
  TimelineClip,
  TimelineOperation,
  TimelineDiff,
} from "./video/types";

import "./video/video-workbench.css";

interface VideoWorkbenchProps {
  apiBase: string;
  imageLibraryDir?: string | null;
  dbPath?: string | null;
  canUseBackend: boolean;
  desktopRuntime: boolean;
  indexedAssetCount?: number;
}

interface PendingInstruction {
  instruction: string;
  operations: TimelineOperation[];
  diff: TimelineDiff[];
  summaries: string[];
  unrecognized: string[];
  mode: "server" | "local";
}

const INITIAL_BRIEF: CreativeBriefInput = {
  goal: "Create a concise memory film with a calm opening and a stronger finish.",
  audience: "Friends and family",
  platform: "Social video",
  duration_ms: 15_000,
  aspect_ratio: "9:16",
  tone: "warm and natural",
  pace: "slow to energetic",
  must_include: [],
  must_exclude: ["black frames", "near duplicates"],
  narrative_arc: "Establish the place, reveal the human moment, then finish with motion.",
  candidate_refs: [],
};

function humanError(error: unknown, fallback: string): string {
  if (error instanceof DOMException && (error.name === "AbortError" || error.name === "TimeoutError")) {
    return "The request was cancelled or reached its deadline. Your last saved revision is unchanged.";
  }
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function splitTerms(value: string): string[] {
  return value
    .split(/[,;，；\n]/)
    .map((term) => term.trim())
    .filter((term, index, terms) => term.length > 0 && terms.indexOf(term) === index);
}

function VideoWorkbench({
  apiBase,
  imageLibraryDir,
  dbPath,
  canUseBackend,
  desktopRuntime,
  indexedAssetCount = 0,
}: VideoWorkbenchProps) {
  const scopeKey = createVideoScopeKey(imageLibraryDir, dbPath);
  const [state, dispatch] = useReducer(
    videoWorkbenchReducer,
    scopeKey,
    initialVideoWorkbenchState,
  );
  const [searchQuery, setSearchQuery] = useState("quiet opening, human detail, then energetic movement");
  const [selectedRefs, setSelectedRefs] = useState<string[]>([]);
  const [brief, setBrief] = useState<CreativeBriefInput>(INITIAL_BRIEF);
  const [mustIncludeText, setMustIncludeText] = useState("");
  const [mustExcludeText, setMustExcludeText] = useState(INITIAL_BRIEF.must_exclude.join(", "));
  const [commandText, setCommandText] = useState("");
  const [pendingInstruction, setPendingInstruction] = useState<PendingInstruction | null>(null);
  const [importSummary, setImportSummary] = useState<string | null>(null);
  const [isWriting, setIsWriting] = useState(false);
  const [isSavingArtifact, setIsSavingArtifact] = useState(false);
  const [artifactSaved, setArtifactSaved] = useState(false);
  const [ideaConfirmed, setIdeaConfirmed] = useState(false);
  const [materialsConfirmed, setMaterialsConfirmed] = useState(false);
  const [expandedStep, setExpandedStep] = useState<VideoWorkflowId>("idea");
  const [recoveredRenderJobs, setRecoveredRenderJobs] = useState<RenderJob[]>([]);
  const scopeRef = useRef(scopeKey);
  const requestControllersRef = useRef(new Set<AbortController>());
  const mutationLedger = useMemo(() => new VideoMutationLedger(window.localStorage), []);

  const canWrite = desktopRuntime && canUseBackend;
  const hasLibrary = Boolean(imageLibraryDir?.trim() && dbPath?.trim());
  const selectedMatch = state.searchResults.find((match) => match.id === state.selectedMatchId) ?? null;
  const timelineClips = useMemo(
    () => state.timeline ? editableTimelineClips(state.timeline) : [],
    [state.timeline],
  );
  const timelineTrack = state.timeline ? editableTimelineTrack(state.timeline) : null;
  const indexRollup = useMemo(() => summarizeMediaJobs(state.indexJobs), [state.indexJobs]);
  const activeIndexJobs = state.indexJobs.filter((job) => isActiveJobStatus(job.status));
  const cancellableIndexJobs = state.indexJobs.filter((job) => isCancellableJobStatus(job.status));
  const interruptedIndexJobs = state.indexJobs.filter((job) => job.status === "interrupted");
  const renderActive = Boolean(state.renderJob && isActiveJobStatus(state.renderJob.status));
  const renderCancellable = Boolean(state.renderJob && isCancellableJobStatus(state.renderJob.status));
  const renderCompleted = Boolean(state.renderJob && isSuccessfulRenderStatus(state.renderJob.status));
  const previewCapabilityReady = Boolean(
    state.capabilities?.ffmpeg.available
    && state.capabilities.ffprobe.available
    && state.capabilities.encoder_probe.available
    && state.capabilities.supported_output.includes("preview-low")
    && state.capabilities.preview_root_id,
  );
  const canPreviewRender = Boolean(
    canWrite
    && previewCapabilityReady,
  );
  const canVerifiedPreviewSaveAs = Boolean(
    desktopRuntime
    && state.capabilities?.verified_preview_save_as,
  );
  const workflow = useMemo(() => deriveVideoWorkflow({
    idea: ideaConfirmed || Boolean(state.project),
    materials: materialsConfirmed || Boolean(state.project),
    brief: Boolean(state.project),
    timeline: Boolean(state.timeline),
    preview: renderCompleted,
    save: artifactSaved,
  }), [
    artifactSaved,
    ideaConfirmed,
    materialsConfirmed,
    renderCompleted,
    state.project,
    state.timeline,
  ]);
  const renderMediaUrl = state.renderJob
    ? resolveVideoResourceUrl(apiBase, state.renderJob.media_url ?? state.renderJob.output?.media_url ?? state.renderJob.download_url)
    : null;

  function createTrackedController(): AbortController {
    const controller = new AbortController();
    requestControllersRef.current.add(controller);
    return controller;
  }

  function releaseController(controller: AbortController): void {
    requestControllersRef.current.delete(controller);
  }

  function isCurrentScope(capturedScope: string): boolean {
    return scopeRef.current === capturedScope;
  }

  function settleMutationError(lease: MutationLease | null, error: unknown): MutationOutcome {
    const outcome = videoMutationOutcomeFromError(error);
    if (lease) mutationLedger.settle(lease, outcome);
    return outcome;
  }

  function persistTimelineHead(projectId: string, timeline: CreativeTimeline): void {
    persistVideoSession(window.localStorage, scopeKey, {
      projectId,
      timelineId: timeline.id,
      timelineRevision: timeline.revision,
    });
  }

  async function restoreLatestTimeline(input: {
    capturedScope: string;
    projectId: string;
    timelineId: string;
    afterRevision: number;
    signal: AbortSignal;
  }): Promise<CreativeTimeline | null> {
    try {
      const latest = await fetchTimeline(
        apiBase,
        input.timelineId,
        undefined,
        dbPath,
        input.signal,
      );
      if (!isCurrentScope(input.capturedScope)) return null;
      if (latest.revision <= input.afterRevision) return latest;
      dispatch({ type: "timeline_ready", timeline: latest });
      persistTimelineHead(input.projectId, latest);
      setPendingInstruction(null);
      return latest;
    } catch {
      return null;
    }
  }

  async function restoreProjectLatestTimeline(input: {
    capturedScope: string;
    projectId: string;
    previousTimelineId: string | null;
    previousTimelineRevision: number | null;
    signal: AbortSignal;
  }): Promise<boolean> {
    try {
      const project = await fetchCreativeProject(apiBase, input.projectId, dbPath, input.signal);
      const timelineId = project.latest_timeline_id ?? null;
      if (!timelineId || !isCurrentScope(input.capturedScope)) return false;
      const timeline = await fetchTimeline(
        apiBase,
        timelineId,
        project.latest_timeline_revision,
        dbPath,
        input.signal,
      );
      if (!isCurrentScope(input.capturedScope)) return false;
      const changed = timelineId !== input.previousTimelineId
        || timeline.revision !== input.previousTimelineRevision;
      if (!changed) return false;
      dispatch({ type: "project_ready", project });
      dispatch({ type: "timeline_ready", timeline });
      persistTimelineHead(project.id, timeline);
      return true;
    } catch {
      return false;
    }
  }

  async function restoreRenderFromJobList(input: {
    capturedScope: string;
    timelineId: string;
    revision: number;
    kind: RenderKind;
    knownJobIds: Set<string>;
    signal: AbortSignal;
  }): Promise<boolean> {
    try {
      if (!dbPath) return false;
      const jobs = await fetchRecentRenderJobs(apiBase, dbPath, input.signal);
      if (!isCurrentScope(input.capturedScope)) return false;
      setRecoveredRenderJobs(jobs);
      const matching = jobs.find((job) => (
        job.timeline_id === input.timelineId
        && job.timeline_revision === input.revision
        && job.kind === input.kind
        && (!input.knownJobIds.has(job.id) || isActiveJobStatus(job.status))
      ));
      if (!matching) return false;
      dispatch({ type: "render_job", job: matching });
      return true;
    } catch {
      return false;
    }
  }

  async function handleRefreshCapabilities(): Promise<void> {
    if (!canUseBackend) return;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    dispatch({ type: "capabilities_loading" });
    try {
      const capabilities = await fetchVideoCapabilities(apiBase, controller.signal);
      if (isCurrentScope(capturedScope)) dispatch({ type: "capabilities_ready", capabilities });
    } catch (error) {
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "capabilities_error", error: humanError(error, "Capability check failed.") });
      }
    } finally {
      releaseController(controller);
    }
  }

  useEffect(() => {
    scopeRef.current = scopeKey;
    for (const controller of requestControllersRef.current) controller.abort();
    requestControllersRef.current.clear();
    dispatch({ type: "reset_scope", scopeKey });
    setSelectedRefs([]);
    setPendingInstruction(null);
    setImportSummary(null);
    setArtifactSaved(false);
    setIdeaConfirmed(false);
    setMaterialsConfirmed(false);
    setExpandedStep("idea");
    setRecoveredRenderJobs([]);
  }, [scopeKey]);

  useEffect(() => {
    setExpandedStep(workflow.currentId);
  }, [workflow.currentId]);

  useEffect(() => {
    setArtifactSaved(false);
  }, [state.renderJob?.id]);

  useEffect(() => () => {
    for (const controller of requestControllersRef.current) controller.abort();
    requestControllersRef.current.clear();
  }, []);

  useEffect(() => {
    if (!canUseBackend) return;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    dispatch({ type: "capabilities_loading" });
    void fetchVideoCapabilities(apiBase, controller.signal)
      .then((capabilities) => {
        if (isCurrentScope(capturedScope)) dispatch({ type: "capabilities_ready", capabilities });
      })
      .catch((error) => {
        if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
          dispatch({ type: "capabilities_error", error: humanError(error, "Media capabilities could not be loaded.") });
        }
      })
      .finally(() => releaseController(controller));
    return () => controller.abort();
  }, [apiBase, canUseBackend, scopeKey]);

  useEffect(() => {
    if (!canUseBackend || !dbPath) return;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    void Promise.all([
      fetchRecentMediaJobs(apiBase, dbPath, controller.signal),
      desktopRuntime
        ? fetchRecentRenderJobs(apiBase, dbPath, controller.signal)
        : Promise.resolve([]),
    ])
      .then(([jobs, renders]) => {
        if (!isCurrentScope(capturedScope)) return;
        for (const job of jobs) {
          dispatch({ type: "index_job", job });
        }
        setRecoveredRenderJobs(desktopRuntime ? renders : []);
      })
      .catch((error) => {
        if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
          dispatch({ type: "index_error", error: humanError(error, "Persisted media jobs could not be restored.") });
        }
      })
      .finally(() => releaseController(controller));
    return () => controller.abort();
  }, [apiBase, canUseBackend, dbPath, desktopRuntime, scopeKey]);

  useEffect(() => {
    if (!desktopRuntime || !state.timeline || state.renderJob) return;
    const matching = recoveredRenderJobs.find((job) => (
      job.timeline_id === state.timeline?.id
      && job.timeline_revision === state.timeline.revision
    ));
    if (matching) dispatch({ type: "render_job", job: matching });
  }, [desktopRuntime, recoveredRenderJobs, state.renderJob, state.timeline]);

  useEffect(() => {
    if (!canUseBackend || !dbPath || state.project || state.projectPhase !== "idle") return;
    const saved = readPersistedVideoSession(window.localStorage, scopeKey);
    if (!saved) return;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    dispatch({ type: "project_loading" });
    void fetchCreativeProject(apiBase, saved.projectId, dbPath, controller.signal)
      .then(async (project) => {
        if (!isCurrentScope(capturedScope)) return;
        dispatch({ type: "project_ready", project });
        const timelineId = project.latest_timeline_id ?? saved.timelineId ?? null;
        if (!timelineId) return;
        dispatch({ type: "timeline_loading" });
        const timeline = await fetchTimeline(
          apiBase,
          timelineId,
          project.latest_timeline_revision ?? saved.timelineRevision,
          dbPath,
          controller.signal,
        );
        if (isCurrentScope(capturedScope)) dispatch({ type: "timeline_ready", timeline });
      })
      .catch((error) => {
        if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
          dispatch({ type: "project_error", error: `Saved video project could not be restored: ${humanError(error, "restore failed")}` });
        }
      })
      .finally(() => releaseController(controller));
    return () => controller.abort();
  }, [apiBase, canUseBackend, dbPath, scopeKey, state.project, state.projectPhase]);

  useEffect(() => {
    if (activeIndexJobs.length === 0) return;
    const capturedScope = scopeKey;
    const jobIds = activeIndexJobs.map((job) => job.id);
    let stopped = false;
    let controller: AbortController | null = null;
    let timeoutId: number | null = null;
    const poll = async () => {
      controller = new AbortController();
      try {
        if (!dbPath) return;
        const jobs = await Promise.all(jobIds.map((jobId) => fetchMediaJob(apiBase, jobId, dbPath, controller!.signal)));
        if (!stopped && isCurrentScope(capturedScope)) {
          for (const job of jobs) dispatch({ type: "index_job", job });
        }
      } catch (error) {
        if (!stopped && !controller.signal.aborted && isCurrentScope(capturedScope)) {
          dispatch({ type: "index_error", error: humanError(error, "Video indexing status could not be refreshed.") });
        }
      } finally {
        if (!stopped) timeoutId = window.setTimeout(() => void poll(), 1100);
      }
    };
    timeoutId = window.setTimeout(() => void poll(), 1100);
    return () => {
      stopped = true;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
      controller?.abort();
    };
  }, [apiBase, scopeKey, activeIndexJobs.map((job) => `${job.id}:${job.status}`).join("|")]);

  useEffect(() => {
    if (!state.renderJob || !isActiveJobStatus(state.renderJob.status)) return;
    const capturedScope = scopeKey;
    const renderJobId = state.renderJob.id;
    let stopped = false;
    let controller: AbortController | null = null;
    let timeoutId: number | null = null;
    const poll = async () => {
      controller = new AbortController();
      try {
        if (!dbPath) return;
        const job = await fetchRenderJob(apiBase, renderJobId, dbPath, controller.signal);
        if (!stopped && isCurrentScope(capturedScope)) dispatch({ type: "render_job", job });
      } catch (error) {
        if (!stopped && !controller.signal.aborted && isCurrentScope(capturedScope)) {
          dispatch({ type: "render_error", error: humanError(error, "Render status could not be refreshed.") });
        }
      } finally {
        if (!stopped) timeoutId = window.setTimeout(() => void poll(), 1000);
      }
    };
    timeoutId = window.setTimeout(() => void poll(), 1000);
    return () => {
      stopped = true;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
      controller?.abort();
    };
  }, [apiBase, scopeKey, state.renderJob?.id, state.renderJob?.status]);

  useEffect(() => {
    if (!selectedMatch || selectedMatch.result_type !== "video_segment") return;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    dispatch({ type: "segment_loading" });
    void fetchVideoSegment(apiBase, selectedMatch.id, dbPath, controller.signal)
      .then((segment) => {
        if (isCurrentScope(capturedScope)) dispatch({ type: "segment_ready", segment });
      })
      .catch((error) => {
        if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
          dispatch({ type: "segment_error", error: humanError(error, "The selected segment could not be opened.") });
        }
      })
      .finally(() => releaseController(controller));
    return () => controller.abort();
  }, [apiBase, dbPath, scopeKey, selectedMatch?.id, selectedMatch?.media_url, selectedMatch?.result_type]);

  async function handleImport(dryRun: boolean): Promise<void> {
    if (!canWrite || !imageLibraryDir) {
      dispatch({ type: "index_error", error: "Video import is an authenticated desktop write. Open MemoLens Desktop and choose a library first." });
      return;
    }
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    setIsWriting(true);
    setImportSummary(null);
    dispatch({ type: "index_error", error: "" });
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: "desktop:POST:/v1/assets/import",
        action: "asset.import",
        payload: {
          root_path: imageLibraryDir,
          recursive: true,
          kinds: ["video"],
          dry_run: Boolean(dryRun),
        },
      });
      const result = await importVideoAssets({
        apiBase,
        imageLibraryDir,
        dbPath,
        dryRun,
        signal: controller.signal,
        idempotencyKey: lease.idempotencyKey,
      });
      mutationLedger.settle(lease, { kind: "success" });
      if (!isCurrentScope(capturedScope)) return;
      const jobs = result.jobs.length > 0 ? result.jobs : result.job ? [result.job] : [];
      for (const job of jobs) dispatch({ type: "index_job", job });
      setImportSummary(
        dryRun
          ? `Scan complete: ${result.imported} candidates, ${result.skipped} already known, ${result.rejected.length} rejected.`
          : jobs.length > 0
            ? `Queued ${jobs.length} local media job${jobs.length === 1 ? "" : "s"}. Originals remain untouched.`
            : result.message ?? `Import finished with ${result.imported} assets and no pending jobs.`,
      );
    } catch (error) {
      settleMutationError(lease, error);
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "index_error", error: humanError(error, "Video import could not start.") });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function handleIndexJobAction(action: "cancel" | "resume"): Promise<void> {
    const targets = action === "cancel" ? cancellableIndexJobs : interruptedIndexJobs;
    if (targets.length === 0) return;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    setIsWriting(true);
    try {
      const results = await Promise.allSettled(
        targets.map(async (job) => {
          let lease: MutationLease | null = null;
          try {
            lease = await mutationLedger.acquire({
              scope: `desktop:POST:/v1/index/jobs/${job.id}/${action}`,
              action: `media_job.${action}`,
              payload: {},
            });
            const updated = await changeMediaJob(
              apiBase,
              job.id,
              action,
              dbPath ?? "",
              controller.signal,
              lease.idempotencyKey,
            );
            mutationLedger.settle(lease, { kind: "success" });
            return updated;
          } catch (error) {
            settleMutationError(lease, error);
            throw error;
          }
        }),
      );
      if (isCurrentScope(capturedScope)) {
        for (const result of results) {
          if (result.status === "fulfilled") dispatch({ type: "index_job", job: result.value });
        }
        const failure = results.find((result) => result.status === "rejected");
        if (failure?.status === "rejected" && !controller.signal.aborted) {
          dispatch({ type: "index_error", error: humanError(failure.reason, `Could not ${action} video indexing.`) });
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "index_error", error: humanError(error, `Could not ${action} video indexing.`) });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function handleSearch(): Promise<void> {
    if (!canUseBackend || !dbPath || !searchQuery.trim()) {
      dispatch({ type: "search_error", error: "Connect an indexed library and describe the material you need." });
      return;
    }
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    dispatch({ type: "search_loading" });
    try {
      const result = await searchMixedAssets({
        apiBase,
        dbPath,
        query: searchQuery.trim(),
        topK: 24,
        excludedTerms: splitTerms(mustExcludeText),
        signal: controller.signal,
      });
      if (isCurrentScope(capturedScope)) dispatch({ type: "search_ready", results: result.results });
    } catch (error) {
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "search_error", error: humanError(error, "Mixed media search failed.") });
      }
    } finally {
      releaseController(controller);
    }
  }

  function toggleReference(matchId: string): void {
    setSelectedRefs((current) => current.includes(matchId)
      ? current.filter((id) => id !== matchId)
      : [...current, matchId].slice(0, 24));
  }

  async function handleCreateBrief(): Promise<void> {
    if (!canWrite || !dbPath) {
      dispatch({ type: "project_error", error: "Saving a creative project requires the authenticated MemoLens Desktop bridge." });
      return;
    }
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    const normalizedBrief: CreativeBriefInput = {
      ...brief,
      must_include: splitTerms(mustIncludeText),
      must_exclude: splitTerms(mustExcludeText),
      candidate_refs: [...selectedRefs],
    };
    const mutationPayload: MutationJson = {
      goal: normalizedBrief.goal,
      audience: normalizedBrief.audience,
      platform: normalizedBrief.platform,
      duration_ms: normalizedBrief.duration_ms,
      aspect_ratio: normalizedBrief.aspect_ratio,
      tone: normalizedBrief.tone,
      pace: normalizedBrief.pace,
      must_include: normalizedBrief.must_include,
      must_exclude: normalizedBrief.must_exclude,
      candidate_refs: normalizedBrief.candidate_refs ?? [],
      ...(normalizedBrief.narrative_arc === undefined
        ? {}
        : { narrative_arc: normalizedBrief.narrative_arc }),
      db_path: dbPath,
    };
    setIsWriting(true);
    dispatch({ type: "project_loading" });
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: "desktop:POST:/v1/creative/briefs",
        action: "creative_brief.create",
        payload: mutationPayload,
      });
      const project = await createCreativeBrief({
        apiBase,
        dbPath,
        brief: normalizedBrief,
        selectedRefs,
        signal: controller.signal,
        idempotencyKey: lease.idempotencyKey,
      });
      mutationLedger.settle(lease, { kind: "success" });
      if (!isCurrentScope(capturedScope)) return;
      dispatch({ type: "project_ready", project });
      persistVideoSession(window.localStorage, scopeKey, {
        projectId: project.id,
        timelineId: null,
        timelineRevision: null,
      });
    } catch (error) {
      settleMutationError(lease, error);
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "project_error", error: humanError(error, "Creative brief could not be created.") });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function handleCreateTimeline(): Promise<void> {
    if (!canWrite || !state.project) return;
    const capturedProject = state.project;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    setIsWriting(true);
    dispatch({ type: "timeline_loading" });
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: `desktop:POST:/v1/creative/projects/${capturedProject.id}/timelines`,
        action: "timeline.create",
        payload: {
          brief_revision: capturedProject.brief.revision,
          ...(dbPath ? { db_path: dbPath } : {}),
        },
      });
      const result = await createTimeline({
        apiBase,
        projectId: capturedProject.id,
        briefRevision: capturedProject.brief.revision,
        dbPath,
        signal: controller.signal,
        idempotencyKey: lease.idempotencyKey,
      });
      mutationLedger.settle(lease, { kind: "success" });
      if (!isCurrentScope(capturedScope)) return;
      dispatch({ type: "timeline_ready", timeline: result.timeline, diff: result.diff });
      persistTimelineHead(capturedProject.id, result.timeline);
    } catch (error) {
      const outcome = settleMutationError(lease, error);
      const reconciled = !controller.signal.aborted && isAmbiguousVideoMutationOutcome(outcome)
        ? await restoreProjectLatestTimeline({
            capturedScope,
            projectId: capturedProject.id,
            previousTimelineId: state.timeline?.id ?? capturedProject.latest_timeline_id ?? null,
            previousTimelineRevision: state.timeline?.revision ?? capturedProject.latest_timeline_revision ?? null,
            signal: controller.signal,
          })
        : false;
      if (!reconciled && !controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "timeline_error", error: humanError(error, "Storyboard could not be created.") });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function applyTimelineOperations(operations: TimelineOperation[]): Promise<void> {
    if (!canWrite || !state.timeline || operations.length === 0 || isWriting) return;
    const capturedTimeline = state.timeline;
    const capturedProjectId = state.project?.id ?? capturedTimeline.project_id;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    setIsWriting(true);
    dispatch({ type: "timeline_loading" });
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: `desktop:POST:/v1/timelines/${capturedTimeline.id}/revise`,
        action: "timeline.revise",
        payload: {
          base_revision: capturedTimeline.revision,
          operations: operations as unknown as MutationJson,
          ...(dbPath ? { db_path: dbPath } : {}),
        },
      });
      const result = await reviseTimeline({
        apiBase,
        timelineId: capturedTimeline.id,
        baseRevision: capturedTimeline.revision,
        dbPath,
        operations,
        signal: controller.signal,
        idempotencyKey: lease.idempotencyKey,
      });
      mutationLedger.settle(lease, { kind: "success" });
      if (!isCurrentScope(capturedScope)) return;
      dispatch({ type: "timeline_ready", timeline: result.timeline, diff: result.diff });
      persistTimelineHead(capturedProjectId, result.timeline);
      setPendingInstruction(null);
      setCommandText("");
    } catch (error) {
      const outcome = settleMutationError(lease, error);
      const latest = !controller.signal.aborted && shouldReconcileTimelineMutation(outcome)
        ? await restoreLatestTimeline({
            capturedScope,
            projectId: capturedProjectId,
            timelineId: capturedTimeline.id,
            afterRevision: capturedTimeline.revision,
            signal: controller.signal,
          })
        : null;
      const restoredNewHead = Boolean(latest && latest.revision > capturedTimeline.revision);
      if (!restoredNewHead && !controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "timeline_error", error: humanError(error, "Timeline revision was rejected; the saved revision is unchanged.") });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function prepareInstruction(): Promise<void> {
    if (!state.timeline || !commandText.trim()) return;
    const instruction = commandText.trim();
    const capturedTimeline = state.timeline;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    setIsWriting(true);
    setPendingInstruction(null);
    try {
      const preview = await previewTimelineInstruction({
        apiBase,
        timelineId: capturedTimeline.id,
        baseRevision: capturedTimeline.revision,
        dbPath,
        instruction,
        signal: controller.signal,
      });
      if (!isCurrentScope(capturedScope)) return;
      setPendingInstruction({
        instruction,
        operations: preview.operations,
        diff: preview.diff,
        summaries: preview.diff.length > 0
          ? preview.diff.map((item) => item.summary || item.op)
          : preview.operations.map((operation) => operation.op.replace(/_/g, " ")),
        unrecognized: [],
        mode: "server",
      });
    } catch (error) {
      if (controller.signal.aborted || !isCurrentScope(capturedScope)) return;
      if (error instanceof VideoApiError && [400, 404, 422].includes(error.status)) {
        const parsed = parseTimelineInstruction(instruction, capturedTimeline);
        setPendingInstruction({
          instruction,
          ...parsed,
          diff: parsed.operations.map((operation, index) => ({
            op: operation.op,
            clip_id: "clip_id" in operation ? operation.clip_id : null,
            summary: parsed.summaries[index] ?? operation.op.replace(/_/g, " "),
          })),
          mode: "local",
        });
      } else {
        dispatch({ type: "timeline_error", error: humanError(error, "Instruction preview failed; no revision was written.") });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function confirmInstruction(): Promise<void> {
    if (!pendingInstruction || !state.timeline || isWriting) return;
    if (pendingInstruction.mode === "local") {
      await applyTimelineOperations(pendingInstruction.operations);
      return;
    }
    const capturedInstruction = pendingInstruction;
    const capturedTimeline = state.timeline;
    const capturedProjectId = state.project?.id ?? capturedTimeline.project_id;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    setIsWriting(true);
    dispatch({ type: "timeline_loading" });
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: `desktop:POST:/v1/timelines/${capturedTimeline.id}/revise`,
        action: "timeline.instruction.apply",
        payload: {
          base_revision: capturedTimeline.revision,
          instruction: capturedInstruction.instruction,
          apply: true,
          ...(dbPath ? { db_path: dbPath } : {}),
        },
      });
      const result = await applyTimelineInstruction({
        apiBase,
        timelineId: capturedTimeline.id,
        baseRevision: capturedTimeline.revision,
        dbPath,
        instruction: capturedInstruction.instruction,
        signal: controller.signal,
        idempotencyKey: lease.idempotencyKey,
      });
      mutationLedger.settle(lease, { kind: "success" });
      if (!isCurrentScope(capturedScope)) return;
      dispatch({ type: "timeline_ready", timeline: result.timeline, diff: result.diff });
      persistTimelineHead(capturedProjectId, result.timeline);
      setPendingInstruction(null);
      setCommandText("");
    } catch (error) {
      const outcome = settleMutationError(lease, error);
      const latest = !controller.signal.aborted && shouldReconcileTimelineMutation(outcome)
        ? await restoreLatestTimeline({
            capturedScope,
            projectId: capturedProjectId,
            timelineId: capturedTimeline.id,
            afterRevision: capturedTimeline.revision,
            signal: controller.signal,
          })
        : null;
      const restoredNewHead = Boolean(latest && latest.revision > capturedTimeline.revision);
      if (!restoredNewHead && !controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "timeline_error", error: humanError(error, "Instruction apply was rejected; the saved revision is unchanged.") });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function handleValidate(): Promise<boolean> {
    if (!state.timeline) return false;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    dispatch({ type: "validation_loading" });
    try {
      const validation = await validateTimeline({
        apiBase,
        timelineId: state.timeline.id,
        revision: state.timeline.revision,
        dbPath,
        signal: controller.signal,
      });
      if (isCurrentScope(capturedScope)) dispatch({ type: "validation_ready", validation });
      return validation.valid;
    } catch (error) {
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "validation_error", error: humanError(error, "Timeline validation failed.") });
      }
      return false;
    } finally {
      releaseController(controller);
    }
  }

  async function handleRender(kind: RenderKind): Promise<void> {
    if (kind !== "preview") {
      dispatch({ type: "render_error", error: "Final export is locked until Electron issues a one-time output grant." });
      return;
    }
    if (!canPreviewRender || !state.timeline || renderActive) return;
    setIsWriting(true);
    const valid = await handleValidate();
    if (!valid || !state.timeline) {
      setIsWriting(false);
      return;
    }
    const capturedTimeline = state.timeline;
    const previewRootId = state.capabilities?.preview_root_id ?? "";
    const profile = kind === "preview" ? "preview-low" : "export-1080p";
    const knownJobIds = new Set([
      ...recoveredRenderJobs.map((job) => job.id),
      ...(state.renderJob ? [state.renderJob.id] : []),
    ]);
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: "desktop:POST:/v1/renders",
        action: "render.start",
        payload: {
          timeline_id: capturedTimeline.id,
          timeline_revision: capturedTimeline.revision,
          expected_timeline_sha256: capturedTimeline.content_sha256 ?? "",
          output: { root_id: previewRootId },
          profile,
          ...(dbPath ? { db_path: dbPath } : {}),
        },
      });
      const job = await startRender({
        apiBase,
        timelineId: capturedTimeline.id,
        revision: capturedTimeline.revision,
        timelineSha256: capturedTimeline.content_sha256,
        previewRootId,
        kind,
        dbPath,
        signal: controller.signal,
        idempotencyKey: lease.idempotencyKey,
      });
      mutationLedger.settle(lease, { kind: "success" });
      if (isCurrentScope(capturedScope)) dispatch({ type: "render_job", job });
    } catch (error) {
      const outcome = settleMutationError(lease, error);
      const reconciled = !controller.signal.aborted && isAmbiguousVideoMutationOutcome(outcome)
        ? await restoreRenderFromJobList({
            capturedScope,
            timelineId: capturedTimeline.id,
            revision: capturedTimeline.revision,
            kind,
            knownJobIds,
            signal: controller.signal,
          })
        : false;
      if (!reconciled && !controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "render_error", error: humanError(error, `${kind} render could not start.`) });
      }
    } finally {
      releaseController(controller);
      if (isCurrentScope(capturedScope)) setIsWriting(false);
    }
  }

  async function handleCancelRender(): Promise<void> {
    if (!state.renderJob || !renderActive) return;
    const capturedJob = state.renderJob;
    const capturedScope = scopeKey;
    const controller = createTrackedController();
    let lease: MutationLease | null = null;
    try {
      lease = await mutationLedger.acquire({
        scope: `desktop:POST:/v1/renders/${capturedJob.id}/cancel`,
        action: "render.cancel",
        payload: {},
      });
      const job = await cancelRenderJob(
        apiBase,
        capturedJob.id,
        dbPath ?? "",
        controller.signal,
        lease.idempotencyKey,
      );
      mutationLedger.settle(lease, { kind: "success" });
      if (isCurrentScope(capturedScope)) dispatch({ type: "render_job", job });
    } catch (error) {
      settleMutationError(lease, error);
      if (!controller.signal.aborted && isCurrentScope(capturedScope)) {
        dispatch({ type: "render_error", error: humanError(error, "Render could not be cancelled.") });
      }
    } finally {
      releaseController(controller);
    }
  }

  async function handleSaveArtifact(): Promise<void> {
    if (!state.renderJob || !renderCompleted || !state.timeline) return;
    const artifactUrl = renderDownloadUrl(apiBase, state.renderJob);
    const filename = state.renderJob.filename ?? state.renderJob.output?.filename ?? defaultPreviewFilename(state.timeline);
    if (!canVerifiedPreviewSaveAs) {
      dispatch({
        type: "save_message",
        message: desktopRuntime
          ? "Verified preview Save As is unavailable in this runtime."
          : "Open this project in MemoLens Desktop to verify and save the preview.",
      });
      return;
    }
    const expectedSha256 = state.renderJob.output_sha256 ?? state.renderJob.output?.output_sha256;
    const expectedSizeBytes = state.renderJob.size_bytes ?? state.renderJob.output?.size_bytes;
    if (!expectedSha256 || typeof expectedSizeBytes !== "number" || expectedSizeBytes <= 0) {
      dispatch({ type: "save_message", message: "The preview is missing its integrity proof. Re-render before saving." });
      return;
    }
    setIsSavingArtifact(true);
    dispatch({ type: "save_message", message: null });
    try {
      const result = await saveVideoArtifactOnDesktop({
        artifactUrl,
        suggestedFilename: filename,
        expectedSha256,
        expectedSizeBytes,
      });
      setArtifactSaved(result?.status === "saved");
      dispatch({ type: "save_message", message: result?.message ?? "Desktop save bridge is unavailable." });
    } catch (error) {
      setArtifactSaved(false);
      dispatch({ type: "save_message", message: humanError(error, "Video could not be saved.") });
    } finally {
      setIsSavingArtifact(false);
    }
  }

  function clipOperationForTrim(clip: TimelineClip, edge: "in" | "out", deltaMs: number): TimelineOperation | null {
    if (clip.kind === "image" || typeof clip.source_in_ms !== "number" || typeof clip.source_out_ms !== "number") {
      const nextDuration = Math.max(250, clip.timeline_duration_ms + deltaMs);
      return {
        op: "set_duration",
        clip_id: clip.id,
        timeline_duration_ms: nextDuration,
      };
    }
    const sourceIn = edge === "in"
      ? Math.max(0, Math.min(clip.source_out_ms - 100, clip.source_in_ms + deltaMs))
      : clip.source_in_ms;
    const sourceOut = edge === "out"
      ? Math.max(sourceIn + 100, clip.source_out_ms + deltaMs)
      : clip.source_out_ms;
    return { op: "trim_clip", clip_id: clip.id, source_in_ms: sourceIn, source_out_ms: sourceOut };
  }

  const selectedSegmentMedia = desktopRuntime && selectedMatch?.result_type === "video_segment"
    ? resolveVideoResourceUrl(apiBase, state.segment?.media_url ?? selectedMatch.media_url)
    : null;
  const selectedThumb = selectedMatch
    ? resolveVideoResourceUrl(apiBase, state.segment?.thumbnail_url ?? selectedMatch.thumbnail_url)
    : null;
  const selectedVideoSrc = selectedSegmentMedia && selectedMatch?.start_ms !== null && selectedMatch?.end_ms !== null
    ? `${selectedSegmentMedia}#t=${Math.max(0, (selectedMatch?.start_ms ?? 0) / 1000)},${Math.max(0, (selectedMatch?.end_ms ?? 0) / 1000)}`
    : selectedSegmentMedia;
  const workflowSummaries: Record<VideoWorkflowId, string> = {
    idea: brief.goal.length > 38 ? `${brief.goal.slice(0, 38)}…` : brief.goal,
    materials: `${selectedRefs.length || state.project?.candidates.length || indexedAssetCount} grounded asset${(selectedRefs.length || state.project?.candidates.length || indexedAssetCount) === 1 ? "" : "s"}`,
    brief: state.project ? `${state.project.title} · r${state.project.brief.revision}` : "Creative constraints",
    timeline: state.timeline ? `r${state.timeline.revision} · ${timelineClips.length} clips · ${formatMilliseconds(state.timeline.format.duration_ms)}` : "Immutable first cut",
    preview: state.renderJob ? state.renderJob.filename ?? state.renderJob.output?.filename ?? "Rendered preview" : "Bounded local render",
    save: artifactSaved ? "Verified copy saved" : "Verified Save As",
  };

  return (
    <section className="section-block video-workbench" id="video-studio" aria-labelledby="video-studio-title">
      <div className="video-workbench-heading">
        <div>
          <p className="eyebrow">Video Creative Workbench</p>
          <h2 id="video-studio-title">Turn indexed media into a grounded first cut.</h2>
          <p>
            Split videos into timestamped local segments, ground a creative brief in real files,
            revise a versioned timeline, then preview locally with FFmpeg and save a verified preview copy.
          </p>
        </div>
        <div className="video-local-contract" role="note">
          <strong>Local by default</strong>
          <span>Probe, fallback analysis, timeline validation, and preview rendering stay on this machine.</span>
          <span>Original images and videos are never overwritten.</span>
        </div>
      </div>

      <ol className="video-stepper" aria-label="Video creation progress">
        {workflow.steps.map((step) => (
          <li key={step.id} className={`video-step-${step.status}`}>
            <button
              type="button"
              aria-controls={expandedStep === step.id ? `video-step-${step.id}-panel` : undefined}
              aria-current={step.id === workflow.currentId ? "step" : undefined}
              aria-expanded={expandedStep === step.id}
              disabled={!step.canOpen}
              onClick={() => setExpandedStep(step.id)}
            >
              <span aria-hidden="true">{step.status === "complete" ? "✓" : step.number}</span>
              <span>
                <strong>{step.label}</strong>
                <small>{step.status === "complete" ? workflowSummaries[step.id] : step.status === "current" ? "Next action" : "Locked"}</small>
              </span>
            </button>
          </li>
        ))}
      </ol>

      <p className="video-workflow-status" role="status" aria-live="polite">
        Step {workflow.steps.find((step) => step.id === workflow.currentId)?.number} of {workflow.steps.length}: {workflow.steps.find((step) => step.id === workflow.currentId)?.label}
      </p>

      <div className="video-workspace-stack">
        {expandedStep === "idea" ? (
          <section
            className="video-panel video-step-panel"
            id="video-step-idea-panel"
            aria-labelledby="video-idea-title"
          >
            <div className="video-panel-head">
              <div>
                <p className="eyebrow">01 · Idea</p>
                <h3 id="video-idea-title">Start with the film you want to make</h3>
                <p className="video-muted">Describe the outcome first. MemoLens will find evidence in your own library next.</p>
              </div>
              {state.project ? <span className="meta-pill">Saved in brief r{state.project.brief.revision}</span> : null}
            </div>
            <div className="video-idea-form">
              <label className="video-field video-field-wide">
                <span>What should this video become?</span>
                <textarea
                  value={brief.goal}
                  onChange={(event) => setBrief({ ...brief, goal: event.target.value })}
                  placeholder="A concise memory film with a calm opening and a stronger finish"
                  required
                />
              </label>
              <label className="video-field video-field-wide">
                <span>Story direction</span>
                <input
                  value={brief.narrative_arc}
                  onChange={(event) => setBrief({ ...brief, narrative_arc: event.target.value })}
                  placeholder="Establish the place, reveal the human moment, finish with motion"
                />
              </label>
              <div className="video-form-actions video-field-wide">
                <button
                  type="button"
                  className="primary-button"
                  disabled={!brief.goal.trim()}
                  onClick={() => setIdeaConfirmed(true)}
                >
                  Find material for this idea
                </button>
                <span>You can revise this direction later without touching original media.</span>
              </div>
            </div>
          </section>
        ) : null}

        {expandedStep === "materials" ? (
        <section
          className="video-panel video-step-panel"
          id="video-step-materials-panel"
          aria-labelledby="video-materials-title"
        >
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">02 · Material</p>
              <h3 id="video-materials-title">Choose grounded images and video moments</h3>
              <p className="video-muted">Index once, search naturally, then keep only the evidence that supports your idea.</p>
            </div>
            <span className="meta-pill">{selectedRefs.length} selected</span>
          </div>

          <details className="video-setup-disclosure">
            <summary>Local media readiness</summary>
            <div className="video-panel-head">
              <p className="video-muted">FFmpeg, source probing, local analysis, and verified Save As</p>
              <button
                type="button"
                className="secondary-button compact-button"
                onClick={() => void handleRefreshCapabilities()}
                disabled={!canUseBackend || state.capabilitiesPhase === "loading"}
              >
                {state.capabilitiesPhase === "loading" ? "Checking…" : "Check again"}
              </button>
            </div>
          {state.capabilitiesPhase === "loading" ? (
            <div className="video-state-card" role="status">Checking FFmpeg, ffprobe, and local analysis modes…</div>
          ) : state.capabilitiesError ? (
            <div className="video-state-card error" role="alert">{state.capabilitiesError}</div>
          ) : state.capabilities ? (
            <div className="video-capability-grid">
              <article className={state.capabilities.ffmpeg.available ? "ready" : "blocked"}>
                <span>FFmpeg</span>
                <strong>{state.capabilities.ffmpeg.available ? "Ready" : "Missing"}</strong>
                <small>{state.capabilities.ffmpeg.version ?? "Install FFmpeg 6+ to analyze video and render previews."}</small>
              </article>
              <article className={state.capabilities.ffprobe.available ? "ready" : "blocked"}>
                <span>ffprobe</span>
                <strong>{state.capabilities.ffprobe.available ? "Ready" : "Missing"}</strong>
                <small>{state.capabilities.ffprobe.version ?? "Media metadata cannot be verified."}</small>
              </article>
              <article className={state.capabilities.encoder_probe.available ? "ready" : "blocked"}>
                <span>Preview encoder</span>
                <strong>{state.capabilities.encoder_probe.available ? "Verified" : formatJobStage(state.capabilities.encoder_probe.code ?? "encoder unavailable")}</strong>
                <small>{state.capabilities.encoder_probe.message ?? (state.capabilities.encoder_probe.available
                  ? `Real encode/decode probe passed${state.capabilities.encoder_probe.profiles.length > 0 ? ` for ${state.capabilities.encoder_probe.profiles.join(", ")}` : ""}.`
                  : "The required libx264/AAC encode/decode probe did not pass.")}</small>
              </article>
              <article className={state.capabilities.verified_preview_save_as ? "ready" : "blocked"}>
                <span>Verified Save As</span>
                <strong>{state.capabilities.verified_preview_save_as ? "Available" : "Unavailable"}</strong>
                <small>{state.capabilities.verified_preview_save_as
                  ? "Desktop can verify the preview digest and size before saving a copy."
                  : "Preview artifacts cannot be offered through the verified desktop Save As flow."}</small>
              </article>
              <article className="ready">
                <span>Visual understanding</span>
                <strong>{formatJobStage(state.capabilities.vision.mode)}</strong>
                <small>{state.capabilities.vision.available ? "Semantic cues available." : "Honest metadata-only fallback."}</small>
              </article>
              <article className={state.capabilities.transcription.available ? "ready" : "partial"}>
                <span>Transcription</span>
                <strong>{formatJobStage(state.capabilities.transcription.mode)}</strong>
                <small>{state.capabilities.transcription.available ? "Timestamped speech can guide cuts." : "Video indexing still works without dialogue understanding."}</small>
              </article>
            </div>
          ) : (
            <div className="video-state-card">Connect the local service to inspect media capabilities.</div>
          )}
          {!desktopRuntime ? (
            <p className="video-inline-warning" role="note">
              Browser mode can search metadata and inspect available thumbnails. Source-video playback, import, revisions, rendering, and secure Save As require MemoLens Desktop.
            </p>
          ) : null}
          </details>

        <div className="video-material-section" aria-labelledby="video-library-title">
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">02 · Library</p>
              <h3 id="video-library-title">Index the mixed media library</h3>
              <p className="video-muted path-line" title={imageLibraryDir ?? "No library selected"}>
                {imageLibraryDir ?? "Choose a local library in MemoLens Setup first."}
              </p>
            </div>
            <div className="action-row">
              <button
                type="button"
                className="secondary-button compact-button"
                onClick={() => void handleImport(true)}
                disabled={!canWrite || !hasLibrary || isWriting || activeIndexJobs.length > 0}
              >
                Scan changes
              </button>
              <button
                type="button"
                className="primary-button compact-button"
                onClick={() => void handleImport(false)}
                disabled={!canWrite || !hasLibrary || isWriting || activeIndexJobs.length > 0}
              >
                Index videos
              </button>
              {cancellableIndexJobs.length > 0 ? (
                <button type="button" className="secondary-button compact-button danger-button" onClick={() => void handleIndexJobAction("cancel")}>
                  Cancel indexing
                </button>
              ) : null}
              {interruptedIndexJobs.length > 0 ? (
                <button type="button" className="secondary-button compact-button" onClick={() => void handleIndexJobAction("resume")}>
                  Resume interrupted
                </button>
              ) : null}
            </div>
          </div>

          {state.indexJobs.length > 0 ? (
            <div className="video-job-card" aria-live="polite">
              <div className="video-job-summary">
                <strong>
                  {indexRollup.active > 0
                    ? `Indexing video · ${indexRollup.active} active`
                    : `Media jobs settled · ${indexRollup.completed} usable`}
                </strong>
                <span>{indexRollup.progress}%</span>
              </div>
              <div
                className="progress-bar"
                role="progressbar"
                aria-label="Video indexing progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={indexRollup.progress}
              >
                <div className="progress-bar-fill" style={{ width: `${indexRollup.progress}%` }} />
              </div>
              <div className="video-job-list">
                {state.indexJobs.slice(0, 8).map((job) => (
                  <span key={job.id}>
                    <strong>{formatJobStage(job.stage)}</strong>
                    <small>{job.status} · {job.progress}%</small>
                  </span>
                ))}
              </div>
              {indexRollup.failed > 0 ? <p className="video-inline-error">{indexRollup.failed} job(s) failed. Inspect the messages and scan again after fixing the source.</p> : null}
            </div>
          ) : (
            <div className="video-state-card">
              <strong>No video analysis jobs in this session.</strong>
              <span>MemoLens will probe MP4/MOV/M4V files, find timestamped shots, and keep image indexing backward compatible.</span>
            </div>
          )}
          {importSummary ? <p className="video-inline-note" role="status">{importSummary}</p> : null}
          {state.indexError ? <p className="video-inline-error" role="alert">{state.indexError}</p> : null}
        </div>

        <div className="video-material-section" aria-labelledby="video-search-title">
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">03 · Find material</p>
              <h3 id="video-search-title">Search images and timestamped video segments</h3>
            </div>
            <span className="meta-pill">No whole-video guesses</span>
          </div>
          <form
            className="video-search-form"
            onSubmit={(event) => {
              event.preventDefault();
              void handleSearch();
            }}
          >
            <label>
              <span className="sr-only">Mixed media search</span>
              <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search filenames and indexed sidecar/segment text" />
            </label>
            <button type="submit" className="primary-button" disabled={!canUseBackend || !dbPath || state.searchPhase === "loading"}>
              {state.searchPhase === "loading" ? "Searching…" : "Search mixed media"}
            </button>
          </form>

          {state.searchPhase === "error" ? <div className="video-state-card error" role="alert">{state.searchError}</div> : null}
          {state.searchPhase === "empty" ? (
            <div className="video-state-card">
              <strong>No grounded matches yet.</strong>
              <span>Try a broader visual description or finish video indexing, then retry.</span>
              <button type="button" className="secondary-button compact-button" onClick={() => void handleSearch()}>Retry search</button>
            </div>
          ) : null}
          {state.searchPhase === "loading" ? <div className="video-state-card" role="status">Ranking image assets and timestamped video segments…</div> : null}

          {state.searchResults.length > 0 ? (
            <div className="video-source-layout">
              <div className="video-source-grid" aria-label="Mixed media search results">
                {state.searchResults.map((match) => {
                  const thumb = resolveVideoResourceUrl(apiBase, match.thumbnail_url);
                  const selected = match.id === state.selectedMatchId;
                  const referenced = selectedRefs.includes(match.id);
                  return (
                    <article key={match.id} className={`video-source-card${selected ? " active" : ""}`}>
                      <button
                        type="button"
                        className="video-source-open"
                        aria-pressed={selected}
                        onClick={() => dispatch({ type: "select_match", matchId: match.id })}
                      >
                        <span className="video-source-art">
                          {thumb ? <img src={thumb} alt="" loading="lazy" decoding="async" /> : <span className="video-source-placeholder">{match.result_type === "video_segment" ? "VIDEO" : "IMAGE"}</span>}
                          <em>{match.result_type === "video_segment" ? "Video segment" : "Image"}</em>
                        </span>
                        <span className="video-source-copy">
                          <strong>{match.title ?? match.filename ?? match.summary}</strong>
                          <small>
                            {match.start_ms !== null && match.end_ms !== null
                              ? `${formatMilliseconds(match.start_ms)} → ${formatMilliseconds(match.end_ms)}`
                              : "Still image"}
                          </small>
                          <span>{match.summary}</span>
                        </span>
                      </button>
                      <label className="video-source-check">
                        <input type="checkbox" checked={referenced} onChange={() => toggleReference(match.id)} />
                        <span>Use in brief</span>
                        <em>{formatMediaScore(match.score)}</em>
                      </label>
                    </article>
                  );
                })}
              </div>

              <aside className="video-segment-inspector">
                {selectedMatch ? (
                  <>
                    <div className="video-preview-frame">
                      {selectedMatch.result_type === "video_segment" ? (
                        selectedVideoSrc ? (
                          <video key={selectedVideoSrc} controls preload="metadata" poster={selectedThumb ?? undefined} src={selectedVideoSrc}>
                            Your browser cannot play this local video segment.
                          </video>
                        ) : selectedThumb ? (
                          <img src={selectedThumb} alt={selectedMatch.title ?? "Selected video segment"} />
                        ) : (
                          <div
                            className="video-state-card"
                            role={state.segmentPhase === "loading" ? "status" : undefined}
                          >
                            {state.segmentPhase === "loading" ? "Loading timestamp preview…" : "No playable proxy is available for this codec yet."}
                          </div>
                        )
                      ) : selectedThumb ? (
                        <img src={selectedThumb} alt={selectedMatch.title ?? "Selected image"} />
                      ) : (
                        <div className="video-state-card">No preview available.</div>
                      )}
                    </div>
                    <p className="eyebrow">Selected evidence</p>
                    <h4>{selectedMatch.title ?? selectedMatch.filename ?? "Local media"}</h4>
                    <p>{selectedMatch.summary}</p>
                    <div className="meta-pills">
                      {selectedMatch.provenance.map((source) => <span key={source} className="meta-pill">{source}</span>)}
                      {selectedMatch.confidence !== null ? <span className="meta-pill">confidence {formatMediaScore(selectedMatch.confidence)}</span> : null}
                      {selectedMatch.analysis_revision !== null ? <span className="meta-pill">analysis r{selectedMatch.analysis_revision}</span> : null}
                    </div>
                    {state.segment?.transcript.length ? (
                      <details className="video-transcript">
                        <summary>Timestamped transcript</summary>
                        {state.segment.transcript.map((line) => (
                          <p key={line.id}><time>{formatMilliseconds(line.start_ms)}</time>{line.text}</p>
                        ))}
                      </details>
                    ) : selectedMatch.result_type === "video_segment" ? (
                      <p className="video-muted">No transcript is available; this result relies on indexed metadata and technical evidence.</p>
                    ) : null}
                    {state.segmentError ? <p className="video-inline-error" role="alert">{state.segmentError}</p> : null}
                  </>
                ) : (
                  <div className="video-state-card">Choose a result to inspect its source range and evidence.</div>
                )}
              </aside>
            </div>
          ) : null}
        </div>

        <div className="video-step-actions">
          <button
            type="button"
            className="primary-button"
            disabled={
              !hasLibrary
              || !canUseBackend
              || !dbPath
              || (
                indexedAssetCount <= 0
                && state.searchResults.length === 0
                && !state.indexJobs.some((job) => isUsableJobStatus(job.status))
              )
            }
            onClick={() => setMaterialsConfirmed(true)}
          >
            Use these materials
          </button>
          <span>Selected references will remain traceable through the brief and timeline.</span>
        </div>
        </section>
        ) : null}

        {expandedStep === "brief" ? (
        <section
          className="video-panel video-step-panel"
          id="video-step-brief-panel"
          aria-labelledby="video-create-title"
        >
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">03 · Brief</p>
              <h3 id="video-create-title">Write an explicit creative brief</h3>
              <p className="video-muted">The Director can suggest structure, but these visible constraints remain authoritative.</p>
            </div>
            <span className="meta-pill">{selectedRefs.length} grounded references</span>
          </div>
          <form
            className="video-brief-grid"
            onSubmit={(event) => {
              event.preventDefault();
              void handleCreateBrief();
            }}
          >
            <label className="video-field video-field-wide">
              <span>Goal</span>
              <textarea value={brief.goal} onChange={(event) => setBrief({ ...brief, goal: event.target.value })} required />
            </label>
            <label className="video-field">
              <span>Audience</span>
              <input value={brief.audience} onChange={(event) => setBrief({ ...brief, audience: event.target.value })} />
            </label>
            <label className="video-field">
              <span>Platform</span>
              <select value={brief.platform} onChange={(event) => setBrief({ ...brief, platform: event.target.value })}>
                <option>Social video</option>
                <option>Personal archive</option>
                <option>Presentation</option>
                <option>Landscape film</option>
              </select>
            </label>
            <label className="video-field">
              <span>Target duration</span>
              <select value={brief.duration_ms} onChange={(event) => setBrief({ ...brief, duration_ms: Number(event.target.value) })}>
                <option value={10_000}>10 seconds</option>
                <option value={15_000}>15 seconds</option>
                <option value={30_000}>30 seconds</option>
                <option value={60_000}>60 seconds</option>
              </select>
            </label>
            <label className="video-field">
              <span>Aspect ratio</span>
              <select value={brief.aspect_ratio} onChange={(event) => setBrief({ ...brief, aspect_ratio: event.target.value })}>
                <option value="9:16">9:16 vertical</option>
                <option value="16:9">16:9 landscape</option>
                <option value="1:1">1:1 square</option>
              </select>
            </label>
            <label className="video-field">
              <span>Tone</span>
              <input value={brief.tone} onChange={(event) => setBrief({ ...brief, tone: event.target.value })} />
            </label>
            <label className="video-field">
              <span>Pace</span>
              <input value={brief.pace} onChange={(event) => setBrief({ ...brief, pace: event.target.value })} />
            </label>
            <label className="video-field">
              <span>Must include</span>
              <input value={mustIncludeText} onChange={(event) => setMustIncludeText(event.target.value)} placeholder="people, beach, blue title" />
            </label>
            <label className="video-field">
              <span>Must exclude</span>
              <input value={mustExcludeText} onChange={(event) => setMustExcludeText(event.target.value)} placeholder="black frames, faces" />
            </label>
            <label className="video-field video-field-wide">
              <span>Narrative arc</span>
              <input value={brief.narrative_arc} onChange={(event) => setBrief({ ...brief, narrative_arc: event.target.value })} />
            </label>
            <div className="video-form-actions video-field-wide">
              <button type="submit" className="primary-button" disabled={!canWrite || isWriting || !brief.goal.trim()}>
                {state.projectPhase === "loading" ? "Grounding brief…" : state.project ? "Create a new project" : "Ground brief in my library"}
              </button>
              {!canWrite ? <span>Authenticated desktop write required.</span> : <span>Creates a saved project; it does not render yet.</span>}
            </div>
          </form>
          {state.projectError ? <p className="video-inline-error" role="alert">{state.projectError}</p> : null}

          {state.project ? (
            <article className="video-project-summary">
              <div>
                <p className="eyebrow">Saved project · brief r{state.project.brief.revision}</p>
                <h4>{state.project.title}</h4>
                <p>{state.project.brief.narrative_arc}</p>
              </div>
              <div className="video-project-evidence">
                <span><strong>{state.project.brief.candidate_refs.length || state.project.candidates.length}</strong> grounded candidates</span>
                <span><strong>{state.project.brief.missing_assets.length}</strong> missing-material notes</span>
                <span><strong>{formatMilliseconds(state.project.brief.duration_ms)}</strong> target</span>
              </div>
              {state.project.brief.missing_assets.length > 0 ? (
                <div className="video-missing-list">
                  <strong>Not found in the library</strong>
                  <ul>{state.project.brief.missing_assets.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              ) : null}
            </article>
          ) : null}
        </section>
        ) : null}

        {expandedStep === "timeline" ? (
        <section
          className="video-panel video-step-panel"
          id="video-step-timeline-panel"
          aria-labelledby="video-timeline-title"
        >
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">04 · Storyboard + Timeline</p>
              <h3 id="video-timeline-title">One immutable revision at a time</h3>
            </div>
            {state.timeline ? (
              <div className="meta-pills">
                <span className="meta-pill">revision {state.timeline.revision}</span>
                <span className="meta-pill">{state.timeline.format.width}×{state.timeline.format.height}</span>
                <span className="meta-pill">{formatMilliseconds(state.timeline.format.duration_ms)}</span>
              </div>
            ) : null}
          </div>

          {!state.timeline ? (
            <div className="video-state-card">
              <strong>No storyboard yet.</strong>
              <span>Your saved brief is ready. Build revision 1 from its grounded candidates.</span>
              <button
                type="button"
                className="primary-button compact-button"
                onClick={() => void handleCreateTimeline()}
                disabled={!canWrite || !state.project || isWriting}
              >
                {state.timelinePhase === "loading" ? "Building storyboard…" : "Build grounded storyboard"}
              </button>
            </div>
          ) : (
            <>
              <div className="video-format-toolbar" role="group" aria-label="Timeline format">
                <span>Canvas</span>
                <button type="button" aria-pressed={state.timeline.format.width === 1080 && state.timeline.format.height === 1920} disabled={isWriting} onClick={() => void applyTimelineOperations([{ op: "set_format", width: 1080, height: 1920, fps: state.timeline!.format.fps }])}>9:16</button>
                <button type="button" aria-pressed={state.timeline.format.width === 1920 && state.timeline.format.height === 1080} disabled={isWriting} onClick={() => void applyTimelineOperations([{ op: "set_format", width: 1920, height: 1080, fps: state.timeline!.format.fps }])}>16:9</button>
                <button type="button" aria-pressed={state.timeline.format.width === 1080 && state.timeline.format.height === 1080} disabled={isWriting} onClick={() => void applyTimelineOperations([{ op: "set_format", width: 1080, height: 1080, fps: state.timeline!.format.fps }])}>1:1</button>
              </div>

              <div className="video-storyboard" aria-label="Editable storyboard">
                {timelineClips.map((clip, index) => {
                  const sourceMatch = [...state.project?.candidates ?? [], ...state.searchResults]
                    .find((match) => match.id === clip.segment_id || match.asset_id === clip.asset_id);
                  const thumb = resolveVideoResourceUrl(apiBase, sourceMatch?.thumbnail_url);
                  return (
                    <article className="video-clip-card" key={clip.id}>
                      <div className="video-clip-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
                      <div className="video-clip-art">
                        {thumb ? <img src={thumb} alt="" /> : <span>{clip.kind?.toUpperCase() ?? timelineTrack?.type.toUpperCase()}</span>}
                      </div>
                      <div className="video-clip-copy">
                        <strong>{sourceMatch?.title ?? sourceMatch?.filename ?? clip.provenance.reason ?? "Grounded clip"}</strong>
                        <span>{clip.provenance.reason}</span>
                        <small>
                          source {typeof clip.source_in_ms === "number" ? formatMilliseconds(clip.source_in_ms) : "still"}
                          {typeof clip.source_out_ms === "number" ? ` → ${formatMilliseconds(clip.source_out_ms)}` : ""}
                          {` · timeline ${formatMilliseconds(clip.timeline_start_ms)} · ${formatMilliseconds(clip.timeline_duration_ms)}`}
                        </small>
                      </div>
                      <div className="video-clip-actions" aria-label={`Edit clip ${index + 1}`}>
                        <button type="button" aria-label={`Move clip ${index + 1} earlier`} disabled={index === 0 || isWriting} onClick={() => void applyTimelineOperations([{ op: "move_clip", clip_id: clip.id, to_index: index - 1 }])}>←</button>
                        <button type="button" aria-label={`Move clip ${index + 1} later`} disabled={index === timelineClips.length - 1 || isWriting} onClick={() => void applyTimelineOperations([{ op: "move_clip", clip_id: clip.id, to_index: index + 1 }])}>→</button>
                        <button type="button" aria-label={`Shorten clip ${index + 1}`} disabled={isWriting} onClick={() => {
                          const operation = clipOperationForTrim(clip, "out", -500);
                          if (operation) void applyTimelineOperations([operation]);
                        }}>-0.5s</button>
                        <button type="button" aria-label={`Extend clip ${index + 1}`} disabled={isWriting} onClick={() => {
                          const operation = clipOperationForTrim(clip, "out", 500);
                          if (operation) void applyTimelineOperations([operation]);
                        }}>+0.5s</button>
                        <span className="meta-pill" title="Preview 0.3 renders deterministic hard cuts; additional transitions remain fail-closed.">
                          Cut
                        </span>
                        {selectedMatch && selectedMatch.id !== clip.segment_id && selectedMatch.asset_id !== clip.asset_id ? (
                          <button type="button" disabled={isWriting} onClick={() => void applyTimelineOperations([{ op: "replace_clip", clip_id: clip.id, match_id: selectedMatch.id }])}>Replace with selected</button>
                        ) : null}
                        <button type="button" className="danger-text" disabled={isWriting || timelineClips.length <= 1} onClick={() => void applyTimelineOperations([{ op: "delete_clip", clip_id: clip.id }])}>Delete</button>
                      </div>
                    </article>
                  );
                })}
              </div>

              <div className="video-command-panel">
                <div>
                  <p className="eyebrow">Conversational edit</p>
                  <h4>Review the typed diff before saving.</h4>
                </div>
                <textarea
                  aria-label="Conversational timeline instruction"
                  value={commandText}
                  onChange={(event) => {
                    setCommandText(event.target.value);
                    setPendingInstruction(null);
                  }}
                  placeholder="For example: 第二个镜头缩短1秒，最后一张图延长2秒，改成9:16竖屏"
                />
                <button type="button" className="secondary-button" onClick={() => void prepareInstruction()} disabled={!commandText.trim() || isWriting}>
                  {isWriting ? "Reviewing…" : "Review operations"}
                </button>
                {pendingInstruction ? (
                  <div className="video-command-diff" role="status">
                    <strong>{pendingInstruction.operations.length > 0 ? `${pendingInstruction.mode === "server" ? "Server" : "Local fallback"} typed diff` : "No safe operation found"}</strong>
                    <ul>
                      {pendingInstruction.summaries.map((summary) => <li key={summary}>{summary}</li>)}
                    </ul>
                    {pendingInstruction.unrecognized.length > 0 ? (
                      <p>Needs a clearer instruction: {pendingInstruction.unrecognized.join(" / ")}</p>
                    ) : null}
                    <div className="action-row">
                      <button type="button" className="primary-button compact-button" onClick={() => void confirmInstruction()} disabled={pendingInstruction.operations.length === 0 || isWriting}>
                        Save as revision {state.timeline.revision + 1}
                      </button>
                      <button type="button" className="secondary-button compact-button" onClick={() => setPendingInstruction(null)}>Discard</button>
                    </div>
                  </div>
                ) : null}
              </div>

              {state.timelineDiff.length > 0 ? (
                <details className="video-revision-diff">
                  <summary>Revision {state.timeline.revision} saved · review server diff</summary>
                  <ul>{state.timelineDiff.map((item, index) => <li key={`${item.op}-${item.clip_id ?? index}`}><strong>{item.op}</strong> {item.summary}</li>)}</ul>
                </details>
              ) : null}
            </>
          )}
          {state.timelineError ? <p className="video-inline-error" role="alert">{state.timelineError}</p> : null}
        </section>
        ) : null}

        {expandedStep === "preview" ? (
        <section
          className="video-panel video-step-panel"
          id="video-step-preview-panel"
          aria-labelledby="video-render-title"
        >
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">05 · Preview</p>
              <h3 id="video-render-title">Validate and render a bounded preview.</h3>
            </div>
            <div className="action-row">
              <button type="button" className="secondary-button compact-button" onClick={() => void handleValidate()} disabled={!state.timeline || state.validationPhase === "loading" || isWriting}>
                {state.validationPhase === "loading" ? "Validating…" : "Validate timeline"}
              </button>
              <button
                type="button"
                className="secondary-button compact-button"
                onClick={() => void handleRender("preview")}
                disabled={!canPreviewRender || !state.timeline || renderActive || isWriting}
                aria-describedby={!canPreviewRender ? "video-preview-requirements" : undefined}
                title={canPreviewRender ? "Render this immutable revision" : "FFmpeg, ffprobe, a verified encoder, preview-low, and app-managed preview storage are required"}
              >
                Render 720p preview
              </button>
              <span className="meta-pill" title="Final export requires a one-time Electron output grant that is not issued in this release.">
                1080p export · output grant required
              </span>
            </div>
          </div>

          {!canPreviewRender ? (
            <p className="video-inline-warning" id="video-preview-requirements" role="note">
              Preview requires MemoLens Desktop, the local service, FFmpeg, ffprobe, a verified encoder, and app-managed preview storage.
            </p>
          ) : null}

          {state.validation ? (
            <div className={`video-validation-card${state.validation.valid ? " valid" : " invalid"}`}>
              <strong>{state.validation.valid ? "Timeline is renderable" : `${state.validation.errors.length} validation error(s)`}</strong>
              {state.validation.errors.length > 0 ? (
                <ul>{state.validation.errors.map((issue) => <li key={`${issue.field}-${issue.code}`}><code>{issue.field ?? issue.code}</code> {issue.message}</li>)}</ul>
              ) : <span>All source ranges, durations, supported edits, and output settings passed render validation.</span>}
              {state.validation.warnings.length > 0 ? <p>{state.validation.warnings.map((warning) => warning.message).join(" ")}</p> : null}
            </div>
          ) : null}
          {state.validationError ? <p className="video-inline-error" role="alert">{state.validationError}</p> : null}

          {state.renderJob ? (
            <div className="video-render-layout">
              <div className="video-render-player">
                {renderCompleted && renderMediaUrl ? (
                  <video key={renderMediaUrl} controls preload="metadata" src={renderMediaUrl}>
                    Your browser cannot play the rendered MP4.
                  </video>
                ) : (
                  <div className="video-render-placeholder" role="status">
                    <span className={renderActive ? "render-orbit" : ""} aria-hidden="true" />
                    <strong>{formatJobStage(state.renderJob.stage)}</strong>
                    <span>{state.renderJob.status} · revision {state.renderJob.timeline_revision}</span>
                  </div>
                )}
              </div>
              <div className="video-render-status" aria-live="polite">
                <div>
                  <span>{state.renderJob.kind === "export" ? "Final export" : "Preview"}</span>
                  <strong>{state.renderJob.progress}%</strong>
                </div>
                <div className="progress-bar" role="progressbar" aria-label="Video render progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={state.renderJob.progress}>
                  <div className="progress-bar-fill" style={{ width: `${state.renderJob.progress}%` }} />
                </div>
                <p>{state.renderJob.message ?? (renderCompleted ? "Artifact verified and ready." : `FFmpeg stage: ${formatJobStage(state.renderJob.stage)}`)}</p>
                <div className="action-row">
                  {renderCancellable ? <button type="button" className="secondary-button compact-button danger-button" onClick={() => void handleCancelRender()}>Cancel render</button> : null}
                  {["failed", "cancelled", "interrupted"].includes(state.renderJob.status) ? <button type="button" className="secondary-button compact-button" onClick={() => void handleRender(state.renderJob!.kind)}>Retry same revision</button> : null}
                </div>
                {state.renderJob.error ? <p className="video-inline-error">{state.renderJob.error.message}</p> : null}
              </div>
            </div>
          ) : (
            <div className="video-state-card">
              <strong>No render has started.</strong>
              <span>Preview uses a bounded 720p H.264/AAC profile. Final 1080p export stays disabled until Electron can issue a one-time output grant.</span>
            </div>
          )}
          {state.renderError ? <p className="video-inline-error" role="alert">{state.renderError}</p> : null}
        </section>
        ) : null}

        {expandedStep === "save" ? (
        <section
          className="video-panel video-step-panel"
          id="video-step-save-panel"
          aria-labelledby="video-save-title"
        >
          <div className="video-panel-head">
            <div>
              <p className="eyebrow">06 · Save</p>
              <h3 id="video-save-title">Keep a verified copy</h3>
              <p className="video-muted">MemoLens verifies the preview digest and byte size before it writes a new MP4. Existing files are never overwritten.</p>
            </div>
            {state.timeline ? <span className="meta-pill">revision {state.timeline.revision}</span> : null}
          </div>

          {renderCompleted && state.renderJob ? (
            <div className="video-render-layout">
              <div className="video-render-player">
                {renderMediaUrl ? (
                  <video key={renderMediaUrl} controls preload="metadata" src={renderMediaUrl}>
                    Your browser cannot play the rendered MP4.
                  </video>
                ) : (
                  <div className="video-render-placeholder">
                    <strong>Verified preview ready</strong>
                    <span>{state.renderJob.filename ?? state.renderJob.output?.filename ?? "MemoLens preview"}</span>
                  </div>
                )}
              </div>
              <div className="video-render-status" aria-live="polite">
                <div>
                  <span>Preview artifact</span>
                  <strong>{artifactSaved ? "Saved" : "Ready"}</strong>
                </div>
                <p>{state.renderJob.message ?? "The immutable preview revision passed rendering."}</p>
                <button
                  type="button"
                  className="primary-button"
                  onClick={() => void handleSaveArtifact()}
                  disabled={isSavingArtifact || isWriting || !canVerifiedPreviewSaveAs}
                >
                  {isSavingArtifact
                    ? "Verifying and saving…"
                    : canVerifiedPreviewSaveAs
                      ? artifactSaved ? "Save another verified copy…" : "Verify and save as…"
                      : desktopRuntime ? "Verified Save As unavailable" : "Open Desktop to save"}
                </button>
                {state.saveMessage ? <p className="video-inline-note" role="status">{state.saveMessage}</p> : null}
              </div>
            </div>
          ) : (
            <div className="video-state-card">
              <strong>No verified preview is ready.</strong>
              <span>Return to Preview and finish a successful render before saving.</span>
            </div>
          )}
        </section>
        ) : null}
      </div>
    </section>
  );
}

export default VideoWorkbench;
