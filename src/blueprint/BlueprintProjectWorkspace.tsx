import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";

import { BLUEPRINT_DECISION_UNITS, type BlueprintDecisionUnitName } from "./authorityTypes";
import {
  fetchCoverageWorkspace,
  materializeCoverageBaseline,
} from "./coverageApi";
import { isCoverageWorkspaceProjectionForBlueprint } from "./coverageModel";
import type {
  CanonicalCoveragePlan,
  CoverageEvidenceManifestItem,
  CoveragePlanWorkspace,
} from "./coverageTypes";
import {
  applyCanonicalTimelineEdit,
  applyCanonicalTimelineStructuralEdit,
  fetchCanonicalTimelineWorkspace,
  materializeCanonicalTimelineFirstCut,
  reconcileCanonicalTimelineFromCoverage,
  restoreCanonicalTimelineRevision,
} from "./timelineApi";
import {
  canonicalTimelinePendingEditKey,
  canonicalTimelineReplacementCandidates,
  stageCanonicalTimelineEdit,
  type CanonicalTimelinePendingEdit,
} from "./timelineEditModel";
import {
  isSameCanonicalTimelineHead,
  isHistoricalTimelineWorkspaceForCurrentHead,
  isHistoricalTimelineSelectionForCurrentLedger,
  isTimelineWorkspaceProjectionForBlueprint,
  timelineCoverageBindingFromWorkspace,
} from "./timelineModel";
import {
  historicalTimelineRevisionNumbers,
  stageCanonicalTimelineRestore,
  type CanonicalTimelinePendingRestore,
} from "./timelineRestoreModel";
import type {
  CanonicalTimelineCoverageBinding,
  CanonicalTimelineEdit,
  CanonicalTimelineFreshnessState,
  CanonicalTimelineStructuralEdit,
  CanonicalTimelineWorkspace,
} from "./timelineTypes";
import {
  fetchBlueprintRevision,
  fetchBlueprintWorkspace,
  restoreBlueprintRevision,
} from "./workspaceApi";
import {
  canApplyBlueprintWorkspaceRequest,
  classifyBlueprintWorkspaceInput,
  compareBlueprintSemantics,
  isSameBlueprintWorkspaceIdentity,
} from "./workspaceModel";
import {
  canonicalExportJobPresentation,
  canonicalExportSourceBindingsSha256,
  isActiveCanonicalExportJobStatus,
  normalizeCanonicalExportPackageBasename,
  normalizeDesktopCanonicalExportApprovalResult,
  observeCanonicalExportPoll,
} from "./exportModel";
import type {
  CanonicalExportJobStatus,
  DesktopCanonicalExportApprovalResult,
} from "./exportTypes";
import type {
  BlueprintCoverageState,
  BlueprintCurrentRevision,
  BlueprintProjectWorkspace as BlueprintProjectWorkspaceModel,
  BlueprintSectionDiff,
  BlueprintSemanticSection,
} from "./workspaceTypes";
import { VideoApiError } from "../video/api/transport";
import { CanonicalTimelinePreview } from "./CanonicalTimelinePreview";

export interface BlueprintProjectWorkspaceProps {
  apiBase: string;
  dbPath?: string | null;
  initialWorkspace: BlueprintProjectWorkspaceModel;
  canWrite: boolean;
  onWorkspaceChange?: (workspace: BlueprintProjectWorkspaceModel) => void;
}

interface RevisionComparison {
  before: BlueprintCurrentRevision;
  after: BlueprintCurrentRevision;
  sections: BlueprintSectionDiff[];
}

interface WorkspaceRequest {
  controller: AbortController;
  generation: number;
  capturedWorkspace: BlueprintProjectWorkspaceModel;
}

interface TimelineClipEditDraft {
  sourceInMs?: string;
  sourceOutMs?: string;
  durationMs?: string;
  replacementAssignmentId?: string;
  sourceSplitMs?: string;
}

class StaleWorkspaceRequestError extends Error {}

const CANONICAL_EXPORT_POLL_INTERVAL_MS = 1_000;
const CANONICAL_EXPORT_POLL_MAX_ATTEMPTS = 300;
const CANONICAL_EXPORT_UNKNOWN_GRACE_ATTEMPTS = 3;
const TIMELINE_REFRESHABLE_CONFLICT_CODES = new Set([
  "blueprint_head_conflict",
  "coverage_head_conflict",
  "database_identity_changed",
  "timeline_head_conflict",
  "timeline_manual_current_conflict",
  "timeline_restore_binding_mismatch",
  "timeline_restore_source_stale",
  "timeline_restore_target_invalid",
]);

type CanonicalExportPollState =
  | { state: "idle" }
  | { state: "polling"; jobId: string | null }
  | { state: "terminal"; jobId: string; status: CanonicalExportJobStatus }
  | { state: "unknown"; detail: string };

function waitForCanonicalExportPoll(
  signal: AbortSignal,
  delayMs = CANONICAL_EXPORT_POLL_INTERVAL_MS,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("The canonical export poll was cancelled.", "AbortError"));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("The canonical export poll was cancelled.", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

const AUTHORITY_LABELS: Record<BlueprintDecisionUnitName, string> = {
  intent_goal: "Intent & goal",
  intent_stance: "Stance",
  script: "Script",
  creative_direction: "Creative direction",
  output: "Output",
  material_constraints: "Material constraints",
  references: "References",
  techniques: "Techniques",
};

const SECTION_LABELS: Record<BlueprintSemanticSection, string> = {
  intent: "Intent",
  script: "Script",
  direction: "Direction",
  output: "Output",
  constraints: "Constraints",
  material_hints: "Material hints",
  reference_refs: "References",
  technique_refs: "Technique cards",
  bindings: "Pinned context",
  assumptions: "Assumptions",
  missing_evidence: "Evidence gaps",
  open_decisions: "Open decisions",
};

const COVERAGE_PRESENTATION: Record<BlueprintCoverageState, { label: string; detail: string }> = {
  missing: {
    label: "Not materialized",
    detail: "The Blueprint is readable, but no footage plan has been fixed yet.",
  },
  current: {
    label: "Current",
    detail: "This plan is pinned to the exact Blueprint and current media evidence.",
  },
  stale_blueprint: {
    label: "Blueprint changed",
    detail: "The plan belongs to an older Blueprint and must not drive a cut.",
  },
  stale_evidence: {
    label: "Media evidence changed",
    detail: "At least one selected material proof must be checked again.",
  },
};

const TIMELINE_PRESENTATION: Record<
  CanonicalTimelineFreshnessState,
  { label: string; detail: string }
> = {
  missing: {
    label: "Not materialized",
    detail: "No canonical first-cut draft exists for this project.",
  },
  current: {
    label: "Current draft",
    detail: "This hard-cut draft is pinned to the exact Blueprint, Coverage, and source identities.",
  },
  stale_blueprint: {
    label: "Blueprint changed",
    detail: "This draft belongs to an older Blueprint and is preserved without being presented as current.",
  },
  stale_coverage: {
    label: "Coverage changed",
    detail: "This draft belongs to an older Coverage head and is preserved without being overwritten.",
  },
  stale_source_binding: {
    label: "Source binding changed",
    detail: "At least one fixed execution source no longer matches the materialized draft.",
  },
};

function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatDuration(value: number | null): string {
  if (value === null) return "Not decided";
  const totalSeconds = Math.round(value / 100) / 10;
  return `${totalSeconds}s`;
}

function displayValue(value: string | null): string {
  return value?.trim() || "Not decided";
}

function humanizeCoverageCode(value: string): string {
  return value.replaceAll("_", " ");
}

function coverageEvidence(
  plan: CanonicalCoveragePlan,
  evidenceRef: string,
): CoverageEvidenceManifestItem | null {
  return plan.evidence_manifest.find((item) => item.evidence_ref === evidenceRef) ?? null;
}

function safeUiError(error: unknown): string {
  if (error instanceof DOMException && error.name === "TimeoutError") {
    return "The local MemoLens service did not respond in time.";
  }
  if (error instanceof VideoApiError && error.message.trim()) {
    return error.message.slice(0, 500);
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message.slice(0, 500);
  }
  return "The canonical Blueprint workspace could not complete this request.";
}

function previousRevision(workspace: BlueprintProjectWorkspaceModel): number {
  const current = workspace.current_blueprint.revision;
  const historical = workspace.history.blueprint.operations
    .flatMap((operation) => [operation.result_revision, operation.restore_from?.revision ?? 0])
    .filter((revision) => revision > 0 && revision < current);
  return historical.length > 0 ? Math.max(...historical) : Math.max(1, current - 1);
}

function Digest({ children, label }: { children: string; label: string }) {
  return (
    <code className="blueprint-digest" aria-label={`${label}: ${children}`} title={children}>
      {children}
    </code>
  );
}

function EmptyState({ children }: { children: string }) {
  return <p className="blueprint-empty">{children}</p>;
}

function RevisionComparisonView({
  comparison,
  label,
}: {
  comparison: RevisionComparison;
  label: string;
}) {
  const changedSections = comparison.sections.filter((section) => section.changed).length;
  return (
    <div className="blueprint-comparison" aria-label={label} aria-live="polite">
      <header>
        <div>
          <strong>Revision {comparison.before.revision}</strong>
          <Digest label={`Revision ${comparison.before.revision} content SHA-256`}>
            {comparison.before.content_sha256}
          </Digest>
        </div>
        <span aria-hidden="true">→</span>
        <div>
          <strong>Revision {comparison.after.revision}</strong>
          <Digest label={`Revision ${comparison.after.revision} content SHA-256`}>
            {comparison.after.content_sha256}
          </Digest>
        </div>
      </header>
      <p>{changedSections} of {comparison.sections.length} typed sections changed.</p>
      <ol>
        {comparison.sections.map((section) => (
          <li className={section.changed ? "changed" : "unchanged"} key={section.section}>
            <strong>{SECTION_LABELS[section.section]}</strong>
            <span>{section.changed ? "Changed" : "Unchanged"}</span>
            <div><small>Before</small><p>{section.before_summary}</p></div>
            <div><small>After</small><p>{section.after_summary}</p></div>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function BlueprintProjectWorkspace({
  apiBase,
  dbPath = null,
  initialWorkspace,
  canWrite,
  onWorkspaceChange,
}: BlueprintProjectWorkspaceProps) {
  const [workspace, setWorkspace] = useState(initialWorkspace);
  const [compareFrom, setCompareFrom] = useState(String(previousRevision(initialWorkspace)));
  const [compareTo, setCompareTo] = useState(String(initialWorkspace.current_blueprint.revision));
  const [comparison, setComparison] = useState<RevisionComparison | null>(null);
  const [restoreFrom, setRestoreFrom] = useState(String(previousRevision(initialWorkspace)));
  const [restoreReview, setRestoreReview] = useState<RevisionComparison | null>(null);
  const [isComparing, setIsComparing] = useState(false);
  const [isReviewingRestore, setIsReviewingRestore] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isMaterializingCoverage, setIsMaterializingCoverage] = useState(false);
  const [isMaterializingTimeline, setIsMaterializingTimeline] = useState(false);
  const [isReconcilingTimeline, setIsReconcilingTimeline] = useState(false);
  const [isApplyingTimelineEdit, setIsApplyingTimelineEdit] = useState(false);
  const [isApplyingTimelineStructuralEdit, setIsApplyingTimelineStructuralEdit] = useState(false);
  const [isRestoringTimeline, setIsRestoringTimeline] = useState(false);
  const [isApprovingExport, setIsApprovingExport] = useState(false);
  const [exportResult, setExportResult] = useState<DesktopCanonicalExportApprovalResult | null>(null);
  const [exportPollState, setExportPollState] = useState<CanonicalExportPollState>({ state: "idle" });
  const [coverageWorkspace, setCoverageWorkspace] = useState<CoveragePlanWorkspace | null>(null);
  const [coverageLoadState, setCoverageLoadState] = useState<"idle" | "loading" | "loaded" | "error">("idle");
  const [coverageError, setCoverageError] = useState<string | null>(null);
  const [timelineWorkspace, setTimelineWorkspace] = useState<CanonicalTimelineWorkspace | null>(null);
  const [timelineLoadState, setTimelineLoadState] = useState<"idle" | "loading" | "loaded" | "error">("idle");
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [pendingTimelineEdit, setPendingTimelineEdit] = useState<CanonicalTimelinePendingEdit | null>(null);
  const [historicalTimelineRevision, setHistoricalTimelineRevision] = useState("");
  const [historicalTimelineWorkspace, setHistoricalTimelineWorkspace] = useState<CanonicalTimelineWorkspace | null>(null);
  const [historicalTimelineLoadState, setHistoricalTimelineLoadState] = useState<"idle" | "loading" | "loaded" | "error">("idle");
  const [historicalTimelineError, setHistoricalTimelineError] = useState<string | null>(null);
  const [pendingTimelineRestore, setPendingTimelineRestore] = useState<CanonicalTimelinePendingRestore | null>(null);
  const [timelineClipEditDrafts, setTimelineClipEditDrafts] = useState<Record<string, TimelineClipEditDraft>>({});
  const [expandedTimelineClipIds, setExpandedTimelineClipIds] = useState<Set<string>>(() => new Set());
  const [headStatus, setHeadStatus] = useState<"validated" | "refreshing" | "unknown">("validated");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const currentWorkspaceRef = useRef(initialWorkspace);
  const incomingWorkspaceRef = useRef(initialWorkspace);
  const lastEmittedWorkspaceRef = useRef<BlueprintProjectWorkspaceModel | null>(null);
  const suppressedInitialWorkspaceEffectRef = useRef<BlueprintProjectWorkspaceModel | null>(null);
  const pendingTimelineEditRef = useRef<CanonicalTimelinePendingEdit | null>(null);
  const pendingTimelineRestoreRef = useRef<CanonicalTimelinePendingRestore | null>(null);
  const isApplyingTimelineEditRef = useRef(false);
  const isRestoringTimelineRef = useRef(false);
  const timelineEditPreconditionIdentityRef = useRef<string | null>(null);
  const onWorkspaceChangeRef = useRef(onWorkspaceChange);
  const requestGenerationRef = useRef(0);
  const activeRequestRef = useRef<AbortController | null>(null);
  const coverageRequestGenerationRef = useRef(0);
  const coverageRequestRef = useRef<AbortController | null>(null);
  const timelineRequestGenerationRef = useRef(0);
  const timelineRequestRef = useRef<AbortController | null>(null);
  const historicalTimelineRequestGenerationRef = useRef(0);
  const historicalTimelineRequestRef = useRef<AbortController | null>(null);
  const exportPollGenerationRef = useRef(0);
  const exportPollRequestRef = useRef<AbortController | null>(null);
  const exportPollScopeRef = useRef(
    `${initialWorkspace.database_uuid}:${initialWorkspace.project_id}`,
  );
  onWorkspaceChangeRef.current = onWorkspaceChange;
  pendingTimelineEditRef.current = pendingTimelineEdit;
  pendingTimelineRestoreRef.current = pendingTimelineRestore;
  isApplyingTimelineEditRef.current = isApplyingTimelineEdit;
  isRestoringTimelineRef.current = isRestoringTimeline;

  const incomingDisposition = classifyBlueprintWorkspaceInput({
    previousIncoming: incomingWorkspaceRef.current,
    incoming: initialWorkspace,
    lastEmitted: lastEmittedWorkspaceRef.current,
  });
  if (incomingDisposition !== "unchanged") {
    incomingWorkspaceRef.current = initialWorkspace;
    if (incomingDisposition === "self_echo") {
      lastEmittedWorkspaceRef.current = null;
      suppressedInitialWorkspaceEffectRef.current = initialWorkspace;
    } else {
      lastEmittedWorkspaceRef.current = null;
      currentWorkspaceRef.current = initialWorkspace;
      requestGenerationRef.current += 1;
    }
  }

  useLayoutEffect(() => {
    if (suppressedInitialWorkspaceEffectRef.current === initialWorkspace) {
      suppressedInitialWorkspaceEffectRef.current = null;
      return;
    }
    const nextExportPollScope = `${initialWorkspace.database_uuid}:${initialWorkspace.project_id}`;
    if (exportPollScopeRef.current !== nextExportPollScope) {
      exportPollScopeRef.current = nextExportPollScope;
      exportPollGenerationRef.current += 1;
      exportPollRequestRef.current?.abort();
      exportPollRequestRef.current = null;
      setExportPollState({ state: "idle" });
    }
    activeRequestRef.current?.abort();
    activeRequestRef.current = null;
    currentWorkspaceRef.current = initialWorkspace;
    setWorkspace(initialWorkspace);
    setCompareTo(String(initialWorkspace.current_blueprint.revision));
    setCompareFrom(String(previousRevision(initialWorkspace)));
    setRestoreFrom(String(previousRevision(initialWorkspace)));
    setComparison(null);
    setRestoreReview(null);
    setHeadStatus("validated");
    setIsComparing(false);
    setIsReviewingRestore(false);
    setIsRestoring(false);
    setIsRefreshing(false);
    setIsMaterializingCoverage(false);
    setIsMaterializingTimeline(false);
    setIsReconcilingTimeline(false);
    setIsApplyingTimelineEdit(false);
    setIsApplyingTimelineStructuralEdit(false);
    setIsRestoringTimeline(false);
    setIsApprovingExport(false);
    setExportResult(null);
    setCoverageWorkspace(null);
    setCoverageLoadState("idle");
    setCoverageError(null);
    setTimelineWorkspace(null);
    setTimelineLoadState("idle");
    setTimelineError(null);
    setPendingTimelineEdit(null);
    setHistoricalTimelineRevision("");
    setHistoricalTimelineWorkspace(null);
    setHistoricalTimelineLoadState("idle");
    setHistoricalTimelineError(null);
    setPendingTimelineRestore(null);
    setTimelineClipEditDrafts({});
    setExpandedTimelineClipIds(new Set());
    setMessage(null);
    setError(null);
  }, [initialWorkspace]);

  useEffect(() => () => {
    requestGenerationRef.current += 1;
    activeRequestRef.current?.abort();
    activeRequestRef.current = null;
    coverageRequestGenerationRef.current += 1;
    coverageRequestRef.current?.abort();
    coverageRequestRef.current = null;
    timelineRequestGenerationRef.current += 1;
    timelineRequestRef.current?.abort();
    timelineRequestRef.current = null;
    historicalTimelineRequestGenerationRef.current += 1;
    historicalTimelineRequestRef.current?.abort();
    historicalTimelineRequestRef.current = null;
    exportPollGenerationRef.current += 1;
    exportPollRequestRef.current?.abort();
    exportPollRequestRef.current = null;
  }, []);

  const current = workspace.current_blueprint;
  const blueprint = current.blueprint;
  const semantic = blueprint.semantic;
  const authority = current.authority_projection;
  const coverage = workspace.coverage;
  const coveragePresentation = COVERAGE_PRESENTATION[coverage.state];
  const confirmedCount = authority.confirmed_decision_unit_count;
  const verifiedEvidence = blueprint.evidence_manifest.filter((item) => item.status === "verified").length;
  const isBusy = isComparing
    || isReviewingRestore
    || isRestoring
    || isRefreshing
    || isMaterializingCoverage
    || isMaterializingTimeline
    || isReconcilingTimeline
    || isApplyingTimelineEdit
    || isApplyingTimelineStructuralEdit
    || isRestoringTimeline
    || isApprovingExport;
  const canRestore = canWrite
    && headStatus === "validated"
    && pendingTimelineEdit === null
    && pendingTimelineRestore === null;
  const canMaterializeCoverage = canWrite
    && headStatus === "validated"
    && pendingTimelineEdit === null
    && pendingTimelineRestore === null
    && workspace.capabilities.coverage_plan_materialization
    && coverage.state !== "current";
  const coveragePlan = coverageWorkspace?.coverage_plan ?? null;
  const coveragePlanUsesDisplayedScript = coveragePlan !== null
    && coveragePlan.blueprint_binding.revision === current.revision
    && coveragePlan.blueprint_binding.content_sha256 === current.content_sha256
    && coveragePlan.blueprint_binding.semantic_sha256 === current.semantic_sha256;
  const timelineSummary = workspace.timeline;
  const timelinePresentation = TIMELINE_PRESENTATION[timelineSummary.state];
  const canonicalTimeline = timelineWorkspace?.timeline ?? null;
  let exactCoverageForTimeline: CanonicalTimelineCoverageBinding | null = null;
  try {
    if (coverageWorkspace !== null) {
      exactCoverageForTimeline = timelineCoverageBindingFromWorkspace(coverageWorkspace);
    }
  } catch {
    exactCoverageForTimeline = null;
  }
  const canMaterializeTimeline = canWrite
    && headStatus === "validated"
    && pendingTimelineEdit === null
    && pendingTimelineRestore === null
    && workspace.capabilities.timeline_materialization
    && timelineSummary.head === null
    && exactCoverageForTimeline !== null
    && coveragePlan !== null
    && coveragePlan.beats.every((beat) => (
      beat.gap === null && beat.selected_assignments.length === 1
    ));
  const canReconcileTimeline = canWrite
    && headStatus === "validated"
    && pendingTimelineEdit === null
    && pendingTimelineRestore === null
    && timelineSummary.state !== "missing"
    && timelineSummary.state !== "current"
    && timelineSummary.head !== null
    && workspace.capabilities.timeline_reconciliation
    && exactCoverageForTimeline !== null
    && coveragePlan !== null
    && coveragePlan.beats.every((beat) => (
      beat.gap === null && beat.selected_assignments.length === 1
    ));
  const canEditTimeline = canWrite
    && headStatus === "validated"
    && workspace.capabilities.timeline_mutation
    && timelineSummary.state === "current"
    && timelineWorkspace !== null
    && timelineWorkspace.head !== null
    && timelineWorkspace.timeline !== null
    && timelineWorkspace.selected_revision?.is_head === true
    && timelineWorkspace.freshness.state === "current"
    && coverageWorkspace !== null
    && coveragePlan !== null
    && exactCoverageForTimeline !== null
    && isCoverageWorkspaceProjectionForBlueprint(workspace, coverageWorkspace)
    && isTimelineWorkspaceProjectionForBlueprint(workspace, timelineWorkspace)
    && JSON.stringify(timelineWorkspace.timeline.coverage_binding)
      === JSON.stringify(exactCoverageForTimeline);
  const canStageTimelineRestore = canEditTimeline
    && pendingTimelineEdit === null
    && pendingTimelineRestore === null
    && timelineWorkspace !== null
    && historicalTimelineWorkspace !== null
    && isHistoricalTimelineWorkspaceForCurrentHead(
      timelineWorkspace,
      historicalTimelineWorkspace,
    );
  const canSubmitPendingTimelineEdit = canWrite
    && pendingTimelineEdit !== null
    && pendingTimelineRestore === null
    && pendingTimelineEdit.database_uuid === workspace.database_uuid
    && pendingTimelineEdit.project_id === workspace.project_id;
  const canSubmitPendingTimelineRestore = canWrite
    && pendingTimelineRestore !== null
    && pendingTimelineEdit === null
    && pendingTimelineRestore.database_uuid === workspace.database_uuid
    && pendingTimelineRestore.project_id === workspace.project_id;
  const localExportInProgress = exportResult?.status === "unknown"
    || (exportResult?.status === "submitted"
      && isActiveCanonicalExportJobStatus(exportResult.job.status));
  const localActiveExportJobId = exportResult?.status === "submitted"
    && isActiveCanonicalExportJobStatus(exportResult.job.status)
    ? exportResult.job.jobId
    : null;
  const authoritativeActiveExportJobId = workspace.canonical_export.latest_job !== null
    && isActiveCanonicalExportJobStatus(workspace.canonical_export.latest_job.status)
    ? workspace.canonical_export.latest_job.job_id
    : null;
  const exportPollExpectedJobId = localActiveExportJobId ?? authoritativeActiveExportJobId;
  const exportPollTrigger = exportPollExpectedJobId !== null
    ? `job:${exportPollExpectedJobId}`
    : exportResult?.status === "unknown"
      ? `unknown:${workspace.database_uuid}:${workspace.project_id}`
      : null;
  const latestCanonicalExportPresentation = workspace.canonical_export.latest_job === null
    ? null
    : canonicalExportJobPresentation(workspace.canonical_export.latest_job.status);
  const localCanonicalExportPresentation = exportResult?.status === "submitted"
    ? canonicalExportJobPresentation(exportResult.job.status)
    : null;
  const hasCanonicalExportBridge = typeof window.memolensDesktop
    ?.approveAndExportCanonicalTimeline === "function";
  const hasExactTimelineForExport = timelineWorkspace !== null
    && timelineWorkspace.head !== null
    && timelineWorkspace.selected_revision?.is_head === true
    && timelineWorkspace.source_bindings.length > 0
    && isTimelineWorkspaceProjectionForBlueprint(workspace, timelineWorkspace);
  const canApproveCanonicalExport = canWrite
    && headStatus === "validated"
    && timelineSummary.state === "current"
    && hasExactTimelineForExport
    && workspace.capabilities.canonical_export
    && workspace.canonical_export.state === "available"
    && !localExportInProgress
    && pendingTimelineEdit === null
    && pendingTimelineRestore === null
    && hasCanonicalExportBridge;

  const timelineEditPreconditionIdentity = JSON.stringify({
    database_uuid: workspace.database_uuid,
    project_id: workspace.project_id,
    blueprint: {
      revision: current.revision,
      content_sha256: current.content_sha256,
      semantic_sha256: current.semantic_sha256,
      operation_id: current.operation_id,
    },
    coverage_summary: {
      state: coverage.state,
      head: coverage.head,
      blueprint_binding: coverage.blueprint_binding,
    },
    timeline_summary: {
      state: timelineSummary.state,
      head: timelineSummary.head,
      blueprint_binding: timelineSummary.blueprint_binding,
      coverage_binding: timelineSummary.coverage_binding,
    },
    coverage_resource: coverageWorkspace === null ? null : {
      database_uuid: coverageWorkspace.database_uuid,
      head: coverageWorkspace.head,
      selected_revision: coverageWorkspace.selected_revision,
      freshness: coverageWorkspace.freshness,
    },
    timeline_resource: timelineWorkspace === null ? null : {
      database_uuid: timelineWorkspace.database_uuid,
      head: timelineWorkspace.head,
      selected_revision: timelineWorkspace.selected_revision,
      freshness: timelineWorkspace.freshness,
      source_bindings: timelineWorkspace.source_bindings,
    },
  });

  useLayoutEffect(() => {
    const previousIdentity = timelineEditPreconditionIdentityRef.current;
    timelineEditPreconditionIdentityRef.current = timelineEditPreconditionIdentity;
    const editSaveInFlight = isApplyingTimelineEditRef.current;
    const restoreSaveInFlight = isRestoringTimelineRef.current;
    if (
      previousIdentity !== null
      && previousIdentity !== timelineEditPreconditionIdentity
      && pendingTimelineEditRef.current !== null
      && !editSaveInFlight
    ) {
      setMessage(null);
      setError(
        "The pending Timeline edit was discarded because its Blueprint, Coverage, Timeline, or fixed source preconditions changed.",
      );
      setPendingTimelineEdit(null);
    }
    if (
      previousIdentity !== null
      && previousIdentity !== timelineEditPreconditionIdentity
      && pendingTimelineRestoreRef.current !== null
      && !restoreSaveInFlight
    ) {
      setMessage(null);
      setError(
        "The pending Timeline restore was discarded because its current head or upstream preconditions changed.",
      );
      setPendingTimelineRestore(null);
    }
    if (editSaveInFlight || restoreSaveInFlight) return;
    historicalTimelineRequestGenerationRef.current += 1;
    historicalTimelineRequestRef.current?.abort();
    historicalTimelineRequestRef.current = null;
    setHistoricalTimelineWorkspace(null);
    setHistoricalTimelineLoadState("idle");
    setHistoricalTimelineError(null);
    setHistoricalTimelineRevision(
      timelineWorkspace?.head !== null && timelineWorkspace?.head !== undefined
        && timelineWorkspace.head.revision > 1
        ? String(timelineWorkspace.head.revision - 1)
        : "",
    );
    setTimelineClipEditDrafts({});
  }, [timelineEditPreconditionIdentity]);

  useLayoutEffect(() => {
    const firstClipId = timelineWorkspace?.timeline?.tracks[0]?.clips[0]?.clip_id;
    setExpandedTimelineClipIds(firstClipId ? new Set([firstClipId]) : new Set());
  }, [
    timelineWorkspace?.database_uuid,
    timelineWorkspace?.project_id,
    timelineWorkspace?.selected_revision?.revision,
    timelineWorkspace?.selected_revision?.revision_sha256,
  ]);

  useEffect(() => {
    if (exportPollTrigger === null) return undefined;

    const controller = new AbortController();
    const generation = exportPollGenerationRef.current + 1;
    const expectedJobId = exportPollExpectedJobId;
    const initialLatestJobId = workspace.canonical_export.latest_job?.job_id ?? null;
    const startedAt = Date.now();
    exportPollGenerationRef.current = generation;
    exportPollRequestRef.current?.abort();
    exportPollRequestRef.current = controller;
    setExportPollState({ state: "polling", jobId: expectedJobId });

    void (async () => {
      let attempts = 0;
      let lastFailure: unknown = null;
      while (
        attempts < CANONICAL_EXPORT_POLL_MAX_ATTEMPTS
        && Date.now() - startedAt < (
          CANONICAL_EXPORT_POLL_INTERVAL_MS * CANONICAL_EXPORT_POLL_MAX_ATTEMPTS
        )
      ) {
        if (attempts > 0) {
          try {
            await waitForCanonicalExportPoll(controller.signal);
          } catch {
            return;
          }
        }
        attempts += 1;
        const capturedWorkspace = currentWorkspaceRef.current;
        try {
          const next = await fetchBlueprintWorkspace({
            apiBase,
            projectId: capturedWorkspace.project_id,
            dbPath,
            signal: controller.signal,
          });
          if (
            controller.signal.aborted
            || generation !== exportPollGenerationRef.current
          ) return;
          if (!isSameBlueprintWorkspaceIdentity(capturedWorkspace, next)) {
            setExportPollState({
              state: "unknown",
              detail: "Automatic export verification stopped because the media database or project identity changed.",
            });
            return;
          }

          const latestCurrent = currentWorkspaceRef.current;
          const candidateRevision = next.current_blueprint.revision;
          const currentRevision = latestCurrent.current_blueprint.revision;
          if (
            !isSameBlueprintWorkspaceIdentity(capturedWorkspace, latestCurrent)
            || candidateRevision < currentRevision
            || (
              candidateRevision === currentRevision
              && next.current_blueprint.content_sha256
                !== latestCurrent.current_blueprint.content_sha256
            )
          ) {
            // A project switch or a newer canonical write won the race. Never
            // overwrite it with this response; the next iteration recaptures.
            continue;
          }

          currentWorkspaceRef.current = next;
          setWorkspace(next);
          setCompareTo(String(next.current_blueprint.revision));

          const observation = observeCanonicalExportPoll({
            workspace: next.canonical_export,
            expectedJobId,
            initialLatestJobId,
            unknownGraceReached: attempts >= CANONICAL_EXPORT_UNKNOWN_GRACE_ATTEMPTS,
          });
          if (observation.state === "active" || observation.state === "terminal") {
            const latestJob = observation.job;
            setExportResult(null);
            if (observation.state === "active") {
              // For an initially unknown IPC outcome, switch to the now
              // authoritative job identity so the effect can remain exact.
              if (expectedJobId === null) return;
              continue;
            }
            setExportPollState({
              state: "terminal",
              jobId: latestJob.job_id,
              status: latestJob.status,
            });
            // Active progress is local renderer state. Notify the parent only
            // once the canonical terminal is observed so one-second polling
            // cannot repeatedly reset or abort unrelated workspace actions.
            onWorkspaceChangeRef.current?.(next);
            return;
          }
          if (observation.state === "unknown" && observation.reason === "no_new_job") {
            // The authoritative workspace is reachable and reports no new or
            // active job after a short race window. Clear the renderer-local
            // uncertainty without inventing a successful export.
            setExportResult(null);
            setExportPollState({
              state: "unknown",
              detail: "No new canonical export job became observable. The canonical workspace reports no active export; no successful package is claimed.",
            });
            return;
          }
          if (observation.state === "unknown") {
            setExportPollState({
              state: "unknown",
              detail: `Canonical job ${expectedJobId} is no longer reported as active, but its exact terminal could not be matched. No successful package is claimed.`,
            });
            return;
          }
          lastFailure = null;
        } catch (cause) {
          if (
            controller.signal.aborted
            || generation !== exportPollGenerationRef.current
          ) return;
          lastFailure = cause;
        }
      }

      if (
        controller.signal.aborted
        || generation !== exportPollGenerationRef.current
      ) return;
      setExportPollState({
        state: "unknown",
        detail: `Automatic export verification reached its bounded limit. Use Refresh canonical head to retry verification.${lastFailure === null ? "" : ` Last refresh error: ${safeUiError(lastFailure)}`}`,
      });
    })().finally(() => {
      if (exportPollRequestRef.current === controller) {
        exportPollRequestRef.current = null;
      }
    });

    return () => {
      controller.abort();
      if (exportPollRequestRef.current === controller) {
        exportPollRequestRef.current = null;
      }
    };
  }, [
    apiBase,
    dbPath,
    exportPollExpectedJobId,
    exportPollTrigger,
    workspace.canonical_export.latest_job?.job_id,
    workspace.database_uuid,
    workspace.project_id,
  ]);

  useEffect(() => {
    coverageRequestRef.current?.abort();
    const controller = new AbortController();
    const generation = coverageRequestGenerationRef.current + 1;
    coverageRequestGenerationRef.current = generation;
    coverageRequestRef.current = controller;
    setCoverageWorkspace(null);
    setCoverageLoadState("loading");
    setCoverageError(null);

    void fetchCoverageWorkspace({
      apiBase,
      projectId: workspace.project_id,
      dbPath,
      signal: controller.signal,
    }).then((candidate) => {
      if (
        controller.signal.aborted
        || generation !== coverageRequestGenerationRef.current
      ) return;
      const latest = currentWorkspaceRef.current;
      if (!isCoverageWorkspaceProjectionForBlueprint(latest, candidate)) {
        throw new Error(
          "Coverage details do not match the validated project summary. Refresh the canonical workspace before using them.",
        );
      }
      setCoverageWorkspace(candidate);
      setCoverageLoadState("loaded");
    }).catch((cause) => {
      if (
        controller.signal.aborted
        || generation !== coverageRequestGenerationRef.current
      ) return;
      setCoverageWorkspace(null);
      setCoverageLoadState("error");
      setCoverageError(safeUiError(cause));
    }).finally(() => {
      if (coverageRequestRef.current === controller) coverageRequestRef.current = null;
    });

    return () => controller.abort();
  }, [
    apiBase,
    dbPath,
    initialWorkspace,
    workspace.database_uuid,
    workspace.project_id,
    workspace.current_blueprint.revision,
    workspace.current_blueprint.content_sha256,
    workspace.coverage.state,
    workspace.coverage.head?.revision,
    workspace.coverage.head?.content_sha256,
  ]);

  useEffect(() => {
    timelineRequestRef.current?.abort();
    const controller = new AbortController();
    const generation = timelineRequestGenerationRef.current + 1;
    timelineRequestGenerationRef.current = generation;
    timelineRequestRef.current = controller;
    setTimelineWorkspace(null);
    setTimelineLoadState("loading");
    setTimelineError(null);

    void fetchCanonicalTimelineWorkspace({
      apiBase,
      projectId: workspace.project_id,
      dbPath,
      signal: controller.signal,
    }).then((candidate) => {
      if (
        controller.signal.aborted
        || generation !== timelineRequestGenerationRef.current
      ) return;
      const latest = currentWorkspaceRef.current;
      if (!isTimelineWorkspaceProjectionForBlueprint(latest, candidate)) {
        throw new Error(
          "Timeline details do not match the validated project summary. Refresh the canonical workspace before using them.",
        );
      }
      setTimelineWorkspace(candidate);
      setTimelineLoadState("loaded");
    }).catch((cause) => {
      if (
        controller.signal.aborted
        || generation !== timelineRequestGenerationRef.current
      ) return;
      setTimelineWorkspace(null);
      setTimelineLoadState("error");
      setTimelineError(safeUiError(cause));
    }).finally(() => {
      if (timelineRequestRef.current === controller) timelineRequestRef.current = null;
    });

    return () => controller.abort();
  }, [
    apiBase,
    dbPath,
    initialWorkspace,
    workspace.database_uuid,
    workspace.project_id,
    workspace.timeline.state,
    workspace.timeline.head?.revision,
    workspace.timeline.head?.revision_sha256,
  ]);

  function beginWorkspaceRequest(): WorkspaceRequest {
    activeRequestRef.current?.abort();
    const controller = new AbortController();
    activeRequestRef.current = controller;
    return {
      controller,
      generation: requestGenerationRef.current + 1,
      capturedWorkspace: currentWorkspaceRef.current,
    };
  }

  function activateWorkspaceRequest(request: WorkspaceRequest): WorkspaceRequest {
    requestGenerationRef.current = request.generation;
    return request;
  }

  function isCurrentWorkspaceRequest(request: WorkspaceRequest): boolean {
    return !request.controller.signal.aborted
      && canApplyBlueprintWorkspaceRequest({
        captured: request.capturedWorkspace,
        current: currentWorkspaceRef.current,
        capturedGeneration: request.generation,
        currentGeneration: requestGenerationRef.current,
      });
  }

  function finishWorkspaceRequest(request: WorkspaceRequest): boolean {
    if (activeRequestRef.current === request.controller) {
      activeRequestRef.current = null;
    }
    return requestGenerationRef.current === request.generation;
  }

  function acceptWorkspace(
    next: BlueprintProjectWorkspaceModel,
    request: WorkspaceRequest,
  ): void {
    if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
    if (!isSameBlueprintWorkspaceIdentity(currentWorkspaceRef.current, next)) {
      setHeadStatus("unknown");
      throw new Error(
        "The media database or project identity changed. Reopen the current library before continuing.",
      );
    }
    currentWorkspaceRef.current = next;
    setWorkspace(next);
    setCompareTo(String(next.current_blueprint.revision));
    setHeadStatus("validated");
    // A successful canonical refresh is the authority for whether the prior
    // native command is still active. Drop the renderer-local snapshot so an
    // `unknown` or initially `requested` result cannot lock egress forever.
    setExportResult(null);
    lastEmittedWorkspaceRef.current = next;
    onWorkspaceChangeRef.current?.(next);
  }

  async function refreshWorkspace(
    request: WorkspaceRequest,
  ): Promise<BlueprintProjectWorkspaceModel> {
    const next = await fetchBlueprintWorkspace({
      apiBase,
      projectId: request.capturedWorkspace.project_id,
      dbPath,
      signal: request.controller.signal,
    });
    acceptWorkspace(next, request);
    return next;
  }

  async function refreshTimelineForWorkspace(
    expectedWorkspace: BlueprintProjectWorkspaceModel,
    request: WorkspaceRequest,
  ): Promise<CanonicalTimelineWorkspace> {
    timelineRequestGenerationRef.current += 1;
    timelineRequestRef.current?.abort();
    timelineRequestRef.current = null;
    setTimelineLoadState("loading");
    setTimelineError(null);
    const candidate = await fetchCanonicalTimelineWorkspace({
      apiBase,
      projectId: expectedWorkspace.project_id,
      dbPath,
      signal: request.controller.signal,
    });
    if (
      request.controller.signal.aborted
      || request.generation !== requestGenerationRef.current
      || currentWorkspaceRef.current !== expectedWorkspace
    ) throw new StaleWorkspaceRequestError();
    if (!isTimelineWorkspaceProjectionForBlueprint(expectedWorkspace, candidate)) {
      setTimelineWorkspace(null);
      setTimelineLoadState("error");
      throw new Error(
        "Canonical Timeline details do not match the refreshed project summary.",
      );
    }
    setTimelineWorkspace(candidate);
    setTimelineLoadState("loaded");
    return candidate;
  }

  async function handleRefresh(): Promise<void> {
    if (pendingTimelineEdit !== null || pendingTimelineRestore !== null) {
      setError("Save or discard the pending Timeline change before refreshing canonical state.");
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    setIsRefreshing(true);
    setHeadStatus("refreshing");
    setMessage(null);
    setError(null);
    setRestoreReview(null);
    try {
      const next = await refreshWorkspace(request);
      if (next.canonical_export.state !== "in_progress") {
        setExportPollState({ state: "idle" });
      }
      setMessage(`Canonical head refreshed at exact revision ${next.current_blueprint.revision}.`);
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      setHeadStatus("unknown");
      setError(
        `Canonical refresh failed integrity or availability checks. The displayed revision is only the last validated snapshot and is not claimed as current: ${safeUiError(cause)}`,
      );
    } finally {
      if (finishWorkspaceRequest(request)) setIsRefreshing(false);
    }
  }

  async function handleRefreshTimeline(): Promise<void> {
    if (pendingTimelineEdit !== null || pendingTimelineRestore !== null) {
      setError("Save or discard the pending Timeline change before refreshing the Timeline resource.");
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    setIsRefreshing(true);
    setHeadStatus("refreshing");
    setMessage(null);
    setError(null);
    try {
      const next = await refreshWorkspace(request);
      const refreshedTimeline = await refreshTimelineForWorkspace(next, request);
      setMessage(
        refreshedTimeline.head === null
          ? "Canonical Timeline resource refreshed: no first-cut draft is materialized."
          : `Canonical Timeline revision ${refreshedTimeline.head.revision} and its fixed source identities were refreshed and verified.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      setHeadStatus("unknown");
      setTimelineWorkspace(null);
      setTimelineLoadState("error");
      setTimelineError(safeUiError(cause));
      setError(
        `Canonical Timeline refresh failed. The displayed project is only the last validated snapshot and no Timeline claim is inferred: ${safeUiError(cause)}`,
      );
    } finally {
      if (finishWorkspaceRequest(request)) setIsRefreshing(false);
    }
  }

  async function handleCompare(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const beforeRevision = Number(compareFrom);
    const afterRevision = Number(compareTo);
    if (!Number.isInteger(beforeRevision) || beforeRevision < 1 || !Number.isInteger(afterRevision) || afterRevision < 1) {
      setError("Choose two positive, whole revision numbers to compare.");
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    setIsComparing(true);
    setMessage(null);
    setError(null);
    try {
      const [before, after] = await Promise.all([
        fetchBlueprintRevision({
          apiBase,
          projectId: request.capturedWorkspace.project_id,
          revision: beforeRevision,
          dbPath,
          signal: request.controller.signal,
        }),
        fetchBlueprintRevision({
          apiBase,
          projectId: request.capturedWorkspace.project_id,
          revision: afterRevision,
          dbPath,
          signal: request.controller.signal,
        }),
      ]);
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        before.database_uuid !== request.capturedWorkspace.database_uuid
        || after.database_uuid !== request.capturedWorkspace.database_uuid
        || before.project_id !== request.capturedWorkspace.project_id
        || after.project_id !== request.capturedWorkspace.project_id
      ) {
        setHeadStatus("unknown");
        setComparison(null);
        throw new Error(
          "The media database changed while revisions were being compared. Refresh the canonical workspace before continuing.",
        );
      }
      setComparison({
        before,
        after,
        sections: compareBlueprintSemantics(before.blueprint.semantic, after.blueprint.semantic),
      });
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      setComparison(null);
      setError(safeUiError(cause));
    } finally {
      if (finishWorkspaceRequest(request)) setIsComparing(false);
    }
  }

  async function handleReviewRestore(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const revision = Number(restoreFrom);
    if (!Number.isInteger(revision) || revision < 1) {
      setError("Choose a positive, whole revision number to restore.");
      return;
    }
    if (!canRestore) {
      setError(
        canWrite
          ? "Current-head integrity is unknown. Refresh the canonical workspace before reviewing a restore."
          : "This workspace is read-only. Open the writable desktop workspace to restore a revision.",
      );
      return;
    }
    if (revision === current.revision) {
      setError("That revision is already the current head; no append-only restore is needed.");
      return;
    }

    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    setIsReviewingRestore(true);
    setMessage(null);
    setError(null);
    setRestoreReview(null);
    try {
      const target = await fetchBlueprintRevision({
        apiBase,
        projectId: request.capturedWorkspace.project_id,
        revision,
        dbPath,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        target.database_uuid !== request.capturedWorkspace.database_uuid
        || target.project_id !== request.capturedWorkspace.project_id
      ) {
        setHeadStatus("unknown");
        throw new Error(
          "The media database changed while the restore target was being loaded. Refresh the canonical workspace before continuing.",
        );
      }
      const capturedCurrent = request.capturedWorkspace.current_blueprint;
      setRestoreReview({
        before: capturedCurrent,
        after: target,
        sections: compareBlueprintSemantics(capturedCurrent.blueprint.semantic, target.blueprint.semantic),
      });
      setMessage(
        `Restore target revision ${target.revision} is verified. Review all 12 typed sections, then explicitly confirm the append-only restore.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      setError(safeUiError(cause));
    } finally {
      if (finishWorkspaceRequest(request)) setIsReviewingRestore(false);
    }
  }

  async function handleConfirmRestore(): Promise<void> {
    if (!restoreReview || !canRestore) {
      setError("Review a restore target against a validated current head before confirming.");
      return;
    }
    const latestWorkspace = currentWorkspaceRef.current;
    const latestCurrent = latestWorkspace.current_blueprint;
    if (
      restoreReview.before.revision !== latestCurrent.revision
      || restoreReview.before.content_sha256 !== latestCurrent.content_sha256
      || restoreReview.before.database_uuid !== latestWorkspace.database_uuid
      || restoreReview.after.database_uuid !== latestWorkspace.database_uuid
      || restoreReview.before.project_id !== latestWorkspace.project_id
      || restoreReview.after.project_id !== latestWorkspace.project_id
    ) {
      setRestoreReview(null);
      setError("The displayed head changed after review. Review the restore target again before writing.");
      return;
    }

    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    setIsRestoring(true);
    setMessage(null);
    setError(null);
    try {
      await restoreBlueprintRevision({
        apiBase,
        projectId: request.capturedWorkspace.project_id,
        dbPath,
        expectedDatabaseUuid: request.capturedWorkspace.database_uuid,
        expectedHead: {
          revision: restoreReview.before.revision,
          content_sha256: restoreReview.before.content_sha256,
        },
        restoreFrom: {
          revision: restoreReview.after.revision,
          content_sha256: restoreReview.after.content_sha256,
        },
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      const next = await refreshWorkspace(request);
      setRestoreFrom(String(restoreReview.after.revision));
      setComparison(null);
      setRestoreReview(null);
      setMessage(
        `Revision ${restoreReview.after.revision} was restored as new head revision ${next.current_blueprint.revision}. Earlier revisions were not rewritten.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && cause.code === "blueprint_head_conflict"
      ) {
        try {
          const next = await refreshWorkspace(request);
          setError(
            `Restore stopped because the Blueprint head moved to revision ${next.current_blueprint.revision}. The workspace was refreshed; review that head before trying again.`,
          );
        } catch (refreshCause) {
          if (
            !isCurrentWorkspaceRequest(request)
            || refreshCause instanceof StaleWorkspaceRequestError
          ) return;
          setHeadStatus("unknown");
          setError(`Restore stopped on a concurrent head change. Refresh also failed: ${safeUiError(refreshCause)}`);
        }
      } else {
        setHeadStatus("unknown");
        setRestoreReview(null);
        setError(
          `The restore outcome and canonical head could not be verified. Further writes are disabled until refresh succeeds: ${safeUiError(cause)}`,
        );
      }
    } finally {
      if (finishWorkspaceRequest(request)) setIsRestoring(false);
    }
  }

  async function handleMaterializeCoverage(): Promise<void> {
    if (!canMaterializeCoverage || isBusy) {
      setError(
        !canWrite
          ? "This workspace is read-only. Open the writable desktop workspace to materialize Coverage."
          : coverage.state === "current"
            ? "The canonical Coverage Plan is already current. No replacement revision is needed."
            : "Coverage materialization requires a validated canonical Blueprint head.",
      );
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const expectedBlueprint = {
      revision: request.capturedWorkspace.current_blueprint.revision,
      content_sha256: request.capturedWorkspace.current_blueprint.content_sha256,
      semantic_sha256: request.capturedWorkspace.current_blueprint.semantic_sha256,
    };
    setIsMaterializingCoverage(true);
    setMessage(null);
    setError(null);
    try {
      const result = await materializeCoverageBaseline({
        apiBase,
        projectId: request.capturedWorkspace.project_id,
        dbPath,
        expectedDatabaseUuid: request.capturedWorkspace.database_uuid,
        expectedBlueprint,
        expectedPlanHead: request.capturedWorkspace.coverage.head,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        result.project_id !== request.capturedWorkspace.project_id
        || result.result.result_head.blueprint_revision !== expectedBlueprint.revision
        || result.result.result_head.blueprint_content_sha256 !== expectedBlueprint.content_sha256
        || result.result.result_head.blueprint_semantic_sha256 !== expectedBlueprint.semantic_sha256
      ) {
        throw new Error("Coverage materialization returned a result for a different exact Blueprint.");
      }
      const next = await refreshWorkspace(request);
      if (
        next.coverage.head?.revision !== result.result.result_head.revision
        || next.coverage.head?.content_sha256 !== result.result.result_head.content_sha256
      ) {
        throw new Error("Coverage was written, but the refreshed canonical summary did not confirm its exact head.");
      }
      setMessage(
        `Coverage Plan revision ${result.result.result_head.revision} is materialized and verified. Refresh its details to see whether every Beat is lowerable into a hard-cut draft.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && [
          "coverage_head_conflict",
          "blueprint_head_conflict",
          "database_identity_changed",
        ].includes(cause.code ?? "")
      ) {
        try {
          const next = await refreshWorkspace(request);
          setError(
            `Coverage materialization stopped because its exact precondition changed. The canonical workspace was refreshed to Blueprint revision ${next.current_blueprint.revision}${next.coverage.head ? ` and Coverage revision ${next.coverage.head.revision}` : " with no Coverage head"}; review it before trying again.`,
          );
        } catch (refreshCause) {
          if (
            !isCurrentWorkspaceRequest(request)
            || refreshCause instanceof StaleWorkspaceRequestError
          ) return;
          setHeadStatus("unknown");
          setError(`Coverage stopped on a concurrent state change. Canonical refresh also failed: ${safeUiError(refreshCause)}`);
        }
      } else if (cause instanceof VideoApiError && cause.status < 500) {
        setError(safeUiError(cause));
      } else {
        setHeadStatus("unknown");
        setError(
          `The Coverage write outcome could not be verified. Further writes are disabled until canonical refresh succeeds: ${safeUiError(cause)}`,
        );
      }
    } finally {
      if (finishWorkspaceRequest(request)) setIsMaterializingCoverage(false);
    }
  }

  async function handleMaterializeTimeline(): Promise<void> {
    if (!canMaterializeTimeline || isBusy || coverageWorkspace === null) {
      setError(
        !canWrite
          ? "This workspace is read-only. Open the writable desktop workspace to materialize a first cut."
          : timelineSummary.head !== null
            ? "A canonical Timeline already exists and will not be overwritten."
            : "Timeline materialization requires a validated, current, gap-free Coverage Plan with exact source evidence.",
      );
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const capturedCoverage = coverageWorkspace;
    if (!isCoverageWorkspaceProjectionForBlueprint(request.capturedWorkspace, capturedCoverage)) {
      setError("Coverage details no longer match the canonical project summary. Refresh before lowering.");
      finishWorkspaceRequest(request);
      return;
    }
    let expectedCoverage: CanonicalTimelineCoverageBinding;
    try {
      expectedCoverage = timelineCoverageBindingFromWorkspace(capturedCoverage);
    } catch (cause) {
      setError(safeUiError(cause));
      finishWorkspaceRequest(request);
      return;
    }
    const expectedBlueprint = {
      revision: request.capturedWorkspace.current_blueprint.revision,
      content_sha256: request.capturedWorkspace.current_blueprint.content_sha256,
      semantic_sha256: request.capturedWorkspace.current_blueprint.semantic_sha256,
      operation_id: request.capturedWorkspace.current_blueprint.operation_id,
    };
    setIsMaterializingTimeline(true);
    setMessage(null);
    setError(null);
    try {
      const result = await materializeCanonicalTimelineFirstCut({
        apiBase,
        projectId: request.capturedWorkspace.project_id,
        dbPath,
        expectedDatabaseUuid: request.capturedWorkspace.database_uuid,
        expectedBlueprint,
        expectedCoverage,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        result.project_id !== request.capturedWorkspace.project_id
        || JSON.stringify(result.result.result_head.blueprint_binding) !== JSON.stringify(expectedBlueprint)
        || JSON.stringify(result.result.result_head.coverage_binding) !== JSON.stringify(expectedCoverage)
      ) {
        throw new Error("Timeline materialization returned a head for different exact inputs.");
      }
      const next = await refreshWorkspace(request);
      if (
        next.timeline.head?.revision_sha256 !== result.result.result_head.revision_sha256
        || next.timeline.head?.timeline_content_sha256
          !== result.result.result_head.timeline_content_sha256
      ) {
        throw new Error("Timeline was written, but the refreshed project summary did not confirm its exact head.");
      }
      const refreshedTimeline = await refreshTimelineForWorkspace(next, request);
      setMessage(
        `Canonical Timeline revision ${refreshedTimeline.head?.revision ?? 1} is materialized as a silent hard-cut draft. Preview remains unavailable; canonical 1080p export now requires a separate native approval.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && [
          "blueprint_head_conflict",
          "coverage_head_conflict",
          "timeline_manual_current_conflict",
          "database_identity_changed",
        ].includes(cause.code ?? "")
      ) {
        try {
          const next = await refreshWorkspace(request);
          await refreshTimelineForWorkspace(next, request);
          setError(
            "Timeline materialization stopped because an exact precondition changed. The canonical project and Timeline were refreshed; review them before trying again.",
          );
        } catch (refreshCause) {
          setHeadStatus("unknown");
          setError(`Timeline stopped on a concurrent change and refresh failed: ${safeUiError(refreshCause)}`);
        }
      } else if (cause instanceof VideoApiError && cause.status < 500) {
        setError(safeUiError(cause));
      } else {
        setHeadStatus("unknown");
        setError(
          `The Timeline write outcome could not be verified. Further writes are disabled until canonical refresh succeeds: ${safeUiError(cause)}`,
        );
      }
    } finally {
      if (finishWorkspaceRequest(request)) setIsMaterializingTimeline(false);
    }
  }

  async function handleReconcileTimeline(): Promise<void> {
    if (!canReconcileTimeline || isBusy || coverageWorkspace === null) {
      setError(
        !canWrite
          ? "This workspace is read-only. Open the writable desktop workspace to reconcile its Timeline."
          : timelineSummary.state === "current"
            ? "The canonical Timeline is already current; no reconciliation is needed."
            : "Timeline reconciliation requires an explicit stale head and a validated, current, gap-free Coverage Plan.",
      );
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const capturedCoverage = coverageWorkspace;
    const expectedTimelineHead = request.capturedWorkspace.timeline.head;
    if (
      expectedTimelineHead === null
      || !isCoverageWorkspaceProjectionForBlueprint(
        request.capturedWorkspace,
        capturedCoverage,
      )
    ) {
      setError(
        "Timeline or Coverage details no longer match the canonical project summary. Refresh before reconciling.",
      );
      finishWorkspaceRequest(request);
      return;
    }
    let expectedCoverage: CanonicalTimelineCoverageBinding;
    try {
      expectedCoverage = timelineCoverageBindingFromWorkspace(capturedCoverage);
    } catch (cause) {
      setError(safeUiError(cause));
      finishWorkspaceRequest(request);
      return;
    }
    const expectedBlueprint = {
      revision: request.capturedWorkspace.current_blueprint.revision,
      content_sha256: request.capturedWorkspace.current_blueprint.content_sha256,
      semantic_sha256: request.capturedWorkspace.current_blueprint.semantic_sha256,
      operation_id: request.capturedWorkspace.current_blueprint.operation_id,
    };
    setIsReconcilingTimeline(true);
    setMessage(null);
    setError(null);
    try {
      const result = await reconcileCanonicalTimelineFromCoverage({
        apiBase,
        projectId: request.capturedWorkspace.project_id,
        dbPath,
        expectedDatabaseUuid: request.capturedWorkspace.database_uuid,
        expectedBlueprint,
        expectedCoverage,
        expectedTimelineHead,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        result.project_id !== request.capturedWorkspace.project_id
        || result.result.result_head.revision !== expectedTimelineHead.revision + 1
        || JSON.stringify(result.result.result_head.blueprint_binding)
          !== JSON.stringify(expectedBlueprint)
        || JSON.stringify(result.result.result_head.coverage_binding)
          !== JSON.stringify(expectedCoverage)
      ) {
        throw new Error(
          "Timeline reconciliation returned a head for different exact inputs.",
        );
      }
      const next = await refreshWorkspace(request);
      if (
        next.timeline.head?.revision_sha256
          !== result.result.result_head.revision_sha256
        || next.timeline.head?.timeline_content_sha256
          !== result.result.result_head.timeline_content_sha256
      ) {
        throw new Error(
          "Timeline reconciliation was written, but canonical refresh did not confirm its exact head.",
        );
      }
      const refreshedTimeline = await refreshTimelineForWorkspace(next, request);
      setMessage(
        `Canonical Timeline revision ${refreshedTimeline.head?.revision ?? result.result.result_head.revision} now reconciles the exact current Coverage Plan. The prior revision remains append-only history.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && [
          "blueprint_head_conflict",
          "coverage_head_conflict",
          "timeline_head_conflict",
          "timeline_already_current",
          "database_identity_changed",
          "coverage_plan_stale_evidence",
          "coverage_blueprint_conflict",
        ].includes(cause.code ?? "")
      ) {
        try {
          const next = await refreshWorkspace(request);
          await refreshTimelineForWorkspace(next, request);
          setError(
            "Timeline reconciliation stopped because an exact precondition changed. The canonical project and Timeline were refreshed; review them before trying again.",
          );
        } catch (refreshCause) {
          setHeadStatus("unknown");
          setError(`Timeline reconciliation stopped and canonical refresh failed: ${safeUiError(refreshCause)}`);
        }
      } else if (cause instanceof VideoApiError && cause.status < 500) {
        setError(safeUiError(cause));
      } else {
        setHeadStatus("unknown");
        setError(
          `The Timeline reconciliation outcome could not be verified. Further writes are disabled until canonical refresh succeeds: ${safeUiError(cause)}`,
        );
      }
    } finally {
      if (finishWorkspaceRequest(request)) setIsReconcilingTimeline(false);
    }
  }

  function updateTimelineClipEditDraft(
    clipId: string,
    patch: TimelineClipEditDraft,
  ): void {
    setTimelineClipEditDrafts((current) => ({
      ...current,
      [clipId]: {
        ...current[clipId],
        ...patch,
      },
    }));
  }

  function replacementCandidatesForClip(clipId: string) {
    if (timelineWorkspace === null || coverageWorkspace === null) return [];
    try {
      return canonicalTimelineReplacementCandidates(
        timelineWorkspace,
        coverageWorkspace,
        clipId,
      );
    } catch {
      return [];
    }
  }

  async function handleInspectTimelineRevision(): Promise<void> {
    const revision = Number(historicalTimelineRevision);
    const currentTimeline = timelineWorkspace;
    if (pendingTimelineEdit !== null || pendingTimelineRestore !== null) {
      setError("Save or discard the pending Timeline change before inspecting another historical revision.");
      return;
    }
    if (
      currentTimeline === null
      || currentTimeline.head === null
      || !Number.isInteger(revision)
      || revision < 1
      || revision >= currentTimeline.head.revision
    ) {
      setError("Choose a historical Timeline revision below the exact current head.");
      return;
    }

    const controller = new AbortController();
    const generation = historicalTimelineRequestGenerationRef.current + 1;
    const capturedPreconditionIdentity = timelineEditPreconditionIdentity;
    historicalTimelineRequestGenerationRef.current = generation;
    historicalTimelineRequestRef.current?.abort();
    historicalTimelineRequestRef.current = controller;
    setHistoricalTimelineLoadState("loading");
    setHistoricalTimelineWorkspace(null);
    setHistoricalTimelineError(null);
    setMessage(null);
    setError(null);
    try {
      const candidate = await fetchCanonicalTimelineWorkspace({
        apiBase,
        projectId: currentTimeline.project_id,
        dbPath,
        revision,
        signal: controller.signal,
      });
      if (
        controller.signal.aborted
        || generation !== historicalTimelineRequestGenerationRef.current
        || capturedPreconditionIdentity !== timelineEditPreconditionIdentityRef.current
      ) throw new StaleWorkspaceRequestError();
      if (!isHistoricalTimelineSelectionForCurrentLedger(currentTimeline, candidate)) {
        throw new Error(
          "The selected revision does not belong to the exact current Timeline and upstream bindings.",
        );
      }
      setHistoricalTimelineWorkspace(candidate);
      setHistoricalTimelineLoadState("loaded");
      setMessage(
        `Historical Timeline revision ${revision} opened read-only. Revision ${currentTimeline.head.revision} remains the canonical head.`,
      );
    } catch (cause) {
      if (
        controller.signal.aborted
        || generation !== historicalTimelineRequestGenerationRef.current
        || cause instanceof StaleWorkspaceRequestError
      ) return;
      setHistoricalTimelineLoadState("error");
      setHistoricalTimelineError(safeUiError(cause));
      setError(`Historical Timeline revision could not be verified: ${safeUiError(cause)}`);
    } finally {
      if (historicalTimelineRequestRef.current === controller) {
        historicalTimelineRequestRef.current = null;
      }
    }
  }

  function stageTimelineRestore(): void {
    if (
      !canStageTimelineRestore
      || isBusy
      || timelineWorkspace === null
      || historicalTimelineWorkspace === null
    ) {
      setError(
        pendingTimelineEdit !== null || pendingTimelineRestore !== null
          ? "Discard or save the current pending Timeline change before staging a restore."
          : "Open one verified historical revision from the exact current Timeline before staging a restore.",
      );
      return;
    }
    try {
      const pending = stageCanonicalTimelineRestore({
        current: timelineWorkspace,
        historical: historicalTimelineWorkspace,
      });
      setPendingTimelineRestore(pending);
      setMessage(
        `${pending.summary} is staged locally. The current head remains revision ${pending.based_on_head.revision} until Save appends a canonical successor.`,
      );
      setError(null);
    } catch (cause) {
      setError(safeUiError(cause));
    }
  }

  function stageTimelineEdit(edit: CanonicalTimelineEdit): void {
    if (
      !canEditTimeline
      || isBusy
      || pendingTimelineEdit !== null
      || pendingTimelineRestore !== null
      || timelineWorkspace === null
      || timelineWorkspace.head === null
      || coverageWorkspace === null
    ) {
      setError(
        pendingTimelineEdit !== null || pendingTimelineRestore !== null
          ? "Discard or save the current pending Timeline change before staging an edit."
          : "Timeline editing requires the exact current Timeline head and its pinned Coverage Plan in a writable workspace.",
      );
      return;
    }
    try {
      const pending = stageCanonicalTimelineEdit({
        workspace: timelineWorkspace,
        coverage: coverageWorkspace,
        edit,
      });
      setPendingTimelineEdit(pending);
      setMessage(
        pending.previewKind === "local_effect_preview"
          ? `${pending.summary}. This is an unsaved local preview; Save as revision ${timelineWorkspace.head.revision + 1} is required.`
          : `${pending.summary}. Replacement source identity is resolved only by Core, so the visual changes only after save and canonical reread.`,
      );
      setError(null);
    } catch (cause) {
      setError(safeUiError(cause));
    }
  }

  async function handleApplyTimelineEdit(): Promise<void> {
    if (
      !canSubmitPendingTimelineEdit
      || isBusy
      || pendingTimelineEdit === null
    ) {
      setError(
        "Stage one Timeline edit in this writable project before saving a new revision.",
      );
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const capturedPending = pendingTimelineEdit;
    const expectedTimelineHead = capturedPending.based_on_head;
    if (
      capturedPending.database_uuid !== request.capturedWorkspace.database_uuid
      || capturedPending.project_id !== request.capturedWorkspace.project_id
    ) {
      setError("Pending Timeline edit belongs to a different database or project.");
      finishWorkspaceRequest(request);
      return;
    }
    const expectedBlueprint = expectedTimelineHead.blueprint_binding;
    const expectedCoverage = expectedTimelineHead.coverage_binding;
    isApplyingTimelineEditRef.current = true;
    setIsApplyingTimelineEdit(true);
    setMessage(null);
    setError(null);
    try {
      const result = await applyCanonicalTimelineEdit({
        apiBase,
        projectId: capturedPending.project_id,
        dbPath,
        expectedDatabaseUuid: capturedPending.database_uuid,
        expectedBlueprint,
        expectedCoverage,
        expectedTimelineHead,
        edit: capturedPending.edit,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        result.project_id !== request.capturedWorkspace.project_id
        || result.result.result_head.revision !== expectedTimelineHead.revision + 1
        || result.result.result_head.timeline_id !== expectedTimelineHead.timeline_id
        || JSON.stringify(result.result.edit) !== JSON.stringify(capturedPending.edit)
        || JSON.stringify(result.result.result_head.blueprint_binding)
          !== JSON.stringify(expectedBlueprint)
        || JSON.stringify(result.result.result_head.coverage_binding)
          !== JSON.stringify(expectedCoverage)
      ) {
        throw new Error("Timeline edit returned a revision for different exact inputs.");
      }
      const next = await refreshWorkspace(request);
      if (
        next.timeline.head === null
        || !isSameCanonicalTimelineHead(
          next.timeline.head,
          result.result.result_head,
        )
      ) {
        throw new Error(
          "Timeline edit was written, but canonical refresh did not confirm its exact successor head.",
        );
      }
      const refreshedTimeline = await refreshTimelineForWorkspace(next, request);
      if (
        refreshedTimeline.head === null
        || refreshedTimeline.selected_revision === null
        || !refreshedTimeline.selected_revision.is_head
        || !isSameCanonicalTimelineHead(
          refreshedTimeline.head,
          result.result.result_head,
        )
        || !isSameCanonicalTimelineHead(
          refreshedTimeline.selected_revision,
          result.result.result_head,
        )
      ) {
        throw new Error(
          "Timeline edit was written, but canonical resource reread did not confirm its exact bytes.",
        );
      }
      setPendingTimelineEdit(null);
      setTimelineClipEditDrafts({});
      setMessage(
        `Canonical Timeline revision ${result.result.result_head.revision} saved and reopened. Revision ${expectedTimelineHead.revision} remains append-only history; export now binds only the new exact head.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && TIMELINE_REFRESHABLE_CONFLICT_CODES.has(cause.code ?? "")
      ) {
        try {
          const next = await refreshWorkspace(request);
          await refreshTimelineForWorkspace(next, request);
          setPendingTimelineEdit(null);
          setTimelineClipEditDrafts({});
          setError(
            "Timeline save stopped because an exact precondition changed. Canonical state was refreshed and the unsaved edit was discarded; review the new head before trying again.",
          );
        } catch (refreshCause) {
          setHeadStatus("unknown");
          setError(`Timeline save conflicted and canonical refresh failed: ${safeUiError(refreshCause)}`);
        }
      } else if (
        cause instanceof VideoApiError
        && cause.status === 409
        && cause.code === "timeline_idempotency_conflict"
      ) {
        setError(
          "This pending Timeline edit identity is permanently bound to a different request. It was retained for inspection; discard it before staging a new edit.",
        );
      } else if (cause instanceof VideoApiError && cause.status === 409) {
        setHeadStatus("unknown");
        setError(
          `Timeline save returned an unrecognized conflict. The pending edit and its exact write identity were retained: ${safeUiError(cause)}`,
        );
      } else if (cause instanceof VideoApiError && cause.status < 500) {
        setError(safeUiError(cause));
      } else {
        setHeadStatus("unknown");
        setError(
          `The Timeline edit outcome could not be verified. Retry Save with the retained exact write identity so the permanent receipt can reconcile it: ${safeUiError(cause)}`,
        );
      }
    } finally {
      isApplyingTimelineEditRef.current = false;
      if (finishWorkspaceRequest(request)) setIsApplyingTimelineEdit(false);
    }
  }

  async function handleApplyTimelineStructuralEdit(
    edit: CanonicalTimelineStructuralEdit,
  ): Promise<void> {
    const capturedTimeline = timelineWorkspace;
    if (
      !canEditTimeline
      || isBusy
      || pendingTimelineEdit !== null
      || pendingTimelineRestore !== null
      || capturedTimeline === null
      || capturedTimeline.head === null
    ) {
      setError(
        "Split and Delete require the exact current Timeline head, no pending edit, and a writable workspace.",
      );
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const expectedTimelineHead = capturedTimeline.head;
    const expectedBlueprint = expectedTimelineHead.blueprint_binding;
    const expectedCoverage = expectedTimelineHead.coverage_binding;
    setIsApplyingTimelineStructuralEdit(true);
    setMessage(null);
    setError(null);
    try {
      const result = await applyCanonicalTimelineStructuralEdit({
        apiBase,
        projectId: capturedTimeline.project_id,
        dbPath,
        expectedDatabaseUuid: capturedTimeline.database_uuid,
        expectedBlueprint,
        expectedCoverage,
        expectedTimelineHead,
        edit,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        result.project_id !== request.capturedWorkspace.project_id
        || result.result.result_head.revision !== expectedTimelineHead.revision + 1
        || result.result.result_head.timeline_id !== expectedTimelineHead.timeline_id
        || JSON.stringify(result.result.structural_edit) !== JSON.stringify(edit)
        || JSON.stringify(result.result.result_head.blueprint_binding)
          !== JSON.stringify(expectedBlueprint)
        || JSON.stringify(result.result.result_head.coverage_binding)
          !== JSON.stringify(expectedCoverage)
      ) {
        throw new Error("Timeline structural edit returned a revision for different exact inputs.");
      }
      const next = await refreshWorkspace(request);
      if (
        next.timeline.head === null
        || !isSameCanonicalTimelineHead(next.timeline.head, result.result.result_head)
      ) {
        throw new Error(
          "Timeline structural edit was written, but canonical refresh did not confirm its exact successor head.",
        );
      }
      const refreshedTimeline = await refreshTimelineForWorkspace(next, request);
      if (
        refreshedTimeline.head === null
        || refreshedTimeline.selected_revision === null
        || !refreshedTimeline.selected_revision.is_head
        || !isSameCanonicalTimelineHead(
          refreshedTimeline.head,
          result.result.result_head,
        )
        || !isSameCanonicalTimelineHead(
          refreshedTimeline.selected_revision,
          result.result.result_head,
        )
      ) {
        throw new Error(
          "Timeline structural edit was written, but canonical reread did not confirm its exact bytes.",
        );
      }
      setTimelineClipEditDrafts({});
      setMessage(
        `${edit.op === "split_clip" ? "Split" : "Delete"} saved as canonical Timeline revision ${result.result.result_head.revision}; revision ${expectedTimelineHead.revision} remains recoverable in append-only history.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && TIMELINE_REFRESHABLE_CONFLICT_CODES.has(cause.code ?? "")
      ) {
        try {
          const next = await refreshWorkspace(request);
          await refreshTimelineForWorkspace(next, request);
          setTimelineClipEditDrafts({});
          setError(
            "Split/Delete stopped because an exact precondition changed. Canonical state was refreshed; review the new head before trying again.",
          );
        } catch (refreshCause) {
          setHeadStatus("unknown");
          setError(`Split/Delete conflicted and canonical refresh failed: ${safeUiError(refreshCause)}`);
        }
      } else if (cause instanceof VideoApiError && cause.status < 500) {
        setError(safeUiError(cause));
      } else {
        setHeadStatus("unknown");
        setError(
          `The Split/Delete outcome could not be verified. Refresh canonical state before another write: ${safeUiError(cause)}`,
        );
      }
    } finally {
      if (finishWorkspaceRequest(request)) setIsApplyingTimelineStructuralEdit(false);
    }
  }

  async function handleRestoreTimelineRevision(): Promise<void> {
    if (
      !canSubmitPendingTimelineRestore
      || isBusy
      || pendingTimelineRestore === null
    ) {
      setError(
        "Stage one verified historical Timeline revision in this writable project before saving a restore.",
      );
      return;
    }

    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const capturedPending = pendingTimelineRestore;
    const expectedTimelineHead = capturedPending.based_on_head;
    if (
      capturedPending.database_uuid !== request.capturedWorkspace.database_uuid
      || capturedPending.project_id !== request.capturedWorkspace.project_id
    ) {
      setError("Pending Timeline restore belongs to a different database or project.");
      finishWorkspaceRequest(request);
      return;
    }
    const expectedBlueprint = expectedTimelineHead.blueprint_binding;
    const expectedCoverage = expectedTimelineHead.coverage_binding;

    isRestoringTimelineRef.current = true;
    setIsRestoringTimeline(true);
    setMessage(null);
    setError(null);
    try {
      const result = await restoreCanonicalTimelineRevision({
        apiBase,
        projectId: capturedPending.project_id,
        dbPath,
        expectedDatabaseUuid: capturedPending.database_uuid,
        expectedBlueprint,
        expectedCoverage,
        expectedTimelineHead,
        restoreFrom: capturedPending.restore_from,
        signal: request.controller.signal,
      });
      if (!isCurrentWorkspaceRequest(request)) throw new StaleWorkspaceRequestError();
      if (
        result.project_id !== request.capturedWorkspace.project_id
        || result.result.result_head.revision !== expectedTimelineHead.revision + 1
        || result.result.result_head.timeline_id !== expectedTimelineHead.timeline_id
        || !isSameCanonicalTimelineHead(
          result.result.restore_from,
          capturedPending.restore_from,
        )
        || JSON.stringify(result.result.result_head.blueprint_binding)
          !== JSON.stringify(expectedBlueprint)
        || JSON.stringify(result.result.result_head.coverage_binding)
          !== JSON.stringify(expectedCoverage)
      ) {
        throw new Error("Timeline restore returned a revision for different exact N and K inputs.");
      }

      const next = await refreshWorkspace(request);
      if (
        next.timeline.head === null
        || !isSameCanonicalTimelineHead(next.timeline.head, result.result.result_head)
      ) {
        throw new Error(
          "Timeline restore was written, but canonical refresh did not confirm its exact successor head.",
        );
      }
      const refreshedTimeline = await refreshTimelineForWorkspace(next, request);
      if (
        refreshedTimeline.head === null
        || refreshedTimeline.selected_revision === null
        || !refreshedTimeline.selected_revision.is_head
        || !isSameCanonicalTimelineHead(
          refreshedTimeline.head,
          result.result.result_head,
        )
        || !isSameCanonicalTimelineHead(
          refreshedTimeline.selected_revision,
          result.result.result_head,
        )
      ) {
        throw new Error(
          "Timeline restore was written, but canonical resource reread did not confirm its exact bytes.",
        );
      }
      setPendingTimelineRestore(null);
      setHistoricalTimelineWorkspace(null);
      setHistoricalTimelineLoadState("idle");
      setHistoricalTimelineError(null);
      setMessage(
        `Historical revision ${capturedPending.restore_from.revision} was saved as canonical Timeline revision ${result.result.result_head.revision}. Revision ${expectedTimelineHead.revision} remains append-only history.`,
      );
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      if (
        cause instanceof VideoApiError
        && cause.status === 409
        && TIMELINE_REFRESHABLE_CONFLICT_CODES.has(cause.code ?? "")
      ) {
        try {
          const next = await refreshWorkspace(request);
          await refreshTimelineForWorkspace(next, request);
          setPendingTimelineRestore(null);
          setHistoricalTimelineWorkspace(null);
          setHistoricalTimelineLoadState("idle");
          setHistoricalTimelineError(null);
          setError(
            "Timeline restore stopped because an exact precondition changed. Canonical state was refreshed and the staged restore was discarded; inspect history again against the new head.",
          );
        } catch (refreshCause) {
          setHeadStatus("unknown");
          setError(`Timeline restore conflicted and canonical refresh failed: ${safeUiError(refreshCause)}`);
        }
      } else if (
        cause instanceof VideoApiError
        && cause.status === 409
        && cause.code === "timeline_idempotency_conflict"
      ) {
        setError(
          "This pending Timeline restore identity is permanently bound to a different request. It was retained for inspection; discard it before selecting another revision.",
        );
      } else if (cause instanceof VideoApiError && cause.status === 409) {
        setHeadStatus("unknown");
        setError(
          `Timeline restore returned an unrecognized conflict. The staged N/K identity was retained: ${safeUiError(cause)}`,
        );
      } else if (cause instanceof VideoApiError && cause.status < 500) {
        setError(safeUiError(cause));
      } else {
        setHeadStatus("unknown");
        setError(
          `The Timeline restore outcome could not be verified. Retry Save with the retained N/K identity so the permanent receipt can reconcile it: ${safeUiError(cause)}`,
        );
      }
    } finally {
      isRestoringTimelineRef.current = false;
      if (finishWorkspaceRequest(request)) setIsRestoringTimeline(false);
    }
  }

  async function handleApproveCanonicalExport(): Promise<void> {
    if (!canApproveCanonicalExport || isBusy) {
      setError(
        !canWrite
          ? "Open the writable desktop workspace to approve a local canonical export."
          : !hasCanonicalExportBridge
            ? "Canonical export approval is available only through the trusted MemoLens desktop bridge."
            : workspace.canonical_export.reason_code === "export_recovery_required"
              ? "The previous export outcome requires recovery. Restart MemoLens before approving another export."
              : timelineSummary.state === "missing"
                ? "Materialize a canonical Timeline before export approval."
                : timelineSummary.state !== "current"
                  ? "The canonical Timeline is stale. Refresh to inspect the trusted state; this first-cut head is preserved and cannot be rematerialized in the current slice."
                  : workspace.canonical_export.state === "in_progress" || localExportInProgress
                    ? "A canonical export job is already in progress. Refresh the workspace for its latest state."
                    : "The server has not enabled native approval for this exact Timeline head.",
      );
      return;
    }
    const request = activateWorkspaceRequest(beginWorkspaceRequest());
    const capturedTimelineWorkspace = timelineWorkspace;
    setIsApprovingExport(true);
    setExportPollState({ state: "idle" });
    setMessage(null);
    setError(null);
    try {
      const bridge = window.memolensDesktop?.approveAndExportCanonicalTimeline;
      if (!bridge) throw new Error("The trusted desktop export bridge is unavailable.");
      const head = request.capturedWorkspace.timeline.head;
      const selected = capturedTimelineWorkspace?.selected_revision;
      if (
        head === null
        || capturedTimelineWorkspace === null
        || selected === null
        || !selected.is_head
        || !isTimelineWorkspaceProjectionForBlueprint(
          request.capturedWorkspace,
          capturedTimelineWorkspace,
        )
        || selected.revision !== head.revision
        || selected.revision_sha256 !== head.revision_sha256
        || selected.timeline_id !== head.timeline_id
        || selected.timeline_content_sha256 !== head.timeline_content_sha256
      ) {
        throw new Error(
          "The exact captured Timeline is unavailable. Refresh the canonical workspace before export approval.",
        );
      }
      const sourceBindingsSha256 = await canonicalExportSourceBindingsSha256(
        capturedTimelineWorkspace.source_bindings,
      );
      if (!isCurrentWorkspaceRequest(request)) return;
      const result = normalizeDesktopCanonicalExportApprovalResult(await bridge({
        projectId: request.capturedWorkspace.project_id,
        databaseUuid: request.capturedWorkspace.database_uuid,
        expectedTimelineBinding: {
          revision: head.revision,
          revisionSha256: head.revision_sha256,
          timelineId: head.timeline_id,
          timelineContentSha256: head.timeline_content_sha256,
          sourceBindingsSha256,
        },
        suggestedPackageName: normalizeCanonicalExportPackageBasename(
          `${request.capturedWorkspace.title}-timeline-r${request.capturedWorkspace.timeline.head?.revision ?? 1}`,
        ),
      }));
      if (!isCurrentWorkspaceRequest(request)) return;
      setExportResult(result);
      if (result.status === "submitted") {
        setMessage(
          `Canonical export job ${result.job.jobId} is ${result.job.status}. The approved package name is ${result.job.packageBasename}.`,
        );
      } else if (result.status === "cancelled") {
        setMessage(result.message);
      } else if (result.status === "unknown") {
        setError(result.message);
      } else {
        setError(result.message);
      }
    } catch (cause) {
      if (!isCurrentWorkspaceRequest(request) || cause instanceof StaleWorkspaceRequestError) return;
      setExportResult(null);
      setError(safeUiError(cause));
    } finally {
      if (finishWorkspaceRequest(request)) setIsApprovingExport(false);
    }
  }

  return (
    <section className="video-panel video-blueprint-workspace" aria-labelledby="blueprint-workspace-title">
      <header className="blueprint-workspace-head">
        <div>
          <p className="eyebrow">Canonical creative Blueprint</p>
          <h3 id="blueprint-workspace-title">{workspace.title}</h3>
          <p>
            {headStatus === "validated"
              ? "This validated Blueprint is the creative proposal head for this project. "
              : "The displayed Blueprint is the last validated snapshot; its current-head status is not established. "}
            Its three history lanes below remain separate; this view is not a unified project ledger or a Timeline authority.
          </p>
        </div>
        <div className="blueprint-head-actions">
          <span className="blueprint-mode-pill">
            {headStatus === "validated" ? "Proposal head" : headStatus === "refreshing" ? "Validating…" : "Last validated snapshot"}
            {` · revision ${current.revision}`}
          </span>
          <button
            className="secondary-button compact-button"
            type="button"
            disabled={isBusy || pendingTimelineEdit !== null || pendingTimelineRestore !== null}
            title={pendingTimelineEdit === null && pendingTimelineRestore === null
              ? "Refresh the exact canonical Blueprint head."
              : "Save or discard the pending Timeline change before refreshing."}
            onClick={() => void handleRefresh()}
          >
            {isRefreshing ? "Refreshing…" : "Refresh canonical head"}
          </button>
        </div>
      </header>

      <div className="blueprint-head-contract" aria-label={headStatus === "validated" ? "Exact canonical Blueprint head" : "Last validated Blueprint snapshot; current status unknown"}>
        <div>
          <span>{headStatus === "validated" ? "Exact head" : "Last validated snapshot"}</span>
          <strong>Revision {current.revision}</strong>
          <Digest label="Blueprint content SHA-256">{current.content_sha256}</Digest>
        </div>
        <div>
          <span>Semantic digest</span>
          <strong>Creative meaning at this head</strong>
          <Digest label="Blueprint semantic SHA-256">{current.semantic_sha256}</Digest>
        </div>
        <div>
          <span>Append operation</span>
          <strong>{current.operation_id}</strong>
          <small>
            {headStatus === "validated" ? "Current and schema-validated" : "Previously validated; current status unknown"}
            {` · ${formatDate(workspace.updated_at)}`}
          </small>
        </div>
      </div>

      {headStatus !== "validated" ? (
        <p className="video-inline-warning" role="status" aria-live="polite">
          {headStatus === "refreshing"
            ? "Revalidating canonical head and scoped histories…"
            : "Current-head integrity is unknown. Restore is disabled until a full canonical refresh succeeds."}
        </p>
      ) : null}

      <section className="blueprint-head-contract" aria-label="Canonical Coverage Plan summary">
        <div>
          <span>Coverage Plan</span>
          <strong>
            {headStatus === "validated" ? coveragePresentation.label : `Last validated · ${coveragePresentation.label}`}
            {coverage.head === null ? "" : ` · revision ${coverage.head.revision}`}
          </strong>
          <small>{coveragePresentation.detail}</small>
          <button
            className="primary-button compact-button"
            type="button"
            disabled={!canMaterializeCoverage || isBusy}
            onClick={() => void handleMaterializeCoverage()}
            title={
              !canWrite
                ? "Coverage materialization requires the writable desktop workspace."
                : headStatus !== "validated"
                  ? "Refresh the canonical Blueprint head first."
                  : coverage.state === "current"
                    ? "The canonical Coverage Plan is already current."
                    : "Materialize a new append-only Coverage Plan revision from this exact Blueprint and plan head."
            }
          >
            {isMaterializingCoverage
              ? "Materializing Coverage…"
              : coverage.state === "missing"
                ? "Materialize Coverage Plan"
                : coverage.state === "current"
                  ? "Coverage Plan is current"
                  : "Refresh Coverage Plan"}
          </button>
        </div>
        <div>
          <span>Script coverage</span>
          <strong>{coverage.beat_count} {coverage.beat_count === 1 ? "Beat" : "Beats"}</strong>
          <small>One canonical Beat per Blueprint script block in the baseline planner.</small>
        </div>
        <div>
          <span>Private material coverage</span>
          <strong>{coverage.selected_assignment_count} selected · {coverage.gap_count} gaps</strong>
          <small>
            {timelineSummary.head === null
              ? "Read-only planning facts; no Timeline clips have been created."
              : `${timelineSummary.clip_count} canonical clips exist in the separate draft head.`}
          </small>
        </div>
      </section>

      <section className="blueprint-boundary" aria-labelledby="blueprint-capability-title">
        <div>
          <p className="eyebrow">Current implementation boundary</p>
          <h4 id="blueprint-capability-title">
            {timelineSummary.head !== null
              ? timelineSummary.state === "current"
                ? "A canonical hard-cut first cut exists as a draft."
                : "The preserved canonical first cut is stale and is not presented as current."
              : workspace.capabilities.timeline_materialization
                ? "Gap-free Coverage can be lowered into a deterministic first-cut draft."
                : coverage.state === "current" && coverage.gap_count > 0
                  ? "Honest Coverage gaps block Timeline lowering."
                  : "Coverage must be current before Timeline lowering."}
          </h4>
          <p>
            Confirmation records the creator's decisions. Coverage materialization can organize script Beats,
            selected private material, and honest gaps. The Timeline compiler only maps one exact selected assignment
            per Beat into silent hard cuts; it does not re-plan, fill gaps, add transitions, subtitles, or audio.
            Its output is a draft without ambient export authority. Canonical export is a separate native-user
            approval bound to one exact Timeline revision and the fixed export-1080p profile.
          </p>
          <p className="blueprint-server-reason">
            Server executable state: <strong>{workspace.executable.state.replaceAll("_", " ")}</strong>
            {` · ${workspace.executable.reason_code}`}
          </p>
        </div>
        <ul aria-label="Canonical creator pipeline capabilities">
          <li><span>Blueprint → Coverage Plan</span><strong>{workspace.capabilities.coverage_plan_materialization ? "Available" : "Unavailable"}</strong></li>
          <li><span>Blueprint → Timeline compiler</span><strong>{workspace.capabilities.blueprint_timeline_compiler ? "Available" : "Unavailable"}</strong></li>
          <li><span>Stale Timeline → Coverage reconciliation</span><strong>{workspace.capabilities.timeline_reconciliation ? "Available" : "Unavailable"}</strong></li>
          <li><span>Authoritative Timeline head</span><strong>{workspace.executable.current_timeline === null ? "None" : "Available"}</strong></li>
          <li><span>Preview / render</span><strong>{workspace.capabilities.preview || workspace.capabilities.render ? "Available" : "Unavailable"}</strong></li>
          <li><span>Canonical 1080p export approval</span><strong>{workspace.capabilities.canonical_export ? "Available" : workspace.canonical_export.state.replaceAll("_", " ")}</strong></li>
          <li><span>Social publish</span><strong>{workspace.capabilities.export ? "Available" : "Unavailable"}</strong></li>
        </ul>
      </section>

      <section className="blueprint-creative-core" aria-labelledby="coverage-plan-detail-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Canonical footage planning</p>
            <h4 id="coverage-plan-detail-title">Beats, assignments, alternatives, and honest gaps</h4>
          </div>
          <span>
            {coverageLoadState === "loading"
              ? "Loading…"
              : coveragePlan === null
                ? "No plan"
                : `Revision ${coveragePlan.revision}`}
          </span>
        </div>

        {isMaterializingCoverage ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Materializing one append-only Coverage revision from the exact Blueprint and Coverage heads…
          </p>
        ) : null}
        {coverageLoadState === "loading" ? (
          <p className="blueprint-empty" role="status" aria-live="polite">
            Loading the path-free canonical Coverage resource…
          </p>
        ) : null}
        {coverageError ? (
          <p className="video-inline-error" role="alert">
            Coverage details are unavailable and are not inferred from summary counts: {coverageError}
          </p>
        ) : null}
        {coverageLoadState === "loaded" && coveragePlan === null ? (
          <EmptyState>
            No Coverage Plan exists yet. Materialization will create Beats from the exact Blueprint script and report missing or unusable evidence as honest, non-blocking gaps.
          </EmptyState>
        ) : null}

        {coveragePlan !== null ? (
          <>
            <p className="blueprint-honesty-note">
              Baseline compiler: {coveragePlan.compiler.id}. Timing is estimated from text; semantic matching and global optimization are both disabled. These are planning facts, not Timeline clips.
            </p>
            <div className="blueprint-history-lanes" aria-label="Canonical Coverage Plan Beats">
              {coveragePlan.beats.map((beat) => {
                const displayedScript = coveragePlanUsesDisplayedScript
                  ? semantic.script.blocks.find((block) => block.block_id === beat.script_block_id)?.text ?? null
                  : null;
                return (
                  <details open={beat.ordinal === 0} key={beat.beat_id}>
                    <summary>
                      <span>{beat.ordinal + 1}</span>
                      <div>
                        <strong>Beat {beat.ordinal + 1} · {beat.script_block_id}</strong>
                        <small>
                          {formatDuration(beat.timing.end_ms - beat.timing.start_ms)} estimated slot
                          {` · ${beat.selected_assignments.length} selected`}
                          {` · ${beat.alternatives.length} alternatives`}
                        </small>
                      </div>
                      <em>{beat.gap === null ? "Covered" : "Honest gap"}</em>
                    </summary>
                    {displayedScript ? (
                      <p>{displayedScript}</p>
                    ) : (
                      <p>
                        Script text is not copied into Coverage. This Beat is bound by script block identity and digest
                        {coveragePlanUsesDisplayedScript
                          ? "."
                          : ` to Blueprint revision ${coveragePlan.blueprint_binding.revision}, not the displayed Blueprint.`}
                      </p>
                    )}
                    <Digest label={`Beat ${beat.ordinal + 1} text SHA-256`}>{beat.text_sha256}</Digest>

                    <div className="blueprint-evidence-grid">
                      <article>
                        <h5>Selected assignment</h5>
                        {beat.selected_assignments.length > 0 ? (
                          <ul className="blueprint-record-list">
                            {beat.selected_assignments.map((assignment) => {
                              const evidence = coverageEvidence(coveragePlan, assignment.evidence_ref);
                              return (
                                <li key={assignment.assignment_id}>
                                  <strong>
                                    {evidence?.proof?.kind === "span" ? "Verified video span" : "Verified image asset"}
                                  </strong>
                                  <code>{assignment.evidence_ref}</code>
                                  <small>
                                    Baseline match: {humanizeCoverageCode(assignment.match_type)} · exact Beat slot {formatDuration(assignment.timeline_duration_ms)} · unlocked
                                  </small>
                                  {evidence?.proof?.kind === "span" ? (
                                    <small>
                                      Frozen span {formatDuration(evidence.proof.start_ms)}–{formatDuration(evidence.proof.end_ms)} · analysis revision {evidence.proof.analysis_revision}
                                    </small>
                                  ) : null}
                                  <Digest label={`Assignment ${assignment.assignment_id} reason SHA-256`}>
                                    {assignment.reason_sha256}
                                  </Digest>
                                </li>
                              );
                            })}
                          </ul>
                        ) : <EmptyState>No material was selected for this Beat.</EmptyState>}
                      </article>

                      <article>
                        <h5>Alternatives</h5>
                        {beat.alternatives.length > 0 ? (
                          <ul className="blueprint-record-list">
                            {beat.alternatives.map((alternative) => {
                              const evidence = coverageEvidence(coveragePlan, alternative.evidence_ref);
                              return (
                                <li key={alternative.assignment_id}>
                                  <strong>{humanizeCoverageCode(alternative.not_selected_reason)}</strong>
                                  <code>{alternative.evidence_ref}</code>
                                  <small>
                                    Evidence {evidence?.status ?? "unknown"} · proposed slot {formatDuration(alternative.timeline_duration_ms)}
                                  </small>
                                </li>
                              );
                            })}
                          </ul>
                        ) : <EmptyState>No alternatives were declared for this Beat.</EmptyState>}
                      </article>
                    </div>

                    {beat.gap ? (
                      <p className="video-inline-warning" role="note">
                        Honest gap: <strong>{humanizeCoverageCode(beat.gap.code)}</strong>. This baseline gap is non-blocking and is not silently replaced with unrelated footage.
                      </p>
                    ) : null}
                  </details>
                );
              })}
            </div>
          </>
        ) : null}
      </section>

      <section className="blueprint-creative-core" aria-labelledby="canonical-timeline-detail-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Canonical first-cut draft</p>
            <h4 id="canonical-timeline-detail-title">Beat → clip hard-cut inspection</h4>
          </div>
          <span>
            {timelineLoadState === "loading"
              ? "Loading…"
              : canonicalTimeline === null
                ? timelinePresentation.label
                : `Draft revision ${canonicalTimeline.revision}`}
          </span>
        </div>

        <div className="blueprint-head-actions">
          <button
            className="primary-button compact-button"
            type="button"
            disabled={!canMaterializeTimeline || isBusy}
            onClick={() => void handleMaterializeTimeline()}
            title={
              timelineSummary.head !== null
                ? "An existing canonical Timeline is preserved and cannot be overwritten by the first-cut compiler."
                : !workspace.capabilities.timeline_materialization
                  ? "A current, gap-free Coverage Plan is required."
                  : "Materialize one deterministic silent hard-cut draft from the exact Blueprint and Coverage heads."
            }
          >
            {isMaterializingTimeline
              ? "Materializing first cut…"
              : timelineSummary.head === null
                ? "Materialize hard-cut draft"
                : "Draft head is preserved"}
          </button>
          {workspace.capabilities.timeline_reconciliation ? (
            <button
              className="primary-button compact-button"
              type="button"
              disabled={!canReconcileTimeline || isBusy}
              onClick={() => void handleReconcileTimeline()}
              title="Append one deterministic successor from the exact current Blueprint, Coverage Plan, and stale Timeline head."
            >
              {isReconcilingTimeline
                ? "Reconciling Timeline…"
                : "Reconcile stale Timeline"}
            </button>
          ) : null}
          <button
            className="secondary-button compact-button"
            type="button"
            disabled={isBusy || pendingTimelineEdit !== null || pendingTimelineRestore !== null}
            title={pendingTimelineEdit === null && pendingTimelineRestore === null
              ? "Refresh the exact canonical Timeline resource."
              : "Save or discard the pending Timeline change before refreshing."}
            onClick={() => void handleRefreshTimeline()}
          >
            Refresh Timeline resource
          </button>
        </div>

        <p className="blueprint-honesty-note">
          {timelinePresentation.detail} Lifecycle: <strong>{timelineSummary.lifecycle.state.replaceAll("_", " ")}</strong>
          {" · no ambient approval"}. Basic edits are staged locally and saved only as append-only canonical revisions. Server render and final-fidelity preview remain disabled; canonical export requires a fresh native approval for one exact saved revision and profile.
        </p>
        {isMaterializingTimeline ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Lowering exact selected Coverage assignments into one append-only silent hard-cut draft…
          </p>
        ) : null}
        {isReconcilingTimeline ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Appending an exact Coverage-derived successor while preserving the stale revision…
          </p>
        ) : null}
        {isApplyingTimelineEdit ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Applying one closed edit, appending revision N+1, then reopening the exact canonical resource…
          </p>
        ) : null}
        {isApplyingTimelineStructuralEdit ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Applying one closed Split/Delete command, appending revision N+1, then reopening the exact canonical Timeline…
          </p>
        ) : null}
        {isRestoringTimeline ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Re-enveloping the selected historical revision as N+1, then reopening the exact canonical resource…
          </p>
        ) : null}
        {timelineLoadState === "loading" ? (
          <p className="blueprint-empty" role="status" aria-live="polite">
            Loading the path-free canonical Timeline and its opaque source identities…
          </p>
        ) : null}
        {timelineError ? (
          <p className="video-inline-error" role="alert">
            Timeline details are unavailable and are not inferred from the project summary: {timelineError}
          </p>
        ) : null}
        {timelineLoadState === "loaded" && canonicalTimeline === null ? (
          <EmptyState>
            No canonical first-cut draft exists. Materialization is available only when every Coverage Beat has exactly one selected, currently resolvable assignment and no gap.
          </EmptyState>
        ) : null}
        {canonicalTimeline !== null && timelineWorkspace !== null ? (
          <>
            <div className="blueprint-head-contract" aria-label="Canonical Timeline draft contract">
              <div>
                <span>Output</span>
                <strong>{canonicalTimeline.output.aspect_ratio} · {formatDuration(canonicalTimeline.output.duration_ms)}</strong>
                <small>Hard cuts · silent · no subtitles</small>
              </div>
              <div>
                <span>Exact Timeline head</span>
                <strong>Revision {canonicalTimeline.revision}</strong>
                <Digest label="Timeline content SHA-256">
                  {timelineWorkspace.selected_revision?.timeline_content_sha256 ?? timelineSummary.head?.timeline_content_sha256 ?? ""}
                </Digest>
              </div>
              <div>
                <span>Lifecycle</span>
                <strong>Draft</strong>
                <small>Approval not established · native export approval required</small>
              </div>
            </div>
            {pendingTimelineEdit !== null ? (
              <div className="timeline-edit-pending">
                <div role="status" aria-live="polite">
                  <p className="eyebrow">Pending edit · not canonical</p>
                  <strong>{pendingTimelineEdit.summary}</strong>
                  <small>
                    Based on revision {timelineWorkspace.head?.revision ?? canonicalTimeline.revision}.
                    {pendingTimelineEdit.previewKind === "save_then_reread"
                      ? " Core must resolve the replacement source; no speculative media preview is shown."
                      : " The preview below is a local copy and cannot drive export or Usage."}
                  </small>
                </div>
                <div
                  className="timeline-edit-pending-actions"
                  role="group"
                  aria-label="Pending Timeline edit actions"
                >
                  <button
                    className="secondary-button compact-button"
                    type="button"
                    disabled={isBusy}
                    onClick={() => {
                      setPendingTimelineEdit(null);
                      setMessage("Pending Timeline edit discarded. The canonical head was not changed.");
                      setError(null);
                    }}
                  >
                    Discard
                  </button>
                  <button
                    className="primary-button compact-button"
                    type="button"
                    disabled={isBusy || !canSubmitPendingTimelineEdit}
                    onClick={() => void handleApplyTimelineEdit()}
                  >
                    {isApplyingTimelineEdit
                      ? "Saving and reopening…"
                      : `Save as revision ${(timelineWorkspace.head?.revision ?? canonicalTimeline.revision) + 1}`}
                  </button>
                </div>
              </div>
            ) : null}
            <CanonicalTimelinePreview
              apiBase={apiBase}
              workspace={timelineWorkspace}
              pendingPreview={pendingTimelineEdit?.preview ?? null}
              previewKey={pendingTimelineEdit === null
                ? ""
                : canonicalTimelinePendingEditKey(pendingTimelineEdit.edit)}
            />
            <section
              className="timeline-history-panel"
              aria-labelledby="timeline-history-title"
            >
              <div className="timeline-history-heading">
                <div>
                  <p className="eyebrow">Append-only Timeline history</p>
                  <h5 id="timeline-history-title">Inspect a prior revision</h5>
                  <small>
                    The current canonical authority remains revision {timelineWorkspace.head?.revision ?? canonicalTimeline.revision} while history is inspected read-only.
                  </small>
                </div>
                <div className="timeline-history-picker">
                  <label>
                    <small>Historical revision</small>
                    <input
                      type="number"
                      min={1}
                      max={(timelineWorkspace.head?.revision ?? 1) - 1}
                      step={1}
                      list="timeline-history-recent-revisions"
                      value={historicalTimelineRevision}
                      disabled={
                        isBusy
                        || historicalTimelineLoadState === "loading"
                        || pendingTimelineEdit !== null
                        || pendingTimelineRestore !== null
                        || historicalTimelineRevisionNumbers(timelineWorkspace.head).length === 0
                      }
                      onChange={(event) => {
                        setHistoricalTimelineRevision(event.currentTarget.value);
                        setHistoricalTimelineWorkspace(null);
                        setHistoricalTimelineLoadState("idle");
                        setHistoricalTimelineError(null);
                      }}
                    />
                    <datalist id="timeline-history-recent-revisions">
                      {historicalTimelineRevisionNumbers(timelineWorkspace.head).map((revision) => (
                        <option value={revision} key={revision}>Revision {revision}</option>
                      ))}
                    </datalist>
                  </label>
                  <button
                    className="secondary-button compact-button"
                    type="button"
                    disabled={
                      isBusy
                      || historicalTimelineLoadState === "loading"
                      || pendingTimelineEdit !== null
                      || pendingTimelineRestore !== null
                      || historicalTimelineRevisionNumbers(timelineWorkspace.head).length === 0
                    }
                    onClick={() => void handleInspectTimelineRevision()}
                  >
                    {historicalTimelineLoadState === "loading"
                      ? "Verifying revision…"
                      : "Inspect revision"}
                  </button>
                </div>
              </div>
              {historicalTimelineError !== null ? (
                <p className="video-inline-error" role="alert">
                  Historical revision unavailable: {historicalTimelineError}
                </p>
              ) : null}
              {historicalTimelineWorkspace !== null
                && historicalTimelineWorkspace.timeline !== null ? (
                  <div className="timeline-history-inspection">
                    <div className="timeline-history-selection">
                      <div>
                        <strong>
                          Historical revision {historicalTimelineWorkspace.selected_revision?.revision}
                        </strong>
                        <small>
                          Read-only inspection. Current canonical head: revision {timelineWorkspace.head?.revision}.
                        </small>
                      </div>
                      <button
                        className="primary-button compact-button"
                        type="button"
                        disabled={!canStageTimelineRestore || isBusy}
                        onClick={stageTimelineRestore}
                      >
                        Stage restore revision {historicalTimelineWorkspace.selected_revision?.revision}
                      </button>
                    </div>
                    <CanonicalTimelinePreview
                      apiBase={apiBase}
                      workspace={historicalTimelineWorkspace}
                      pendingPreview={null}
                      previewKey={`history-${historicalTimelineWorkspace.selected_revision?.revision ?? ""}`}
                    />
                  </div>
                ) : null}
              {pendingTimelineRestore !== null ? (
                <div className="timeline-edit-pending timeline-restore-pending">
                  <div role="status" aria-live="polite">
                    <p className="eyebrow">Pending restore · not canonical</p>
                    <strong>{pendingTimelineRestore.summary}</strong>
                    <small>
                      Historical bytes remain read-only. Saving appends N+1 with parent revision {pendingTimelineRestore.based_on_head.revision}; it does not overwrite either revision.
                    </small>
                  </div>
                  <div
                    className="timeline-edit-pending-actions"
                    role="group"
                    aria-label="Pending Timeline restore actions"
                  >
                    <button
                      className="secondary-button compact-button"
                      type="button"
                      disabled={isBusy}
                      onClick={() => {
                        setPendingTimelineRestore(null);
                        setMessage("Pending Timeline restore discarded. The canonical head was not changed.");
                        setError(null);
                      }}
                    >
                      Discard
                    </button>
                      <button
                        className="primary-button compact-button"
                        type="button"
                        disabled={isBusy || !canSubmitPendingTimelineRestore}
                      onClick={() => void handleRestoreTimelineRevision()}
                    >
                      {isRestoringTimeline
                        ? "Saving and reopening…"
                        : `Save revision ${pendingTimelineRestore.restore_from.revision} as ${pendingTimelineRestore.based_on_head.revision + 1}`}
                    </button>
                  </div>
                </div>
              ) : null}
            </section>
            <div className="blueprint-history-lanes" aria-label="Canonical Timeline clips">
              {canonicalTimeline.tracks[0].clips.map((clip, index) => {
                const source = timelineWorkspace.source_bindings[index];
                const draft = timelineClipEditDrafts[clip.clip_id] ?? {};
                const replacementCandidates = replacementCandidatesForClip(clip.clip_id);
                const editControlsDisabled = !canEditTimeline
                  || isBusy
                  || pendingTimelineEdit !== null
                  || pendingTimelineRestore !== null;
                return (
                  <details
                    open={expandedTimelineClipIds.has(clip.clip_id)}
                    key={clip.clip_id}
                    onToggle={(event) => {
                      const isOpen = event.currentTarget.open;
                      setExpandedTimelineClipIds((current) => {
                        const next = new Set(current);
                        if (isOpen) next.add(clip.clip_id);
                        else next.delete(clip.clip_id);
                        return next;
                      });
                    }}
                  >
                    <summary>
                      <span>{clip.ordinal + 1}</span>
                      <div>
                        <strong>Beat {clip.beat_id} → Clip {clip.clip_id}</strong>
                        <small>
                          {clip.media_kind} · {formatDuration(clip.end_ms - clip.start_ms)} · hard cut · silent
                        </small>
                      </div>
                      <em>Draft</em>
                    </summary>
                    <p><code>{clip.evidence_ref}</code></p>
                    <p>
                      Asset <code>{clip.asset_id}</code> is adopted by durable content identity.
                      Execution uses opaque source binding <code>{source?.asset_source_id ?? "unavailable"}</code>; no local path is canonicalized.
                    </p>
                    {clip.media_kind === "video" ? (
                      "residual_id" in clip ? (
                        <small>
                          Exact residual <code>{clip.residual_id}</code> · parent <code>{clip.parent_segment_id}</code>
                          {" · "}current source {formatDuration(clip.source_in_ms)}–{formatDuration(clip.source_out_ms)}
                          {" · "}frozen residual bound {formatDuration(clip.residual_binding.source_in_ms)}–{formatDuration(clip.residual_binding.source_out_ms)}
                          {" · "}analysis revision {clip.analysis_revision}
                        </small>
                      ) : (
                        <small>
                          Verified span {clip.span_id} · source {formatDuration(clip.source_in_ms)}–{formatDuration(clip.source_out_ms)} · analysis revision {clip.analysis_revision}
                        </small>
                      )
                    ) : (
                      <small>Image duration starts from the Coverage Beat slot and can be revised explicitly.</small>
                    )}
                    <div
                      className="timeline-edit-controls"
                      role="group"
                      aria-label={`Edit Beat ${clip.beat_id}, clip ${index + 1}`}
                    >
                      <div className="timeline-edit-control-group">
                        <span>Order</span>
                        <div>
                          <button
                            className="secondary-button compact-button"
                            type="button"
                            aria-label={`Move Beat ${clip.beat_id}, clip ${index + 1} earlier`}
                            disabled={editControlsDisabled || index === 0}
                            onClick={() => stageTimelineEdit({
                              op: "move_clip",
                              clip_id: clip.clip_id,
                              to_index: index - 1,
                            })}
                          >
                            Move earlier
                          </button>
                          <button
                            className="secondary-button compact-button"
                            type="button"
                            aria-label={`Move Beat ${clip.beat_id}, clip ${index + 1} later`}
                            disabled={editControlsDisabled
                              || index === canonicalTimeline.tracks[0].clips.length - 1}
                            onClick={() => stageTimelineEdit({
                              op: "move_clip",
                              clip_id: clip.clip_id,
                              to_index: index + 1,
                            })}
                          >
                            Move later
                          </button>
                        </div>
                      </div>

                      {clip.media_kind === "video" ? (
                        <>
                        <div className="timeline-edit-control-group">
                          <span>Trim verified span</span>
                          <div className="timeline-edit-number-row">
                            <label>
                              <small>Source in (ms)</small>
                              <input
                                type="number"
                                min={0}
                                step={100}
                                value={draft.sourceInMs ?? String(clip.source_in_ms)}
                                disabled={editControlsDisabled}
                                onChange={(event) => updateTimelineClipEditDraft(
                                  clip.clip_id,
                                  { sourceInMs: event.currentTarget.value },
                                )}
                              />
                            </label>
                            <label>
                              <small>Source out (ms)</small>
                              <input
                                type="number"
                                min={1}
                                step={100}
                                value={draft.sourceOutMs ?? String(clip.source_out_ms)}
                                disabled={editControlsDisabled}
                                onChange={(event) => updateTimelineClipEditDraft(
                                  clip.clip_id,
                                  { sourceOutMs: event.currentTarget.value },
                                )}
                              />
                            </label>
                            <button
                              className="primary-button compact-button"
                              type="button"
                              aria-label={`Stage trim for Beat ${clip.beat_id}, clip ${index + 1}`}
                              disabled={editControlsDisabled}
                              onClick={() => stageTimelineEdit({
                                op: "trim_clip",
                                clip_id: clip.clip_id,
                                source_in_ms: Number(draft.sourceInMs ?? clip.source_in_ms),
                                source_out_ms: Number(draft.sourceOutMs ?? clip.source_out_ms),
                              })}
                            >
                              Stage trim
                            </button>
                          </div>
                        </div>
                        <div className="timeline-edit-control-group">
                          <span>Structural cut</span>
                          <div className="timeline-edit-number-row">
                            <label>
                              <small>Split at source (ms)</small>
                              <input
                                type="number"
                                min={clip.source_in_ms + 1}
                                max={clip.source_out_ms - 1}
                                step={100}
                                value={draft.sourceSplitMs ?? String(
                                  Math.floor((clip.source_in_ms + clip.source_out_ms) / 2),
                                )}
                                disabled={editControlsDisabled || clip.source_out_ms - clip.source_in_ms < 2}
                                onChange={(event) => updateTimelineClipEditDraft(
                                  clip.clip_id,
                                  { sourceSplitMs: event.currentTarget.value },
                                )}
                              />
                            </label>
                            <button
                              className="primary-button compact-button"
                              type="button"
                              aria-label={`Split Beat ${clip.beat_id}, clip ${index + 1}`}
                              disabled={editControlsDisabled || clip.source_out_ms - clip.source_in_ms < 2}
                              onClick={() => void handleApplyTimelineStructuralEdit({
                                op: "split_clip",
                                clip_id: clip.clip_id,
                                source_split_ms: Number(draft.sourceSplitMs ?? Math.floor(
                                  (clip.source_in_ms + clip.source_out_ms) / 2,
                                )),
                              })}
                            >
                              Split clip
                            </button>
                          </div>
                        </div>
                        </>
                      ) : (
                        <div className="timeline-edit-control-group">
                          <span>Still duration</span>
                          <div className="timeline-edit-number-row">
                            <label>
                              <small>Duration (ms)</small>
                              <input
                                type="number"
                                min={1}
                                max={1_800_000}
                                step={100}
                                value={draft.durationMs ?? String(clip.end_ms - clip.start_ms)}
                                disabled={editControlsDisabled}
                                onChange={(event) => updateTimelineClipEditDraft(
                                  clip.clip_id,
                                  { durationMs: event.currentTarget.value },
                                )}
                              />
                            </label>
                            <button
                              className="primary-button compact-button"
                              type="button"
                              aria-label={`Stage duration for Beat ${clip.beat_id}, clip ${index + 1}`}
                              disabled={editControlsDisabled}
                              onClick={() => stageTimelineEdit({
                                op: "set_clip_duration",
                                clip_id: clip.clip_id,
                                duration_ms: Number(draft.durationMs ?? clip.end_ms - clip.start_ms),
                              })}
                            >
                              Stage duration
                            </button>
                          </div>
                        </div>
                      )}

                      <div className="timeline-edit-control-group">
                        <span>Same-Beat replacement</span>
                        {replacementCandidates.length > 0 ? (
                          <div className="timeline-edit-replace-row">
                            <label>
                              <small>Verified Coverage alternative</small>
                              <select
                                value={draft.replacementAssignmentId ?? ""}
                                disabled={editControlsDisabled}
                                onChange={(event) => updateTimelineClipEditDraft(
                                  clip.clip_id,
                                  { replacementAssignmentId: event.currentTarget.value },
                                )}
                              >
                                <option value="">Choose an alternative…</option>
                                {replacementCandidates.map((candidate) => (
                                  <option
                                    key={candidate.assignment_id}
                                    value={candidate.assignment_id}
                                  >
                                    {candidate.evidence_ref} · {formatDuration(candidate.timeline_duration_ms)}
                                  </option>
                                ))}
                              </select>
                            </label>
                            <button
                              className="primary-button compact-button"
                              type="button"
                              aria-label={`Stage replacement for Beat ${clip.beat_id}, clip ${index + 1}`}
                              disabled={editControlsDisabled || !draft.replacementAssignmentId}
                              onClick={() => stageTimelineEdit({
                                op: "replace_clip",
                                clip_id: clip.clip_id,
                                assignment_id: draft.replacementAssignmentId ?? "",
                              })}
                            >
                              Stage replacement
                            </button>
                          </div>
                        ) : (
                          <small>No verified, long-enough alternative unused in the current Timeline is available for this Beat.</small>
                        )}
                      </div>
                      <div className="timeline-edit-control-group">
                        <span>Remove from this cut</span>
                        <div>
                          <button
                            className="danger-text"
                            type="button"
                            aria-label={`Delete Beat ${clip.beat_id}, clip ${index + 1}`}
                            disabled={editControlsDisabled || canonicalTimeline.tracks[0].clips.length <= 1}
                            onClick={() => void handleApplyTimelineStructuralEdit({
                              op: "delete_clip",
                              clip_id: clip.clip_id,
                            })}
                          >
                            Delete clip
                          </button>
                        </div>
                      </div>
                    </div>
                  </details>
                );
              })}
            </div>
          </>
        ) : null}

        <div className="blueprint-head-contract" aria-label="Canonical export approval">
          <div>
            <span>Exact native egress</span>
            <strong>1080p · hard cuts · silent</strong>
            <small>
              Final video · exact Blueprint script · package manifest · human usage list · completion marker.
              Subtitles and cover are explicitly absent; this is not social publishing.
            </small>
          </div>
          <div>
            <span>Server export state</span>
            <strong>{workspace.canonical_export.state.replaceAll("_", " ")}</strong>
            <small>
              {workspace.canonical_export.reason_code === "export_recovery_required"
                ? "Restart MemoLens to reconcile the previous export outcome before exporting again."
                : workspace.canonical_export.reason_code.replaceAll("_", " ")}
            </small>
          </div>
          <div>
            <button
              className="primary-button compact-button"
              type="button"
              disabled={!canApproveCanonicalExport || isBusy}
              onClick={() => void handleApproveCanonicalExport()}
              title={
                !hasCanonicalExportBridge
                  ? "Use the trusted MemoLens desktop app for native export approval."
                  : workspace.canonical_export.reason_code === "export_recovery_required"
                    ? "The previous export outcome requires recovery. Restart MemoLens to run the activation recovery gate before exporting again."
                    : timelineSummary.state === "missing"
                      ? "A canonical Timeline is required."
                      : timelineSummary.state !== "current"
                        ? "The preserved Timeline is stale and cannot be approved for export."
                        : workspace.canonical_export.state === "in_progress" || localExportInProgress
                          ? "A canonical export job is already in progress."
                          : "Choose a local parent folder and approve this exact Timeline revision with the export-1080p profile."
              }
            >
              {isApprovingExport
                ? "Opening native approval…"
                : workspace.canonical_export.state === "in_progress" || localExportInProgress
                  ? "Canonical export in progress"
                  : "Approve this revision and export…"}
            </button>
            <small>Destination selection stays in the main process; MemoLens never overwrites by default.</small>
          </div>
        </div>
        <p className="blueprint-honesty-note">
          Preview and Save As do not mark source material as used. Usage is finalized only when an exact canonical
          export package succeeds; cancelling native approval creates no export job.
        </p>
        {exportPollState.state === "polling" ? (
          <p className="video-inline-note" role="status" aria-live="polite">
            Automatically verifying canonical export
            {exportPollState.jobId === null ? " outcome" : <> job <code>{exportPollState.jobId}</code></>}.
            The poll is bounded and will stop automatically; Refresh canonical head remains available.
          </p>
        ) : exportPollState.state === "unknown" ? (
          <p className="video-inline-note" role="status" aria-live="assertive">
            <strong>Export outcome unknown.</strong> {exportPollState.detail}
          </p>
        ) : exportPollState.state === "terminal" ? (
          <p
            className="video-inline-note"
            role="status"
            aria-live={canonicalExportJobPresentation(exportPollState.status).assertive ? "assertive" : "polite"}
          >
            Automatic verification reached terminal for job <code>{exportPollState.jobId}</code>.
          </p>
        ) : null}
        {exportResult?.status === "submitted" ? (
          <p
            className="video-inline-note"
            role="status"
            aria-live={localCanonicalExportPresentation?.assertive ? "assertive" : "polite"}
          >
            <strong>{localCanonicalExportPresentation?.label}</strong>
            {" · job "}<code>{exportResult.job.jobId}</code>
            {" · package "}<code>{exportResult.job.packageBasename}</code>. {localCanonicalExportPresentation?.detail}
            {" No absolute destination is exposed here."}
          </p>
        ) : exportResult?.status === "unknown" ? (
          <p className="video-inline-note" role="status" aria-live="assertive">
            Export outcome verification is pending. MemoLens is automatically polling the canonical workspace;
            manual Refresh canonical head remains available.
          </p>
        ) : workspace.canonical_export.latest_job !== null ? (
          <p
            className="video-inline-note"
            role="status"
            aria-live={latestCanonicalExportPresentation?.assertive ? "assertive" : "polite"}
          >
            <strong>{latestCanonicalExportPresentation?.label}</strong>
            {" · job "}<code>{workspace.canonical_export.latest_job.job_id}</code>
            {" · package "}<code>{workspace.canonical_export.latest_job.package_basename}</code>.
            {` ${latestCanonicalExportPresentation?.detail ?? ""} No absolute destination is exposed here.`}
          </p>
        ) : null}
      </section>

      <section className="blueprint-authority" aria-labelledby="blueprint-authority-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Creator decision authority</p>
            <h4 id="blueprint-authority-title">{confirmedCount} / 8 decisions confirmed</h4>
          </div>
          <span className={`blueprint-authority-state state-${authority.state}`}>
            {authority.state.replace("_", " ")}
          </span>
        </div>
        <div className="blueprint-authority-grid">
          {BLUEPRINT_DECISION_UNITS.map((name) => {
            const unit = authority.decision_units[name];
            return (
              <article className={unit.confirmed ? "confirmed" : "unverified"} key={name}>
                <span aria-hidden="true">{unit.confirmed ? "✓" : "○"}</span>
                <div>
                  <strong>{AUTHORITY_LABELS[name]}</strong>
                  <small>{unit.confirmed ? "Creator confirmed" : "Unverified proposal"}</small>
                </div>
              </article>
            );
          })}
        </div>
        <p className="blueprint-honesty-note">
          Authority is projected at {headStatus === "validated" ? "the exact head" : "the displayed snapshot"} revision {authority.as_of_revision}. Confirmation is not edit,
          compile, render, export, or publication permission.
        </p>
      </section>

      <section className="blueprint-creative-core" aria-labelledby="blueprint-core-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Creative proposal</p>
            <h4 id="blueprint-core-title">What this project is trying to say</h4>
          </div>
          <span>{semantic.script.blocks.length} script blocks</span>
        </div>

        <div className="blueprint-intent-grid">
          <article><span>Intent</span><strong>{displayValue(semantic.intent.goal)}</strong></article>
          <article><span>Creator stance</span><strong>{displayValue(semantic.intent.stance)}</strong></article>
          <article><span>Audience</span><strong>{displayValue(semantic.intent.audience)}</strong></article>
          <article><span>Platform</span><strong>{displayValue(semantic.intent.platform)}</strong></article>
        </div>

        <div className="blueprint-content-grid">
          <article className="blueprint-script-card">
            <h5>Script</h5>
            {semantic.script.blocks.length > 0 ? (
              <ol>
                {semantic.script.blocks.map((block) => (
                  <li key={block.block_id}>
                    <code>{block.block_id}</code>
                    <p>{block.text}</p>
                  </li>
                ))}
              </ol>
            ) : <EmptyState>No script blocks are present at this head.</EmptyState>}
          </article>

          <div className="blueprint-direction-stack">
            <article>
              <h5>Creative direction</h5>
              <dl>
                <div><dt>Theme</dt><dd>{displayValue(semantic.direction.theme)}</dd></div>
                <div><dt>Arc</dt><dd>{displayValue(semantic.direction.narrative_arc)}</dd></div>
                <div><dt>Emotion</dt><dd>{displayValue(semantic.direction.emotion)}</dd></div>
                <div><dt>Tone</dt><dd>{displayValue(semantic.direction.tone)}</dd></div>
                <div><dt>Pace</dt><dd>{displayValue(semantic.direction.pace)}</dd></div>
              </dl>
            </article>
            <article>
              <h5>Intended output</h5>
              <dl>
                <div><dt>Aspect</dt><dd>{semantic.output.aspect_ratio ?? "Not decided"}</dd></div>
                <div><dt>Target duration</dt><dd>{formatDuration(semantic.output.duration_target_ms)}</dd></div>
              </dl>
              <small>
                Intent only. {timelineSummary.head === null
                  ? "No canonical Timeline draft or render exists."
                  : "A separate canonical Timeline draft exists; no render or approval is implied."}
              </small>
            </article>
          </div>
        </div>

        <details className="blueprint-detail-block blueprint-proposal-details">
          <summary>Review references, techniques, pinned context, and assumptions</summary>
          <div className="blueprint-evidence-grid">
            <article>
              <h5>References</h5>
              {semantic.reference_refs.length > 0 ? (
                <ul className="blueprint-record-list">
                  {semantic.reference_refs.map((reference) => (
                    <li key={reference.reference_id}>
                      <strong>{reference.kind.replaceAll("_", " ")}</strong>
                      <code>{reference.locator}</code>
                      {reference.note ? <small>{reference.note}</small> : null}
                    </li>
                  ))}
                </ul>
              ) : <EmptyState>No references are declared.</EmptyState>}
            </article>

            <article>
              <h5>Technique cards</h5>
              {semantic.technique_refs.length > 0 ? (
                <ul className="blueprint-record-list">
                  {semantic.technique_refs.map((technique) => (
                    <li key={`${technique.card_id}-${technique.revision}`}>
                      <strong>{technique.card_id} · revision {technique.revision}</strong>
                      <small>Declared state: {technique.declared_state}</small>
                    </li>
                  ))}
                </ul>
              ) : <EmptyState>No technique cards are declared.</EmptyState>}
            </article>

            <article>
              <h5>Pinned context</h5>
              <div className="blueprint-binding-stack">
                <div>
                  <strong>Creator context</strong>
                  {semantic.bindings.creator_context
                    ? <pre>{JSON.stringify(semantic.bindings.creator_context, null, 2)}</pre>
                    : <small>Not pinned</small>}
                </div>
                <div>
                  <strong>Wiki generation</strong>
                  {semantic.bindings.wiki_generation
                    ? <pre>{JSON.stringify(semantic.bindings.wiki_generation, null, 2)}</pre>
                    : <small>Not pinned</small>}
                </div>
              </div>
            </article>

            <article>
              <h5>Assumptions</h5>
              {semantic.assumptions.length > 0 ? (
                <ul className="blueprint-gap-list">
                  {semantic.assumptions.map((assumption) => (
                    <li key={assumption.assumption_id}>
                      <strong>{assumption.assumption_id}</strong>
                      <small>{assumption.text}</small>
                    </li>
                  ))}
                </ul>
              ) : <EmptyState>No assumptions are declared.</EmptyState>}
            </article>
          </div>
        </details>
      </section>

      <section className="blueprint-evidence" aria-labelledby="blueprint-evidence-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Grounding &amp; unresolved work</p>
            <h4 id="blueprint-evidence-title">{verifiedEvidence} / {blueprint.evidence_manifest.length} evidence references verified</h4>
          </div>
          <span>{semantic.missing_evidence.length + semantic.open_decisions.length} declared gaps</span>
        </div>

        <div className="blueprint-evidence-grid">
          <article>
            <h5>Evidence manifest</h5>
            {blueprint.evidence_manifest.length > 0 ? (
              <ul className="blueprint-record-list">
                {blueprint.evidence_manifest.map((item) => (
                  <li key={item.evidence_ref}>
                    <span className={`blueprint-record-state ${item.status}`}>{item.status}</span>
                    <code>{item.evidence_ref}</code>
                    {item.proof_sha256 ? <Digest label={`Proof SHA-256 for ${item.evidence_ref}`}>{item.proof_sha256}</Digest> : <small>No proof digest</small>}
                  </li>
                ))}
              </ul>
            ) : <EmptyState>No evidence references are declared.</EmptyState>}
          </article>

          <article>
            <h5>Material hints</h5>
            {semantic.material_hints.length > 0 ? (
              <ul className="blueprint-record-list">
                {semantic.material_hints.map((hint) => (
                  <li key={hint.hint_id}>
                    <strong>{hint.hint_id}</strong>
                    <code>{hint.evidence_ref}</code>
                    <small>
                      Script: {hint.script_block_ids.join(", ") || "not assigned"}
                      {hint.reason ? ` · ${hint.reason}` : ""}
                    </small>
                  </li>
                ))}
              </ul>
            ) : <EmptyState>No material hints have been proposed.</EmptyState>}
          </article>

          <article>
            <h5>Missing evidence</h5>
            {semantic.missing_evidence.length > 0 ? (
              <ul className="blueprint-gap-list">
                {semantic.missing_evidence.map((gap) => (
                  <li key={gap.gap_id}>
                    <strong>{gap.description}</strong>
                    <small>Required before: {gap.required_before} · Script: {gap.script_block_ids.join(", ") || "project-wide"}</small>
                  </li>
                ))}
              </ul>
            ) : <EmptyState>No missing evidence is declared.</EmptyState>}
          </article>

          <article>
            <h5>Open decisions</h5>
            {semantic.open_decisions.length > 0 ? (
              <ul className="blueprint-gap-list">
                {semantic.open_decisions.map((decision) => (
                  <li key={decision.decision_id}>
                    <strong>{decision.question}</strong>
                    <small>{decision.scope} · Required before: {decision.required_before}</small>
                  </li>
                ))}
              </ul>
            ) : <EmptyState>No open decisions are declared.</EmptyState>}
          </article>
        </div>

        {(semantic.constraints.must_include.length > 0 || semantic.constraints.must_exclude.length > 0) ? (
          <details className="blueprint-detail-block">
            <summary>Review declared material constraints</summary>
            <div className="blueprint-constraint-grid">
              <div>
                <h5>Must include</h5>
                {semantic.constraints.must_include.map((constraint) => (
                  <p key={constraint.constraint_id}>{constraint.text ?? constraint.evidence_ref ?? constraint.constraint_id}</p>
                ))}
              </div>
              <div>
                <h5>Must exclude</h5>
                {semantic.constraints.must_exclude.map((constraint) => (
                  <p key={constraint.constraint_id}>{constraint.text ?? constraint.evidence_ref ?? constraint.constraint_id}</p>
                ))}
              </div>
            </div>
          </details>
        ) : null}
      </section>

      <section className="blueprint-revision-tools" aria-labelledby="blueprint-revisions-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Typed revision tools</p>
            <h4 id="blueprint-revisions-title">Compare exact revisions. Restore by appending.</h4>
          </div>
          <span>{workspace.history.blueprint.available_revision_count} revisions available</span>
        </div>

        <div className="blueprint-tool-grid">
          <form onSubmit={(event) => void handleCompare(event)}>
            <h5>Section-aware comparison</h5>
            <p>Both immutable revision resources are fetched before their typed semantic sections are compared.</p>
            <div className="blueprint-revision-inputs">
              <label>
                From revision
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={compareFrom}
                  onChange={(event) => setCompareFrom(event.target.value)}
                />
              </label>
              <label>
                To revision
                <input
                  type="number"
                  min="1"
                  step="1"
                  value={compareTo}
                  onChange={(event) => setCompareTo(event.target.value)}
                />
              </label>
            </div>
            <button className="secondary-button" type="submit" disabled={isBusy}>
              {isComparing ? "Fetching exact revisions…" : "Compare revisions"}
            </button>
          </form>

          <form onSubmit={(event) => void handleReviewRestore(event)}>
            <h5>CAS append-only restore</h5>
            <p>
              Stage 1 only fetches the target digest and builds a current-to-target 12-section review. No write occurs
              until the separate confirmation in stage 2.
            </p>
            <label>
              Revision to restore
              <input
                type="number"
                min="1"
                step="1"
                value={restoreFrom}
                onChange={(event) => {
                  setRestoreFrom(event.target.value);
                  setRestoreReview(null);
                }}
                disabled={!canRestore || isBusy}
              />
            </label>
            <button className="secondary-button" type="submit" disabled={!canRestore || isBusy}>
              {isReviewingRestore ? "Fetching exact target…" : "Review restore changes"}
            </button>
            {!canWrite ? <small>Read-only mode: comparison is available; restore is disabled.</small> : null}
            {canWrite && headStatus !== "validated" ? <small>Restore is disabled until canonical refresh succeeds.</small> : null}
          </form>
        </div>

        {comparison ? <RevisionComparisonView comparison={comparison} label="Exact Blueprint revision comparison" /> : null}

        {restoreReview ? (
          <div className="blueprint-restore-review">
            <div>
              <p className="eyebrow">Stage 2 · explicit write confirmation</p>
              <h5>Review current revision {restoreReview.before.revision} → target revision {restoreReview.after.revision}</h5>
              <p>
                Confirming will append a new revision with the target semantics. The current, target, and intervening
                revisions remain immutable. A moved head causes a 409 stop and refresh; it is never auto-replayed.
              </p>
            </div>
            <RevisionComparisonView comparison={restoreReview} label="Append-only restore section review" />
            <button
              className="primary-button"
              type="button"
              disabled={!canRestore || isBusy}
              onClick={() => void handleConfirmRestore()}
            >
              {isRestoring ? "Applying CAS restore…" : `Confirm append-only restore of revision ${restoreReview.after.revision}`}
            </button>
          </div>
        ) : null}

        {message ? <p className="video-inline-note" role="status" aria-live="polite">{message}</p> : null}
        {error ? <p className="video-inline-error" role="alert">{error}</p> : null}
      </section>

      <section className="blueprint-history" aria-labelledby="blueprint-history-title">
        <div className="blueprint-section-heading">
          <div>
            <p className="eyebrow">Scoped history</p>
            <h4 id="blueprint-history-title">Three honest lanes, not one invented ledger</h4>
          </div>
          <span>Read-only projections</span>
        </div>

        <div className="blueprint-history-contract" aria-label="Server-declared history scope">
          <span><strong>Complete project history</strong>{workspace.history.complete_project_history ? "Yes" : "No"}</span>
          <span><strong>Global total order</strong>{workspace.history.global_total_order ? "Yes" : "No"}</span>
          <span><strong>Replay scope</strong>{workspace.history.replay_scope.replaceAll("_", " ")}</span>
        </div>

        <div className="blueprint-history-lanes">
          <details open>
            <summary>
              <span>1</span>
              <div><strong>Blueprint operations</strong><small>Creative Blueprint scope only</small></div>
              <em>{workspace.history.blueprint.operations.length}</em>
            </summary>
            <p>
              Append-only proposal and restore operations. This lane explicitly does not claim complete project history.
              {` Server order: ${workspace.history.blueprint.order.replaceAll("_", " ")}.`}
              {workspace.history.blueprint.operations_truncated ? " The visible window is truncated." : ""}
            </p>
            {workspace.history.blueprint.operations.length > 0 ? (
              <ol>
                {workspace.history.blueprint.operations.map((operation) => (
                  <li key={operation.operation_id}>
                    <div>
                      <strong>#{operation.sequence} · {operation.intent_code.replaceAll("_", " ")}</strong>
                      <time dateTime={operation.created_at}>{formatDate(operation.created_at)}</time>
                    </div>
                    <span>Result: revision {operation.result_revision} · {operation.result_kind.replaceAll("_", " ")}</span>
                    <span>Sections: {operation.changed_sections.map((section) => SECTION_LABELS[section]).join(", ") || "none"}</span>
                    {operation.restore_from ? <span>Restored from exact revision {operation.restore_from.revision}</span> : null}
                    <Digest label={`Operation ${operation.sequence} result SHA-256`}>{operation.result_content_sha256}</Digest>
                  </li>
                ))}
              </ol>
            ) : <EmptyState>No Blueprint operations are visible.</EmptyState>}
          </details>

          <details>
            <summary>
              <span>2</span>
              <div><strong>Decision authority events</strong><small>Confirmation / revocation scope only</small></div>
              <em>{workspace.history.decision_authority.available_event_count}</em>
            </summary>
            <p>
              This separate lane explains the displayed {confirmedCount}/8 projection. It does not mutate Blueprint content
              and does not claim complete project history. Server order: {workspace.history.decision_authority.order.replaceAll("_", " ")}.
            </p>
            {workspace.history.decision_authority.events.length > 0 ? (
              <ol>
                {workspace.history.decision_authority.events.map((event) => (
                  <li key={event.event_id}>
                    <div>
                      <strong>#{event.sequence} · {event.authority_operation}</strong>
                      <time dateTime={event.created_at}>{formatDate(event.created_at)}</time>
                    </div>
                    <span>Observed Blueprint revision {event.observed_revision}</span>
                    <span>{event.decision_units.map((unit) => AUTHORITY_LABELS[unit]).join(", ")}</span>
                  </li>
                ))}
              </ol>
            ) : <EmptyState>No decision authority events are visible.</EmptyState>}
            {workspace.history.decision_authority.events_truncated ? <small>Visible decision authority history is truncated.</small> : null}
          </details>

          <details>
            <summary>
              <span>3</span>
              <div><strong>Legacy artifacts</strong><small>Migration / historical context only</small></div>
              <em>
                {workspace.history.legacy_artifacts.status === "available"
                  ? workspace.history.legacy_artifacts.brief_count + workspace.history.legacy_artifacts.timeline_revision_count
                  : "unavailable"}
              </em>
            </summary>
            <p>
              Legacy briefs are migration context; legacy Timelines are observed historical artifacts. Neither is an
              authoritative Timeline head for this canonical Blueprint workspace. Server order: {workspace.history.legacy_artifacts.order.replaceAll("_", " ")}.
            </p>
            {workspace.history.legacy_artifacts.status === "unavailable" ? (
              <p className="video-inline-warning" role="status">
                Legacy artifact history is unavailable and is not interpreted as empty.
                {workspace.history.legacy_artifacts.reason_code
                  ? ` Reason: ${workspace.history.legacy_artifacts.reason_code}.`
                  : " No reason code was supplied."}
              </p>
            ) : (
              <div className="blueprint-legacy-columns">
                <div>
                  <h5>Briefs ({workspace.history.legacy_artifacts.brief_count})</h5>
                  {workspace.history.legacy_artifacts.briefs.map((brief) => (
                    <article key={`${brief.revision}-${brief.content_sha256}`}>
                      <strong>Revision {brief.revision}</strong>
                      <time dateTime={brief.created_at}>{formatDate(brief.created_at)}</time>
                      <Digest label={`Legacy brief ${brief.revision} SHA-256`}>{brief.content_sha256}</Digest>
                    </article>
                  ))}
                  {workspace.history.legacy_artifacts.briefs.length === 0 ? <EmptyState>No legacy briefs exist in the available projection.</EmptyState> : null}
                  {workspace.history.legacy_artifacts.briefs_truncated ? <small>Visible brief history is truncated.</small> : null}
                </div>
                <div>
                  <h5>Timeline revisions ({workspace.history.legacy_artifacts.timeline_revision_count})</h5>
                  {workspace.history.legacy_artifacts.timelines.map((timeline) => (
                    <article key={`${timeline.timeline_id}-${timeline.revision}`}>
                      <strong>{timeline.timeline_id} · revision {timeline.revision}</strong>
                      <span>{timeline.validation_status} · schema {timeline.schema_version}</span>
                      <time dateTime={timeline.created_at}>{formatDate(timeline.created_at)}</time>
                      <Digest label={`Legacy Timeline ${timeline.timeline_id} revision ${timeline.revision} SHA-256`}>{timeline.content_sha256}</Digest>
                    </article>
                  ))}
                  {workspace.history.legacy_artifacts.timelines.length === 0 ? <EmptyState>No legacy Timeline revisions exist in the available projection.</EmptyState> : null}
                  {workspace.history.legacy_artifacts.timelines_truncated ? <small>Visible Timeline history is truncated.</small> : null}
                </div>
              </div>
            )}
          </details>
        </div>
      </section>
    </section>
  );
}
