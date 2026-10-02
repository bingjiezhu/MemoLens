import { isBlueprintProjectWorkspace } from "../blueprint/workspaceModel";
import type { BlueprintProjectWorkspace } from "../blueprint/workspaceTypes";
import { fetchCreativeProject } from "./api/creative";
import { fetchTimeline } from "./api/timeline";
import { persistVideoSession } from "./session";
import type { PersistedVideoSession, VideoSessionStorage } from "./session";
import type { CreativeProject, CreativeTimeline } from "./types";

export interface VideoProjectOpenScope {
  scopeKey: string;
  scopeEpoch: number;
  apiBase: string;
  dbPath: string;
}

export interface VideoProjectOpenRequest extends VideoProjectOpenScope {
  requestId: number;
  projectId: string;
}

export interface VideoProjectRequestIdentity extends VideoProjectOpenScope {
  requestId: number;
  projectId: string | null;
  timelineId: string | null;
  timelineRevision: number | null;
  renderJobId?: string | null;
}

export function canApplyVideoProjectResponse(
  request: VideoProjectRequestIdentity,
  current: VideoProjectRequestIdentity,
): boolean {
  return request.scopeKey === current.scopeKey
    && request.scopeEpoch === current.scopeEpoch
    && request.apiBase === current.apiBase
    && request.dbPath === current.dbPath
    && request.requestId === current.requestId
    && request.projectId === current.projectId
    && request.timelineId === current.timelineId
    && request.timelineRevision === current.timelineRevision
    && (request.renderJobId === undefined || request.renderJobId === current.renderJobId);
}

export type OpenedVideoProject = {
  request: VideoProjectOpenRequest;
  session: PersistedVideoSession;
} & (
  | { kind: "canonical"; workspace: BlueprintProjectWorkspace }
  | { kind: "legacy"; project: CreativeProject; timeline: CreativeTimeline | null }
);

export function canAdoptOpenedVideoProject(
  request: VideoProjectOpenRequest,
  current: VideoProjectOpenScope & { requestId: number },
): boolean {
  return request.scopeKey === current.scopeKey
    && request.scopeEpoch === current.scopeEpoch
    && request.apiBase === current.apiBase
    && request.dbPath === current.dbPath
    && request.requestId === current.requestId;
}

/** Read both legacy resources before switching the visible project or its session. */
export async function loadExistingVideoProject(input: {
  request: VideoProjectOpenRequest;
  signal: AbortSignal;
  isCurrent: () => boolean;
  savedSession?: PersistedVideoSession | null;
  expectedDatabaseUuid?: string | null;
  canonicalOnly?: boolean;
}, readers = { fetchCreativeProject, fetchTimeline }): Promise<OpenedVideoProject | null> {
  const { request, signal } = input;
  const isCurrent = () => !signal.aborted && input.isCurrent();
  if (!request.projectId.trim() || !request.dbPath.trim()) {
    throw new Error("Choose a library and enter an exact Project ID.");
  }
  if (!isCurrent()) return null;
  const project = await readers.fetchCreativeProject(
    request.apiBase, request.projectId, request.dbPath, signal,
  );
  if (!isCurrent()) return null;
  if (project.id !== request.projectId) {
    throw new Error("The returned project does not match the requested Project ID.");
  }
  if (isBlueprintProjectWorkspace(project)) {
    if (
      project.project_id !== request.projectId
      || project.current_blueprint.project_id !== request.projectId
      || !project.database_uuid
      || project.current_blueprint.database_uuid !== project.database_uuid
      || (input.expectedDatabaseUuid && project.database_uuid !== input.expectedDatabaseUuid)
    ) {
      throw new Error("The returned Blueprint does not match this library and project.");
    }
    return {
      kind: "canonical",
      request,
      workspace: project,
      session: { projectId: request.projectId, timelineId: null, timelineRevision: null },
    };
  }
  if (input.canonicalOnly) {
    throw new Error("The canonical Blueprint identity disappeared. MemoLens will not fall back to the legacy brief automatically.");
  }
  const saved = input.savedSession?.projectId === request.projectId ? input.savedSession : null;
  const timelineId = project.latest_timeline_id ?? saved?.timelineId ?? null;
  const revision = project.latest_timeline_id
    ? project.latest_timeline_revision
    : saved?.timelineRevision;
  const timeline = timelineId
    ? await readers.fetchTimeline(request.apiBase, timelineId, revision, request.dbPath, signal)
    : null;
  if (!isCurrent()) return null;
  if (timeline && (
    timeline.id !== timelineId
    || timeline.project_id !== request.projectId
    || (revision != null && timeline.revision !== revision)
  )) {
    throw new Error("The returned timeline does not match the requested project and revision.");
  }
  return {
    kind: "legacy",
    request,
    project,
    timeline,
    session: {
      projectId: request.projectId,
      timelineId: timeline?.id ?? null,
      timelineRevision: timeline?.revision ?? null,
    },
  };
}

export function persistOpenedVideoProject(
  storage: VideoSessionStorage,
  opened: OpenedVideoProject,
  current: VideoProjectOpenScope & { requestId: number },
): boolean {
  if (!canAdoptOpenedVideoProject(opened.request, current)) return false;
  persistVideoSession(storage, opened.request.scopeKey, opened.session);
  return true;
}
