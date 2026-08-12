import { readdir } from "node:fs/promises";
import { extname, join, relative, resolve, sep } from "node:path";

import type {
  DesktopIndexingPhase,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
} from "../src/query/types.js";

const SUPPORTED_IMAGE_EXTENSIONS = new Set([
  ".jpg",
  ".jpeg",
  ".png",
  ".webp",
  ".bmp",
  ".gif",
  ".tif",
  ".tiff",
  ".heic",
  ".heif",
]);

export const LOCAL_INDEX_BATCH_SIZE = 6;
const PROGRESS_CHANNEL = "memolens:indexing-progress";

export interface IndexingProgressSender {
  isDestroyed(): boolean;
  send(channel: string, progress: DesktopIndexingProgress): void;
}

export interface ImageBatchRequest {
  apiBase: string;
  filePaths: string[];
  rootPath: string;
  model: string | null;
  dbPath: string;
  reindex: boolean;
}

export interface ImageBatchResult {
  indexed: number;
  skipped: number;
  failed: number;
  errors: string[];
}

export type ImageBatchAnalyzer = (request: ImageBatchRequest) => Promise<ImageBatchResult>;

interface HttpBatchDependencies {
  getSessionToken: () => string;
  fetch?: typeof fetch;
}

export interface DesktopIndexingCoordinatorOptions {
  apiBase: string;
  getSessionToken: () => string;
  resolveDbPath: (folderPath: string) => string;
  collectImageFiles?: (folderPath: string) => Promise<string[]>;
  analyzeImageBatch?: ImageBatchAnalyzer;
  fetch?: typeof fetch;
  resolvePath?: (filePath: string) => string;
  batchSize?: number;
}

interface ActiveIndexingJob {
  sender: IndexingProgressSender;
  progress: DesktopIndexingProgress;
  pauseRequested: boolean;
  resumeResolvers: Array<() => void>;
}

interface IndexingCounts {
  completed: number;
  indexed: number;
  skipped: number;
  failed: number;
}

interface IndexingRun {
  folderPath: string;
  dbPath: string;
  imageFiles: string[];
  errors: string[];
  counts: IndexingCounts;
}

export async function collectImageFiles(folderPath: string): Promise<string[]> {
  const entries = await readdir(folderPath, { withFileTypes: true });
  const nestedFiles = await Promise.all(
    entries.map(async (entry) => {
      const entryPath = join(folderPath, entry.name);
      if (entry.isDirectory()) {
        return collectImageFiles(entryPath);
      }
      if (entry.isFile() && SUPPORTED_IMAGE_EXTENSIONS.has(extname(entry.name).toLowerCase())) {
        return [entryPath];
      }
      return [];
    }),
  );
  return nestedFiles.flat().sort();
}

function toRelativePath(rootPath: string, filePath: string): string {
  return relative(rootPath, filePath).split(sep).join("/");
}

function chunkPaths(values: string[], chunkSize: number): string[][] {
  const chunks: string[][] = [];
  for (let index = 0; index < values.length; index += chunkSize) {
    chunks.push(values.slice(index, index + chunkSize));
  }
  return chunks;
}

function responseItems<T>(value: T[] | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

function itemRelativePath(item: { relative_path?: string | null }): string | null {
  return typeof item?.relative_path === "string" && item.relative_path.trim().length > 0
    ? item.relative_path
    : null;
}

function responseError(item: {
  relative_path?: string | null;
  message?: string | null;
}): string {
  const relativePath = itemRelativePath(item);
  const message = typeof item?.message === "string" && item.message.trim().length > 0
    ? item.message
    : "indexing failed";
  return relativePath ? `${relativePath}: ${message}` : message;
}

export async function requestImageBatch(
  request: ImageBatchRequest,
  dependencies: HttpBatchDependencies,
): Promise<ImageBatchResult> {
  const relativePaths = request.filePaths.map((filePath) => (
    toRelativePath(request.rootPath, filePath)
  ));
  const response = await (dependencies.fetch ?? fetch)(
    `${request.apiBase.replace(/\/$/, "")}/v1/indexing/jobs`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-MemoLens-Desktop-Token": dependencies.getSessionToken(),
      },
      body: JSON.stringify({
        model: request.model,
        persist_to_server: true,
        reindex: request.reindex,
        db_path: request.dbPath,
        input: {
          image_dir: request.rootPath,
          files: relativePaths,
          recursive: false,
        },
      }),
    },
  );
  const body = (await response.json()) as {
    message?: string;
    data?: Array<{ relative_path?: string | null }>;
    skipped?: Array<{ relative_path?: string | null }>;
    errors?: Array<{ relative_path?: string | null; message?: string | null }>;
  };
  if (!response.ok) {
    throw new Error(body.message ?? `indexing request failed with status ${response.status}`);
  }

  const indexedItems = responseItems(body.data);
  const skippedItems = responseItems(body.skipped);
  const errorItems = responseItems(body.errors);
  const processedPaths = new Set(
    [...indexedItems, ...skippedItems, ...errorItems]
      .map(itemRelativePath)
      .filter((value): value is string => value !== null),
  );
  const missingPaths = relativePaths.filter((relativePath) => !processedPaths.has(relativePath));
  const errors = [
    ...errorItems.map(responseError),
    ...missingPaths.map(
      (relativePath) => `${relativePath}: backend did not return a result for this file`,
    ),
  ];

  if (
    indexedItems.length === 0
    && skippedItems.length === 0
    && errorItems.length === 0
    && missingPaths.length === 0
  ) {
    throw new Error("indexing response did not contain any processed items");
  }

  return {
    indexed: indexedItems.length,
    skipped: skippedItems.length,
    failed: errorItems.length + missingPaths.length,
    errors,
  };
}

function canPausePhase(phase: DesktopIndexingPhase): boolean {
  return phase === "running" || phase === "pausing" || phase === "paused";
}

function batchLabel(rootPath: string, filePaths: string[]): string | null {
  const relativePaths = filePaths.map((filePath) => toRelativePath(rootPath, filePath));
  if (relativePaths.length <= 1) {
    return relativePaths[0] ?? null;
  }
  return `${relativePaths[0]} ... ${relativePaths[relativePaths.length - 1]}`;
}

function resultStatus(total: number, failed: number): DesktopIndexingResult["status"] {
  if (total === 0) {
    return "empty";
  }
  if (failed === total) {
    return "failed";
  }
  return failed > 0 ? "partial" : "completed";
}

export class DesktopIndexingCoordinator {
  private activeJob: ActiveIndexingJob | null = null;
  private startInProgress = false;
  private readonly collectFiles: (folderPath: string) => Promise<string[]>;
  private readonly analyzeBatch: ImageBatchAnalyzer;
  private readonly resolvePath: (filePath: string) => string;
  private readonly batchSize: number;

  constructor(private readonly options: DesktopIndexingCoordinatorOptions) {
    this.collectFiles = options.collectImageFiles ?? collectImageFiles;
    this.resolvePath = options.resolvePath ?? resolve;
    this.batchSize = Math.max(1, options.batchSize ?? LOCAL_INDEX_BATCH_SIZE);
    this.analyzeBatch = options.analyzeImageBatch ?? ((request) => (
      requestImageBatch(request, {
        getSessionToken: options.getSessionToken,
        fetch: options.fetch,
      })
    ));
  }

  async start(
    sender: IndexingProgressSender,
    options: DesktopIndexingStartOptions,
  ): Promise<DesktopIndexingResult> {
    this.assertCanStart();
    this.startInProgress = true;
    let job: ActiveIndexingJob | null = null;

    try {
      const run = await this.prepareRun(options);
      job = this.activateJob(sender, run);
      this.startInProgress = false;
      await this.processBatches(job, run, options);
      return this.completeJob(job, run);
    } finally {
      this.startInProgress = false;
      if (job !== null) {
        this.releaseResumeResolvers(job);
      }
      if (this.activeJob === job) {
        this.activeJob = null;
      }
    }
  }

  pause(): boolean {
    const job = this.activeJob;
    if (job === null || !canPausePhase(job.progress.phase)) {
      return false;
    }

    job.pauseRequested = true;
    if (job.progress.phase === "running") {
      this.publishProgress(job, { phase: "pausing" });
    }
    return true;
  }

  resume(): boolean {
    const job = this.activeJob;
    if (job === null || !canPausePhase(job.progress.phase)) {
      return false;
    }

    job.pauseRequested = false;
    if (job.progress.phase === "paused" || job.progress.phase === "pausing") {
      this.publishProgress(job, { phase: "running" });
    }
    this.releaseResumeResolvers(job);
    return true;
  }

  private assertCanStart(): void {
    if (this.activeJob !== null) {
      throw new Error(
        "An indexing job is already running. Pause or wait for the current run to finish.",
      );
    }
    if (this.startInProgress) {
      throw new Error(
        "An indexing job is already starting. Wait for the current run to initialize.",
      );
    }
  }

  private async prepareRun(options: DesktopIndexingStartOptions): Promise<IndexingRun> {
    const folderPath = this.resolvePath(options.folderPath);
    const dbPath = this.resolvePath(options.dbPath ?? this.options.resolveDbPath(folderPath));
    return {
      folderPath,
      dbPath,
      imageFiles: await this.collectFiles(folderPath),
      errors: [],
      counts: { completed: 0, indexed: 0, skipped: 0, failed: 0 },
    };
  }

  private activateJob(sender: IndexingProgressSender, run: IndexingRun): ActiveIndexingJob {
    const total = run.imageFiles.length;
    const job: ActiveIndexingJob = {
      sender,
      progress: {
        phase: "running",
        total,
        ...run.counts,
        currentFile: null,
        folderPath: run.folderPath,
        dbPath: run.dbPath,
        percent: total === 0 ? 100 : 0,
      },
      pauseRequested: false,
      resumeResolvers: [],
    };
    this.activeJob = job;
    this.publishProgress(job, job.progress);
    return job;
  }

  private async processBatches(
    job: ActiveIndexingJob,
    run: IndexingRun,
    options: DesktopIndexingStartOptions,
  ): Promise<void> {
    for (const filePaths of chunkPaths(run.imageFiles, this.batchSize)) {
      await this.waitIfPaused(job);
      const currentFile = batchLabel(run.folderPath, filePaths);
      this.publishProgress(job, { phase: "running", currentFile });
      await this.processBatch(run, options, filePaths, currentFile);
      this.publishBatchProgress(job, run, currentFile);
    }
  }

  private async processBatch(
    run: IndexingRun,
    options: DesktopIndexingStartOptions,
    filePaths: string[],
    currentFile: string | null,
  ): Promise<void> {
    try {
      const result = await this.analyzeBatch({
        apiBase: this.options.apiBase,
        filePaths,
        rootPath: run.folderPath,
        model: options.model ?? null,
        dbPath: run.dbPath,
        reindex: Boolean(options.reindex),
      });
      run.counts.indexed += result.indexed;
      run.counts.skipped += result.skipped;
      run.counts.failed += result.failed;
      run.errors.push(...result.errors);
    } catch (error) {
      run.counts.failed += filePaths.length;
      const message = error instanceof Error ? error.message : String(error);
      run.errors.push(`${currentFile ?? "batch"}: ${message}`);
    }
    run.counts.completed += filePaths.length;
  }

  private publishBatchProgress(
    job: ActiveIndexingJob,
    run: IndexingRun,
    currentFile: string | null,
  ): void {
    const total = run.imageFiles.length;
    this.publishProgress(job, {
      phase: run.counts.completed >= total
        ? "finalizing"
        : job.pauseRequested
          ? "pausing"
          : "running",
      ...run.counts,
      currentFile,
      percent: total === 0 ? 100 : Math.round((run.counts.completed / total) * 100),
    });
  }

  private completeJob(job: ActiveIndexingJob, run: IndexingRun): DesktopIndexingResult {
    const { indexed, skipped, failed, completed } = run.counts;
    const result: DesktopIndexingResult = {
      status: resultStatus(run.imageFiles.length, failed),
      folderPath: run.folderPath,
      dbPath: run.dbPath,
      total: run.imageFiles.length,
      indexed,
      skipped,
      failed,
      errors: run.errors,
    };
    this.publishProgress(job, {
      phase: "completed",
      completed,
      indexed,
      skipped,
      failed,
      currentFile: null,
      percent: 100,
    });
    return result;
  }

  private publishProgress(
    job: ActiveIndexingJob,
    patch: Partial<DesktopIndexingProgress>,
  ): void {
    job.progress = { ...job.progress, ...patch };
    if (!job.sender.isDestroyed()) {
      job.sender.send(PROGRESS_CHANNEL, job.progress);
    }
  }

  private async waitIfPaused(job: ActiveIndexingJob): Promise<void> {
    if (!job.pauseRequested) {
      return;
    }
    if (job.progress.phase !== "paused") {
      this.publishProgress(job, { phase: "paused" });
    }
    await new Promise<void>((resolvePromise) => {
      job.resumeResolvers.push(resolvePromise);
    });
  }

  private releaseResumeResolvers(job: ActiveIndexingJob): void {
    const resolvers = job.resumeResolvers;
    job.resumeResolvers = [];
    for (const resolvePromise of resolvers) {
      resolvePromise();
    }
  }
}
