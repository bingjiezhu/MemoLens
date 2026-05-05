import { existsSync } from "node:fs";
import { readdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, extname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { ensureBackendReady, stopManagedBackend } from "./backendManager.js";
import {
  DEFAULT_BACKEND_URL,
  loadDesktopSettings,
  saveDesktopSettings,
} from "./desktopSettings.js";

import type {
  DesktopSettings,
  DesktopFolderSelection,
  DesktopIndexingPhase,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
} from "../src/query/types.js";

const require = createRequire(import.meta.url);
const { app, BrowserWindow, dialog, ipcMain, protocol, net } =
  require("electron") as typeof Electron.CrossProcessExports;

protocol.registerSchemesAsPrivileged([
  {
    scheme: "memolens-local",
    privileges: {
      standard: true,
      secure: true,
      bypassCSP: true,
      allowServiceWorkers: true,
      supportFetchAPI: true,
      corsEnabled: true,
      stream: true,
    },
  },
]);

// Linux dev setups often lack a correctly configured chrome-sandbox helper.
// MemoLens runs as a local desktop tool, so we disable the setuid sandbox here
// to keep the Electron shell usable without extra system-level setup.
if (process.platform === "linux") {
  app.commandLine.appendSwitch("no-sandbox");
  app.commandLine.appendSwitch("disable-setuid-sandbox");
}

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

const LOCAL_INDEX_BATCH_SIZE = 6;

const CURRENT_FILE = fileURLToPath(import.meta.url);
const CURRENT_DIR = dirname(CURRENT_FILE);
const SOURCE_PROJECT_ROOT = resolve(CURRENT_DIR, "..", "..");

function resolveProjectRoot(): string {
  const candidates = [
    process.env.MEMOLENS_PROJECT_ROOT,
    app.getAppPath(),
    SOURCE_PROJECT_ROOT,
  ];
  for (const candidate of candidates) {
    if (!candidate) {
      continue;
    }
    const resolved = resolve(candidate);
    if (existsSync(join(resolved, "package.json")) && existsSync(join(resolved, "backend"))) {
      return resolved;
    }
  }
  return SOURCE_PROJECT_ROOT;
}

const PROJECT_ROOT = resolveProjectRoot();

interface ActiveIndexingJob {
  sender: Electron.WebContents;
  progress: DesktopIndexingProgress;
  pauseRequested: boolean;
  resumeResolvers: Array<() => void>;
}

let activeIndexingJob: ActiveIndexingJob | null = null;
let indexingStartInProgress = false;

/**
 * Allow the renderer (loaded from file://) to make requests to the local
 * backend at 127.0.0.1:5519.  We intercept every response from that origin
 * and ensure the CORS headers the browser needs are present, regardless of
 * what the backend itself returns.  This is safe because MemoLens only ever
 * connects to a trusted, user-started localhost service.
 */
function configureLocalBackendAccess(): void {
  const { session } = require("electron") as typeof Electron.CrossProcessExports;
  const backendPattern = "http://127.0.0.1:5519/*";

  session.defaultSession.webRequest.onHeadersReceived(
    { urls: [backendPattern] },
    (details, callback) => {
      const headers = { ...details.responseHeaders };
      headers["Access-Control-Allow-Origin"] = ["*"];
      headers["Access-Control-Allow-Methods"] = ["GET, POST, PUT, OPTIONS"];
      headers["Access-Control-Allow-Headers"] = ["Content-Type, Authorization"];
      callback({ responseHeaders: headers });
    },
  );
}

function createWindow(): Electron.BrowserWindow {
  const window = new BrowserWindow({
    width: 1560,
    height: 1040,
    minWidth: 1120,
    minHeight: 760,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      preload: join(CURRENT_DIR, "preload.js"),
    },
  });

  const devUrl = process.env.ELECTRON_RENDERER_URL;
  if (devUrl) {
    void window.loadURL(devUrl);
  } else {
    const indexPath = join(PROJECT_ROOT, "dist", "index.html");
    void window.loadURL(pathToFileURL(indexPath).toString());
  }

  window.webContents.on("did-finish-load", () => {
    console.log("[memolens-desktop] renderer finished loading");
  });
  window.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedUrl) => {
    console.error(
      `[memolens-desktop] renderer failed to load (${errorCode}) ${errorDescription} :: ${validatedUrl}`,
    );
  });

  return window;
}

async function collectImageFiles(folderPath: string): Promise<string[]> {
  const entries = await readdir(folderPath, { withFileTypes: true });
  const nested = await Promise.all(
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
  return nested.flat().sort();
}

function toRelativePath(rootPath: string, filePath: string): string {
  return relative(rootPath, filePath).split(sep).join("/");
}

function chunkPaths(values: string[], chunkSize: number): string[][] {
  if (chunkSize <= 1) {
    return values.map((value) => [value]);
  }

  const chunks: string[][] = [];
  for (let index = 0; index < values.length; index += chunkSize) {
    chunks.push(values.slice(index, index + chunkSize));
  }
  return chunks;
}

function resolveSelectedDbPath(settings: DesktopSettings, folderPath: string): string {
  const defaultLibraryDir = settings.defaultLibraryDir ? resolve(settings.defaultLibraryDir) : null;
  const defaultDbPath = settings.defaultDbPath ? resolve(settings.defaultDbPath) : null;

  if (defaultLibraryDir === folderPath && defaultDbPath) {
    return defaultDbPath;
  }

  return join(folderPath, "photo_index.db");
}

function emitProgress(sender: Electron.WebContents, progress: DesktopIndexingProgress): void {
  if (sender.isDestroyed()) {
    return;
  }
  sender.send("memolens:indexing-progress", progress);
}

function publishJobProgress(
  job: ActiveIndexingJob,
  patch: Partial<DesktopIndexingProgress>,
): DesktopIndexingProgress {
  const nextProgress = {
    ...job.progress,
    ...patch,
  };
  job.progress = nextProgress;
  emitProgress(job.sender, nextProgress);
  return nextProgress;
}

function releaseResumeResolvers(job: ActiveIndexingJob): void {
  const resolvers = [...job.resumeResolvers];
  job.resumeResolvers = [];
  for (const resolve of resolvers) {
    resolve();
  }
}

async function waitIfPaused(job: ActiveIndexingJob): Promise<void> {
  if (!job.pauseRequested) {
    return;
  }

  if (job.progress.phase !== "paused") {
    publishJobProgress(job, {
      phase: "paused",
    });
  }

  await new Promise<void>((resolve) => {
    job.resumeResolvers.push(resolve);
  });
}

function canPausePhase(phase: DesktopIndexingPhase): boolean {
  return phase === "running" || phase === "pausing" || phase === "paused";
}

async function analyzeImageBatch({
  apiBase,
  filePaths,
  rootPath,
  model,
  dbPath,
  reindex,
}: {
  apiBase: string;
  filePaths: string[];
  rootPath: string;
  model: string | null;
  dbPath: string;
  reindex: boolean;
}): Promise<{ indexed: number; skipped: number; failed: number; errors: string[] }> {
  const relativePaths = filePaths.map((filePath) => toRelativePath(rootPath, filePath));
  const payload = {
    model,
    persist_to_server: true,
    reindex,
    db_path: dbPath,
    input: {
      image_dir: rootPath,
      files: relativePaths,
      recursive: false,
    },
  };

  const response = await fetch(`${apiBase.replace(/\/$/, "")}/v1/indexing/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  const body = (await response.json()) as {
    message?: string;
    data?: Array<{ relative_path?: string | null }>;
    skipped?: Array<{ relative_path?: string | null }>;
    errors?: Array<{ relative_path?: string | null; message?: string | null }>;
  };
  if (!response.ok) {
    throw new Error(body.message ?? `indexing request failed with status ${response.status}`);
  }

  const indexedItems = Array.isArray(body.data) ? body.data : [];
  const skippedItems = Array.isArray(body.skipped) ? body.skipped : [];
  const errorItems = Array.isArray(body.errors) ? body.errors : [];
  const errors = errorItems
    .map((item) => {
      const relativePath =
        typeof item?.relative_path === "string" && item.relative_path.trim().length > 0
          ? item.relative_path
          : null;
      const message =
        typeof item?.message === "string" && item.message.trim().length > 0
          ? item.message
          : "indexing failed";
      return relativePath ? `${relativePath}: ${message}` : message;
    });
  const processedRelativePaths = new Set(
    [...indexedItems, ...skippedItems, ...errorItems]
      .map((item) =>
        typeof item?.relative_path === "string" && item.relative_path.trim().length > 0
          ? item.relative_path
          : null,
      )
      .filter((value): value is string => value !== null),
  );
  const missingRelativePaths = relativePaths.filter(
    (relativePath) => !processedRelativePaths.has(relativePath),
  );
  if (missingRelativePaths.length > 0) {
    errors.push(
      ...missingRelativePaths.map(
        (relativePath) => `${relativePath}: backend did not return a result for this file`,
      ),
    );
  }

  if (
    indexedItems.length === 0
    && skippedItems.length === 0
    && errorItems.length === 0
    && missingRelativePaths.length === 0
  ) {
    throw new Error("indexing response did not contain any processed items");
  }

  return {
    indexed: indexedItems.length,
    skipped: skippedItems.length,
    failed: errorItems.length + missingRelativePaths.length,
    errors,
  };
}

ipcMain.handle("memolens:pick-image-folder", async () => {
  const settings = await loadDesktopSettings(PROJECT_ROOT);
  const result = await dialog.showOpenDialog({
    properties: ["openDirectory"],
    title: "Select local image folder",
    defaultPath: settings.defaultLibraryDir ?? undefined,
  });
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  const folderPath = resolve(result.filePaths[0]);
  const selection: DesktopFolderSelection = {
    folderPath,
    dbPath: resolveSelectedDbPath(settings, folderPath),
  };
  return selection;
});

ipcMain.handle("memolens:get-settings", async (): Promise<DesktopSettings> => {
  return loadDesktopSettings(PROJECT_ROOT);
});

ipcMain.handle(
  "memolens:save-settings",
  async (_event, settings: DesktopSettings): Promise<DesktopSettings> => {
    return saveDesktopSettings(PROJECT_ROOT, settings);
  },
);

ipcMain.handle("memolens:ensure-backend", async () => {
  const settings = await loadDesktopSettings(PROJECT_ROOT);
  return ensureBackendReady(PROJECT_ROOT, settings);
});

ipcMain.handle(
  "memolens:start-indexing",
  async (event, options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult> => {
    if (activeIndexingJob !== null) {
      throw new Error("An indexing job is already running. Pause or wait for the current run to finish.");
    }
    if (indexingStartInProgress) {
      throw new Error("An indexing job is already starting. Wait for the current run to initialize.");
    }

    indexingStartInProgress = true;
    let job: ActiveIndexingJob | null = null;

    try {
      const folderPath = resolve(options.folderPath);
      const settings = await loadDesktopSettings(PROJECT_ROOT);
      const dbPath = resolve(options.dbPath ?? resolveSelectedDbPath(settings, folderPath));
      const apiBase = DEFAULT_BACKEND_URL;
      const imageFiles = await collectImageFiles(folderPath);

      const errors: string[] = [];
      let completed = 0;
      let indexed = 0;
      let skipped = 0;
      let failed = 0;
      job = {
        sender: event.sender,
        progress: {
          phase: "running",
          total: imageFiles.length,
          completed,
          indexed,
          skipped,
          failed,
          currentFile: null,
          folderPath,
          dbPath,
          percent: imageFiles.length === 0 ? 100 : 0,
        },
        pauseRequested: false,
        resumeResolvers: [],
      };
      activeIndexingJob = job;
      indexingStartInProgress = false;

      publishJobProgress(job, {
        phase: "running",
        total: imageFiles.length,
        completed,
        indexed,
        skipped,
        failed,
        currentFile: null,
        folderPath,
        dbPath,
        percent: imageFiles.length === 0 ? 100 : 0,
      });

      const imageBatches = chunkPaths(imageFiles, LOCAL_INDEX_BATCH_SIZE);

      for (const fileBatch of imageBatches) {
        await waitIfPaused(job);

        const batchRelativePaths = fileBatch.map((filePath) => toRelativePath(folderPath, filePath));
        const currentFile =
          batchRelativePaths.length <= 1
            ? (batchRelativePaths[0] ?? null)
            : `${batchRelativePaths[0]} ... ${batchRelativePaths[batchRelativePaths.length - 1]}`;
        publishJobProgress(job, {
          phase: "running",
          currentFile,
        });

        try {
          const result = await analyzeImageBatch({
            apiBase,
            filePaths: fileBatch,
            rootPath: folderPath,
            model: options.model ?? null,
            dbPath,
            reindex: Boolean(options.reindex),
          });
          indexed += result.indexed;
          skipped += result.skipped;
          failed += result.failed;
          errors.push(...result.errors);
        } catch (error) {
          failed += fileBatch.length;
          errors.push(`${currentFile ?? "batch"}: ${error instanceof Error ? error.message : String(error)}`);
        }

        completed += fileBatch.length;
        publishJobProgress(job, {
          phase:
            completed >= imageFiles.length ? "finalizing" : job.pauseRequested ? "pausing" : "running",
          completed,
          indexed,
          skipped,
          failed,
          currentFile,
          percent:
            imageFiles.length === 0 ? 100 : Math.round((completed / imageFiles.length) * 100),
        });
      }

      const result: DesktopIndexingResult = {
        status: "completed",
        folderPath,
        dbPath,
        total: imageFiles.length,
        indexed,
        skipped,
        failed,
        errors,
      };
      publishJobProgress(job, {
        phase: "completed",
        completed,
        indexed,
        skipped,
        failed,
        currentFile: null,
        percent: 100,
      });
      return result;
    } finally {
      indexingStartInProgress = false;
      if (job) {
        releaseResumeResolvers(job);
      }
      if (job && activeIndexingJob === job) {
        activeIndexingJob = null;
      }
    }
  },
);

ipcMain.handle("memolens:pause-indexing", async (): Promise<boolean> => {
  const job = activeIndexingJob;
  if (job === null || !canPausePhase(job.progress.phase)) {
    return false;
  }

  job.pauseRequested = true;
  if (job.progress.phase === "running") {
    publishJobProgress(job, {
      phase: "pausing",
    });
  }
  return true;
});

ipcMain.handle("memolens:resume-indexing", async (): Promise<boolean> => {
  const job = activeIndexingJob;
  if (job === null || !canPausePhase(job.progress.phase)) {
    return false;
  }

  job.pauseRequested = false;
  if (job.progress.phase === "paused" || job.progress.phase === "pausing") {
    publishJobProgress(job, {
      phase: "running",
    });
  }
  releaseResumeResolvers(job);
  return true;
});

app.whenReady().then(() => {
  configureLocalBackendAccess();

  protocol.handle("memolens-local", (request) => {
    try {
      const parsed = new URL(request.url);
      const decodedPath = decodeURIComponent(parsed.pathname);
      console.log(`[memolens-local] Serving: ${decodedPath}`);
      return net.fetch(pathToFileURL(decodedPath).toString());
    } catch (error) {
      console.error("[memolens-local] Failed to parse URL:", request.url, error);
      return new Response("Not found", { status: 404 });
    }
  });

  void loadDesktopSettings(PROJECT_ROOT)
    .then((settings) => ensureBackendReady(PROJECT_ROOT, settings))
    .then((status) => {
      console.log(
        `[memolens-desktop] backend bootstrap ${status.state} :: ${status.url} :: ${status.message}`,
      );
    })
    .catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`[memolens-desktop] backend bootstrap failed :: ${message}`);
    });

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });

  app.on("browser-window-created", (_, win) => {
    win.webContents.on("console-message", (_event, _level, message, line, sourceId) => {
      console.log(`[Renderer] ${message} (${sourceId}:${line})`);
    });
  });
});

app.on("before-quit", () => {
  stopManagedBackend();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
