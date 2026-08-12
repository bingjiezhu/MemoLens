import { createWriteStream, existsSync } from "node:fs";
import { link, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { dirname, join, resolve, sep } from "node:path";
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
import { DesktopIndexingCoordinator } from "./indexingCoordinator.js";

import type {
  DesktopSettings,
  DesktopFolderSelection,
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
const indexingCoordinator = new DesktopIndexingCoordinator({
  apiBase: DEFAULT_BACKEND_URL,
  getSessionToken: getDesktopSessionToken,
  resolveDbPath: resolveSelectedDbPath,
});

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
    return indexingCoordinator.start(event.sender, options);
  },
);

ipcMain.handle("memolens:pause-indexing", async (event): Promise<boolean> => {
  assertTrustedIpcSender(event);
  return indexingCoordinator.pause();
});

ipcMain.handle("memolens:resume-indexing", async (event): Promise<boolean> => {
  assertTrustedIpcSender(event);
  return indexingCoordinator.resume();
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
