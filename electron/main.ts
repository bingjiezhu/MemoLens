import { createWriteStream, existsSync } from "node:fs";
import { link, readdir, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { dirname, extname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

import {
  ensureBackendReady,
  getDesktopSessionToken,
  isBackendIdentityVerified,
  stopManagedBackend,
} from "./backendManager.js";
import {
  DEFAULT_BACKEND_URL,
  loadDesktopSettings,
  resolveLibraryDbPath,
  saveDesktopSettings,
} from "./desktopSettings.js";
import {
  ArtifactIntegrityTracker,
  normalizeSha256Etag,
  parseArtifactIntegrityProof,
} from "./artifactIntegrity.js";

import type {
  DesktopSettings,
  DesktopFolderSelection,
  DesktopIndexingPhase,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
} from "../src/query/types.js";
import type {
  DesktopArtifactSaveRequest,
  DesktopArtifactSaveResult,
} from "../src/video/types.js";

const require = createRequire(import.meta.url);
const { app, BrowserWindow, dialog, ipcMain } =
  require("electron") as typeof Electron.CrossProcessExports;

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
let desktopSessionAuthenticationConfigured = false;
const trustedRendererEntries = new Map<number, string>();

function assertTrustedIpcSender(event: Electron.IpcMainInvokeEvent): void {
  const expectedEntryUrl = trustedRendererEntries.get(event.sender.id);
  const isMainFrame = event.senderFrame === event.sender.mainFrame;
  if (
    !expectedEntryUrl
    || !isMainFrame
    || !isTrustedRendererNavigation(event.senderFrame.url, expectedEntryUrl)
  ) {
    throw new Error("Rejected IPC call from an untrusted renderer frame.");
  }
}

function configureSessionPermissions(): void {
  const { session } = require("electron") as typeof Electron.CrossProcessExports;
  const isAllowed = (
    webContents: Electron.WebContents | null,
    permission: string,
  ): boolean => Boolean(
    webContents
    && trustedRendererEntries.has(webContents.id)
    && permission === "clipboard-sanitized-write"
  );

  session.defaultSession.setPermissionCheckHandler((webContents, permission) => (
    isAllowed(webContents, permission)
  ));
  session.defaultSession.setPermissionRequestHandler((webContents, permission, callback) => {
    callback(isAllowed(webContents, permission));
  });
  session.defaultSession.setDevicePermissionHandler(() => false);
}

function configureDesktopSessionAuthentication(): void {
  if (desktopSessionAuthenticationConfigured) {
    return;
  }
  const { session } = require("electron") as typeof Electron.CrossProcessExports;
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${DEFAULT_BACKEND_URL}/*`] },
    (details, callback) => {
      if (isBackendIdentityVerified()) {
        details.requestHeaders["X-MemoLens-Desktop-Token"] = getDesktopSessionToken();
      } else {
        delete details.requestHeaders["X-MemoLens-Desktop-Token"];
      }
      callback({ requestHeaders: details.requestHeaders });
    },
  );
  desktopSessionAuthenticationConfigured = true;
}

function isLoopbackDevelopmentUrl(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl);
    return ["http:", "https:"].includes(parsed.protocol)
      && ["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname);
  } catch {
    return false;
  }
}

function isTrustedRendererNavigation(targetUrl: string, entryUrl: string): boolean {
  try {
    const target = new URL(targetUrl);
    const entry = new URL(entryUrl);
    if (entry.protocol === "file:") {
      return target.protocol === "file:" && fileURLToPath(target) === fileURLToPath(entry);
    }
    return target.origin === entry.origin;
  } catch {
    return false;
  }
}

function createWindow(): Electron.BrowserWindow {
  const window = new BrowserWindow({
    width: 1560,
    height: 1040,
    minWidth: 760,
    minHeight: 640,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      preload: join(CURRENT_DIR, "preload.cjs"),
    },
  });

  const requestedDevUrl = process.env.ELECTRON_RENDERER_URL;
  const devUrl = requestedDevUrl && isLoopbackDevelopmentUrl(requestedDevUrl)
    ? requestedDevUrl
    : null;
  if (requestedDevUrl && devUrl === null) {
    console.error(`[memolens-desktop] rejected non-loopback renderer URL: ${requestedDevUrl}`);
  }
  const indexPath = join(PROJECT_ROOT, "dist", "index.html");
  const rendererEntryUrl = devUrl ?? pathToFileURL(indexPath).toString();
  const webContentsId = window.webContents.id;
  trustedRendererEntries.set(webContentsId, rendererEntryUrl);
  window.webContents.once("destroyed", () => {
    trustedRendererEntries.delete(webContentsId);
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    console.warn(`[memolens-desktop] blocked new window: ${url}`);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, navigationUrl) => {
    if (!isTrustedRendererNavigation(navigationUrl, rendererEntryUrl)) {
      event.preventDefault();
      console.warn(`[memolens-desktop] blocked renderer navigation: ${navigationUrl}`);
    }
  });
  window.webContents.on("will-attach-webview", (event) => {
    event.preventDefault();
  });

  void window.loadURL(rendererEntryUrl);

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

function resolveSelectedDbPath(folderPath: string): string {
  return resolveLibraryDbPath(folderPath);
}

function sanitizeVideoExportFilename(value: string): string {
  const cleaned = value
    .normalize("NFKC")
    .replace(/[\\/\u0000-\u001f\u007f<>:"|?*]+/g, "-")
    .replace(/^\.+/, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 116);
  const stem = cleaned.replace(/\.mp4$/i, "").replace(/\.[^.]+$/, "").trim();
  return `${stem || "memolens-export"}.mp4`;
}

function isTrustedRenderArtifactUrl(rawUrl: string): boolean {
  try {
    const artifactUrl = new URL(rawUrl);
    const backendUrl = new URL(DEFAULT_BACKEND_URL);
    return artifactUrl.origin === backendUrl.origin
      && artifactUrl.username === ""
      && artifactUrl.password === ""
      && artifactUrl.search === ""
      && artifactUrl.hash === ""
      && /^\/v1\/renders\/[A-Za-z0-9_-]+\/download$/.test(artifactUrl.pathname);
  } catch {
    return false;
  }
}

async function saveCompletedRenderArtifact(
  request: DesktopArtifactSaveRequest,
): Promise<DesktopArtifactSaveResult> {
  const maximumBytes = 100 * 1024 * 1024 * 1024;
  const integrityProof = parseArtifactIntegrityProof(
    request?.expectedSha256,
    request?.expectedSizeBytes,
    maximumBytes,
  );
  if (
    !request
    || !isTrustedRenderArtifactUrl(request.artifactUrl)
    || integrityProof === null
  ) {
    return {
      status: "failed",
      filename: null,
      message: "MemoLens rejected an incomplete or untrusted render artifact proof.",
    };
  }

  const settings = await loadDesktopSettings(PROJECT_ROOT);
  const backendStatus = await ensureBackendReady(PROJECT_ROOT, settings);
  if (backendStatus.state !== "connected" && backendStatus.state !== "started") {
    return {
      status: "failed",
      filename: null,
      message: "MemoLens could not verify the local render service. Reconnect and try again.",
    };
  }

  const suggestedFilename = sanitizeVideoExportFilename(request.suggestedFilename);
  const selection = await dialog.showSaveDialog({
    title: "Save MemoLens video",
    defaultPath: suggestedFilename,
    buttonLabel: "Save video",
    filters: [{ name: "MP4 video", extensions: ["mp4"] }],
    properties: ["createDirectory", "showOverwriteConfirmation"],
  });
  if (selection.canceled || !selection.filePath) {
    return { status: "cancelled", filename: null, message: "Video save was cancelled." };
  }

  const destinationPath = selection.filePath.toLowerCase().endsWith(".mp4")
    ? selection.filePath
    : `${selection.filePath}.mp4`;
  if (existsSync(destinationPath)) {
    return {
      status: "exists",
      filename: sanitizeVideoExportFilename(destinationPath.split(sep).pop() ?? suggestedFilename),
      message: "That file already exists. Choose a new filename; MemoLens never overwrites by default.",
    };
  }

  const temporaryPath = `${destinationPath}.memolens-${randomUUID()}.part`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30 * 60 * 1000);
  try {
    const response = await fetch(request.artifactUrl, {
      headers: { "X-MemoLens-Desktop-Token": getDesktopSessionToken() },
      redirect: "error",
      signal: controller.signal,
    });
    if (response.url !== request.artifactUrl || !response.ok || response.body === null) {
      throw new Error(`Render download failed with status ${response.status}.`);
    }
    const declaredSize = Number(response.headers.get("content-length"));
    if (!Number.isSafeInteger(declaredSize) || declaredSize !== integrityProof.sizeBytes) {
      throw new Error("Render artifact size proof did not match the download response.");
    }
    if (normalizeSha256Etag(response.headers.get("etag")) !== integrityProof.sha256) {
      throw new Error("Render artifact ETag did not match its integrity proof.");
    }

    const integrityTracker = new ArtifactIntegrityTracker(integrityProof, maximumBytes);
    const byteLimit = new Transform({
      transform(chunk: Buffer, _encoding, callback) {
        try {
          integrityTracker.update(chunk);
        } catch (error) {
          controller.abort();
          callback(error instanceof Error ? error : new Error("Artifact verification failed."));
          return;
        }
        callback(null, chunk);
      },
    });

    await pipeline(
      Readable.fromWeb(response.body as Parameters<typeof Readable.fromWeb>[0]),
      byteLimit,
      createWriteStream(temporaryPath, { flags: "wx" }),
    );
    if (!integrityTracker.verify()) {
      throw new Error("Render artifact bytes did not match their integrity proof.");
    }
    // A hard link publishes the fully downloaded file atomically and fails if
    // another process created the destination after the save dialog closed.
    await link(temporaryPath, destinationPath);
    await unlink(temporaryPath);
    return {
      status: "saved",
      filename: destinationPath.split(sep).pop() ?? suggestedFilename,
      message: "Video saved without changing any source media.",
    };
  } catch (error) {
    await unlink(temporaryPath).catch(() => {});
    const rawMessage = error instanceof Error ? error.message : "";
    const safeMessage = rawMessage.includes("100 GB desktop safety limit")
      ? "Render artifact exceeds the 100 GB desktop safety limit."
      : rawMessage.includes("integrity proof") || rawMessage.includes("size proof") || rawMessage.includes("ETag")
        ? "Video integrity verification failed; no destination file was published."
      : controller.signal.aborted
        ? "Video save timed out before the artifact finished downloading."
        : /^Render download failed with status \d+\.$/.test(rawMessage)
          ? rawMessage
          : "Video could not be saved. Choose a new filename and try again.";
    return {
      status: "failed",
      filename: null,
      message: safeMessage,
    };
  } finally {
    clearTimeout(timeout);
  }
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
      "X-MemoLens-Desktop-Token": getDesktopSessionToken(),
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

ipcMain.handle("memolens:pick-image-folder", async (event) => {
  assertTrustedIpcSender(event);
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
  const dbPath = resolveSelectedDbPath(folderPath);
  await saveDesktopSettings(PROJECT_ROOT, {
    ...settings,
    defaultLibraryDir: folderPath,
    defaultDbPath: dbPath,
  });
  const selection: DesktopFolderSelection = {
    folderPath,
    dbPath,
  };
  return selection;
});

ipcMain.handle("memolens:get-settings", async (event): Promise<DesktopSettings> => {
  assertTrustedIpcSender(event);
  return loadDesktopSettings(PROJECT_ROOT);
});

ipcMain.handle(
  "memolens:save-video-artifact",
  async (
    event,
    request: DesktopArtifactSaveRequest,
  ): Promise<DesktopArtifactSaveResult> => {
    assertTrustedIpcSender(event);
    return saveCompletedRenderArtifact(request);
  },
);

ipcMain.handle(
  "memolens:save-settings",
  async (event, settings: DesktopSettings): Promise<DesktopSettings> => {
    assertTrustedIpcSender(event);
    return saveDesktopSettings(PROJECT_ROOT, settings);
  },
);

ipcMain.handle("memolens:ensure-backend", async (event) => {
  assertTrustedIpcSender(event);
  const settings = await loadDesktopSettings(PROJECT_ROOT);
  const status = await ensureBackendReady(PROJECT_ROOT, settings);
  if (status.state === "connected" || status.state === "started") {
    configureDesktopSessionAuthentication();
  }
  return status;
});

ipcMain.handle(
  "memolens:start-indexing",
  async (event, options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult> => {
    assertTrustedIpcSender(event);
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
      const dbPath = resolve(options.dbPath ?? resolveSelectedDbPath(folderPath));
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
        status:
          imageFiles.length === 0
            ? "empty"
            : failed === imageFiles.length
              ? "failed"
              : failed > 0
                ? "partial"
                : "completed",
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

ipcMain.handle("memolens:pause-indexing", async (event): Promise<boolean> => {
  assertTrustedIpcSender(event);
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

ipcMain.handle("memolens:resume-indexing", async (event): Promise<boolean> => {
  assertTrustedIpcSender(event);
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

app.whenReady().then(async () => {
  configureSessionPermissions();
  try {
    const settings = await loadDesktopSettings(PROJECT_ROOT);
    const status = await ensureBackendReady(PROJECT_ROOT, settings);
    if (status.state === "connected" || status.state === "started") {
      // Only expose the renderer session token after the backend has proved
      // possession of the spawn-time secret via the public health challenge.
      configureDesktopSessionAuthentication();
    }
    console.log(
      `[memolens-desktop] backend bootstrap ${status.state} :: ${status.url} :: ${status.message}`,
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[memolens-desktop] backend bootstrap failed :: ${message}`);
  }

  // Do not create a renderer that can make authenticated requests until the
  // backend proof attempt above has completed.
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
