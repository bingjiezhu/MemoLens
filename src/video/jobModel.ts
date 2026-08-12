import type { CreativeTimeline, MediaJob } from "./types";


const ACTIVE_STATUSES: ReadonlySet<string> = new Set(["queued", "running", "cancelling"]);
const CANCELLABLE_STATUSES: ReadonlySet<string> = new Set(["queued", "running"]);
const USABLE_STATUSES: ReadonlySet<string> = new Set(["succeeded", "completed", "partial"]);
const SUCCESSFUL_RENDER_STATUSES: ReadonlySet<string> = new Set(["succeeded", "completed"]);

export interface MediaJobSummary {
  progress: number;
  active: number;
  failed: number;
  completed: number;
}

export function isActiveJobStatus(status: string): boolean {
  return ACTIVE_STATUSES.has(status);
}

export function isCancellableJobStatus(status: string): boolean {
  return CANCELLABLE_STATUSES.has(status);
}

export function isUsableJobStatus(status: string): boolean {
  return USABLE_STATUSES.has(status);
}

export function isSuccessfulRenderStatus(status: string): boolean {
  return SUCCESSFUL_RENDER_STATUSES.has(status);
}

export function summarizeMediaJobs(jobs: readonly MediaJob[]): MediaJobSummary {
  if (jobs.length === 0) {
    return { progress: 0, active: 0, failed: 0, completed: 0 };
  }
  return {
    progress: Math.round(jobs.reduce((total, job) => total + job.progress, 0) / jobs.length),
    active: jobs.filter((job) => isActiveJobStatus(job.status)).length,
    failed: jobs.filter((job) => job.status === "failed").length,
    completed: jobs.filter((job) => isUsableJobStatus(job.status)).length,
  };
}

export function formatJobStage(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function formatMediaScore(value: number): string {
  const percentage = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return `${percentage}%`;
}

export function defaultPreviewFilename(timeline: CreativeTimeline): string {
  return `memolens-${timeline.project_id}-r${timeline.revision}.mp4`;
}
