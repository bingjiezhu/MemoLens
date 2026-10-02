import { constants, existsSync } from "node:fs";
import { open } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  commitLibraryBootstrapCandidate,
  ensureBackendReady,
  ensureLibraryBootstrapCandidateReady,
  getDesktopSessionToken,
  getMainAuthorityToken,
  isBackendIdentityVerified,
  matchesLibraryBootstrapRuntime,
  probeBackendHealthV2,
  stopManagedBackend,
} from "./backendManager.js";
import {
  commitDesktopLibrarySelection,
  commitBootstrapLibraryProjection,
  DEFAULT_BACKEND_URL,
  loadApprovedDesktopLibraryBinding,
  loadDesktopSettings,
  saveDesktopSettings,
} from "./desktopSettings.js";
import {
  LibrarySelectionAuthority,
  NativeLibrarySelectionCoordinator,
} from "./librarySelectionAuthority.js";
import {
  LibraryBootstrapBroker,
  LibraryBootstrapRequestSpool,
} from "./libraryBootstrapBroker.js";
import { LibraryBootstrapBindingStore } from "./libraryBootstrapBinding.js";
import { LibraryBootstrapCoordinator } from "./libraryBootstrapCoordinator.js";
import { runDesktopStartupRoute } from "./desktopStartupRouter.js";
import { DesktopIndexingCoordinator } from "./indexingCoordinator.js";
import { VideoArtifactSaveCoordinator } from "./videoArtifactSaveCoordinator.js";
import {
  buildMemoLensCodexUrl,
  dispatchMemoLensLocalPluginUrl,
} from "./codexIntegration.js";
import {
  AgentAuthorityCoordinator,
  conductNativeDecisionReview,
  DesktopAuthorityError,
  sanitizeNativeAuthorityText,
} from "./agentAuthorityCoordinator.js";
import { CanonicalExportCoordinator } from "./canonicalExportCoordinator.js";
import {
  configureElectronSessionNetworkPolicy,
  dispatchExternalNavigation,
  guardedFetch,
  isLiteralLoopbackUrl,
} from "./networkPolicy.js";
import {
  assertTrustedIpcSender,
  isTrustedRendererNavigation,
} from "./ipcAuthority.js";
import { ProductionIpcHandlerRegistry } from "./productionSurfaceRegistry.js";

import type {
  DesktopSettings,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
  DesktopSettingsUpdate,
} from "../src/query/types.js";
import type {
  DesktopArtifactSaveRequest,
  DesktopArtifactSaveResult,
} from "../src/video/types.js";
import type {
  DesktopAgentAuthorityOverview,
  DesktopAuthorityActionResult,
  DesktopDecisionReviewRequest,
} from "../src/blueprint/authorityTypes.js";
import type {
  CanonicalExportPresentation,
  DesktopCanonicalExportApprovalRequest,
  DesktopCanonicalExportApprovalResult,
} from "../src/blueprint/exportTypes.js";

const require = createRequire(import.meta.url);
const { app, BrowserWindow, dialog, ipcMain, shell } =
  require("electron") as typeof Electron.CrossProcessExports;
const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
}

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
let backendLifecycleActivated = false;
const librarySelectionAuthority = new LibrarySelectionAuthority();
const nativeLibrarySelectionCoordinator = new NativeLibrarySelectionCoordinator({
  authority: librarySelectionAuthority,
  async chooseFolder() {
    const settings = await loadDesktopSettings(PROJECT_ROOT);
    const result = await dialog.showOpenDialog({
      properties: ["openDirectory"],
      title: "Select local media folder",
      defaultPath: settings.defaultLibraryDir ?? undefined,
    });
    return result.canceled || result.filePaths.length === 0
      ? null
      : result.filePaths[0];
  },
});
const libraryBootstrapRequestSpool = new LibraryBootstrapRequestSpool();
const libraryBootstrapBindingStore = new LibraryBootstrapBindingStore();
const libraryBootstrapCoordinator = new LibraryBootstrapCoordinator({
  bindingStore: libraryBootstrapBindingStore,
  authority: librarySelectionAuthority,
  async ensureCandidate(envelope) {
    backendLifecycleActivated = true;
    const settings = await loadDesktopSettings(PROJECT_ROOT);
    return ensureLibraryBootstrapCandidateReady(PROJECT_ROOT, settings, {
      requestId: envelope.request_id,
      candidateBindingSha256: envelope.candidate_binding_sha256,
    });
  },
  async commitCore(envelope) {
    return commitLibraryBootstrapCandidate(
      DEFAULT_BACKEND_URL,
      envelope.request_id,
      envelope.candidate_binding_sha256,
    );
  },
  async verifyActive(envelope) {
    const expectation = {
      mode: "active" as const,
      requestId: envelope.request_id,
      candidateBindingSha256: envelope.candidate_binding_sha256,
    };
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const runtime = await probeBackendHealthV2(DEFAULT_BACKEND_URL);
      if (runtime !== null) {
        if (matchesLibraryBootstrapRuntime(runtime, expectation)) return;
        throw new Error("MemoLens refused a Library projection from the wrong active runtime.");
      }
      if (attempt < 2) {
        await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 100));
      }
    }
    throw new Error("MemoLens could not prove the committed active Library runtime.");
  },
  commitProjection(binding) {
    return commitBootstrapLibraryProjection(PROJECT_ROOT, binding);
  },
});
const libraryBootstrapBroker = new LibraryBootstrapBroker({
  spool: libraryBootstrapRequestSpool,
  picker: nativeLibrarySelectionCoordinator,
  authority: librarySelectionAuthority,
  resume: (claimed) => libraryBootstrapCoordinator.resume(claimed),
  commit: (verified, claimed) => libraryBootstrapCoordinator.commit(verified, claimed),
});
const indexingCoordinator = new DesktopIndexingCoordinator({
  apiBase: DEFAULT_BACKEND_URL,
  getSessionToken: getDesktopSessionToken,
  async resolveApprovedSelection() {
    const binding = await loadApprovedDesktopLibraryBinding();
    if (binding === null) {
      throw new Error("Choose and connect a local library before indexing.");
    }
    const verified = await librarySelectionAuthority.verifyActiveBinding(binding);
    return {
      folderPath: verified.canonicalRoot,
      dbPath: verified.dbPath,
      rootDevice: verified.device,
      rootInode: verified.inode,
      verifyCurrentIdentity: verified.verifyCurrentIdentity,
      release: verified.release,
    };
  },
});

function showAuthorityMessageBox(
  options: Electron.MessageBoxOptions,
): Promise<Electron.MessageBoxReturnValue> {
  const focusedWindow = BrowserWindow.getFocusedWindow();
  return focusedWindow
    ? dialog.showMessageBox(focusedWindow, options)
    : dialog.showMessageBox(options);
}

async function ensureAuthorityBackendTrusted(forceIdentityProof = false): Promise<void> {
  if (!forceIdentityProof && isBackendIdentityVerified()) {
    return;
  }
  backendLifecycleActivated = true;
  const settings = await loadDesktopSettings(PROJECT_ROOT);
  const status = await ensureBackendReady(PROJECT_ROOT, settings);
  if (
    (status.state !== "connected" && status.state !== "started")
    || !isBackendIdentityVerified()
  ) {
    throw new DesktopAuthorityError(
      "native_confirmation_required",
      "MemoLens could not verify the local authority service.",
    );
  }
}

const videoArtifactSaveCoordinator = new VideoArtifactSaveCoordinator({
  backendUrl: DEFAULT_BACKEND_URL,
  ensureBackendTrusted: () => ensureAuthorityBackendTrusted(true),
  getSessionToken: getDesktopSessionToken,
  fetchImpl: guardedFetch,
  async chooseDestination(suggestedFilename) {
    const selection = await dialog.showSaveDialog({
      title: "Save MemoLens video",
      defaultPath: suggestedFilename,
      buttonLabel: "Save video",
      filters: [{ name: "MP4 video", extensions: ["mp4"] }],
      properties: ["createDirectory", "showOverwriteConfirmation"],
    });
    return selection.canceled || !selection.filePath
      ? { cancelled: true }
      : { cancelled: false, filePath: selection.filePath };
  },
});

const authorityCoordinator = new AgentAuthorityCoordinator({
  apiBase: DEFAULT_BACKEND_URL,
  ensureBackendTrusted: ensureAuthorityBackendTrusted,
  getMainAuthorityToken,
  getDesktopSessionToken,
  fetchImpl: guardedFetch,
  presenter: {
    async reviewPairing(presentation) {
      const actions = presentation.requestedActions.map((value) => (
        sanitizeNativeAuthorityText(value)
      )).join(", ");
      const timeline = presentation.observedTimelineHead;
      const detail = [
        `Project: ${sanitizeNativeAuthorityText(presentation.projectId)}`,
        `Agent subject ID (self-reported): ${sanitizeNativeAuthorityText(presentation.pairedSubjectId)}`,
        `Current Blueprint revision: ${presentation.observedRevision}`,
        ...(timeline === null ? [] : [
          `Current canonical Timeline: revision ${timeline.revision}`,
          `Timeline ID: ${sanitizeNativeAuthorityText(timeline.timelineId)}`,
          `Timeline revision SHA-256: ${timeline.revisionSha256}`,
          `Timeline content SHA-256: ${timeline.timelineContentSha256}`,
          `Pinned Blueprint/Coverage revisions: ${timeline.blueprintRevision}/${timeline.coverageRevision}`,
        ]),
        `Pairing code: ${sanitizeNativeAuthorityText(presentation.shortCode)}`,
        `Requested actions: ${actions}`,
        `Window: ${presentation.ttlSeconds} seconds, up to ${presentation.maxOperations} operations`,
        `Request expires: ${sanitizeNativeAuthorityText(presentation.expiresAt)}`,
        "",
        "The client name is self-reported. MemoLens verifies only possession of this local pairing secret. Pairing permits only the listed reversible project writes; it never confirms creative decisions or permits render, export, or publication.",
      ].join("\n");
      const result = await showAuthorityMessageBox({
        type: "question",
        title: "Review Agent pairing",
        message: `${sanitizeNativeAuthorityText(presentation.claimedClientLabel, 120)} requests project access`,
        detail,
        buttons: ["Allow scoped project access", "Reject request", "Cancel"],
        defaultId: 2,
        cancelId: 2,
        noLink: true,
      });
      if (result.response === 0) return "approve";
      if (result.response === 1) return "reject";
      return "cancel";
    },
    async confirmCapabilityRevoke(capability) {
      const detail = [
        `Project: ${sanitizeNativeAuthorityText(capability.projectId)}`,
        `Agent subject ID (self-reported): ${sanitizeNativeAuthorityText(capability.pairedSubjectId)}`,
        `Client label: ${sanitizeNativeAuthorityText(capability.claimedClientLabel, 120)} (self-reported)`,
        `Scope: ${capability.actions.map((value) => sanitizeNativeAuthorityText(value)).join(", ")}`,
        `Remaining operations: ${capability.remainingOperations} of ${capability.maxOperations}`,
        `Expires: ${sanitizeNativeAuthorityText(capability.expiresAt)}`,
        "",
        "Revocation stops new Agent writes. Revisions already committed remain in project history.",
      ].join("\n");
      const result = await showAuthorityMessageBox({
        type: "warning",
        title: "Revoke Agent access",
        message: "Stop this Agent from submitting new project writes?",
        detail,
        buttons: ["Revoke access", "Cancel"],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
      });
      return result.response === 0;
    },
    async reviewDecision(presentation) {
      const verb = presentation.operation === "confirm" ? "Confirm" : "Revoke";
      return conductNativeDecisionReview(presentation, async (step) => {
        if (step.kind === "content") {
          const result = await showAuthorityMessageBox({
            type: presentation.operation === "confirm" ? "question" : "warning",
            title: `Review exact content before ${verb.toLowerCase()}`,
            message: `${sanitizeNativeAuthorityText(step.unitLabel, 120)} — page ${step.pageIndex + 1} of ${step.pageCount}`,
            detail: [
              `Project: ${sanitizeNativeAuthorityText(step.projectId)}`,
              `Exact Blueprint revision: ${step.observedRevision}`,
              `Decision unit ${step.unitIndex + 1} of ${step.unitCount}: ${step.unitName}`,
              "",
              "Complete canonical content on this page:",
              "",
              // This text is already losslessly canonicalized and control-safe.
              // Never pass it through a truncating display sanitizer.
              step.pageContent,
              "",
              "Continue only after reviewing this entire page. Cancel changes no authority.",
            ].join("\n"),
            buttons: ["Continue review", "Cancel"],
            defaultId: 1,
            cancelId: 1,
            noLink: true,
          });
          return result.response === 0;
        }

        const selectedUnits = step.decisionUnits.map((unit, index) => (
          `${index + 1}. ${sanitizeNativeAuthorityText(unit.label, 120)} (${unit.name})`
        )).join("\n");
        const result = await showAuthorityMessageBox({
          type: presentation.operation === "confirm" ? "question" : "warning",
          title: `${verb} creative decisions`,
          message: `${verb} ${step.decisionUnits.length} decision ${step.decisionUnits.length === 1 ? "unit" : "units"}?`,
          detail: [
            `Project: ${sanitizeNativeAuthorityText(step.projectId)}`,
            `Exact Blueprint revision: ${step.observedRevision}`,
            "",
            "You reviewed every page of the complete canonical content for:",
            selectedUnits,
            "",
            presentation.operation === "confirm"
              ? "This confirms only those exact decisions. It does not approve rendering, export, publishing, or future changed content."
              : "This removes user authority only. It does not change the Blueprint proposal itself.",
          ].join("\n"),
          buttons: [`${verb} selected decisions`, "Cancel"],
          defaultId: 1,
          cancelId: 1,
          noLink: true,
        });
        return result.response === 0;
      });
    },
  },
});

function showCanonicalExportDirectoryDialog(
  options: Electron.OpenDialogOptions,
): Promise<Electron.OpenDialogReturnValue> {
  const focusedWindow = BrowserWindow.getFocusedWindow();
  return focusedWindow
    ? dialog.showOpenDialog(focusedWindow, options)
    : dialog.showOpenDialog(options);
}

async function chooseCanonicalExportDestination(
  presentation: CanonicalExportPresentation,
  presentationSha256: string,
  suggestedPackageName: string,
) {
  const timeline = presentation.timeline_binding;
  const result = await showCanonicalExportDirectoryDialog({
    title: "Approve exact canonical export",
    message: [
      `Project: ${sanitizeNativeAuthorityText(presentation.project_id)}`,
      `Approve exact Timeline revision ${timeline.revision}`,
      `Timeline ID: ${sanitizeNativeAuthorityText(timeline.timeline_id)}`,
      `Profile: ${presentation.profile} · ${presentation.aspect_ratio} · ${presentation.duration_ms} ms · ${presentation.clip_count} clips`,
      "Hard cuts, silent, no subtitles. Choose the parent folder for a new non-overwriting package.",
      "Package: final video, exact Blueprint script, package manifest, human usage list, completion marker.",
      `Package name: ${suggestedPackageName}`,
      `Exact presentation SHA-256: ${presentationSha256}`,
      `Timeline revision SHA-256: ${timeline.revision_sha256}`,
      `Timeline content SHA-256: ${timeline.timeline_content_sha256}`,
      `Source bindings SHA-256: ${timeline.source_bindings_sha256}`,
    ].join("\n"),
    buttonLabel: `Approve revision ${timeline.revision} export`,
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || result.filePaths.length !== 1) return { cancelled: true } as const;
  const canonicalPath = resolve(result.filePaths[0]);
  const handle = await open(
    canonicalPath,
    constants.O_RDONLY
      | (constants.O_DIRECTORY ?? 0)
      | (constants.O_NOFOLLOW ?? 0),
  );
  try {
    const identity = await handle.stat({ bigint: true });
    if (!identity.isDirectory()) {
      throw new Error("The approved canonical export destination is not a directory.");
    }
    return {
      cancelled: false,
      canonicalPath,
      selectionIdentity: {
        device: identity.dev.toString(10),
        inode: identity.ino.toString(10),
      },
      release: async () => handle.close(),
    } as const;
  } catch (error) {
    await handle.close().catch(() => undefined);
    throw error;
  }
}

const canonicalExportCoordinator = new CanonicalExportCoordinator({
  apiBase: DEFAULT_BACKEND_URL,
  ensureBackendTrusted: ensureAuthorityBackendTrusted,
  getMainAuthorityToken,
  fetchImpl: guardedFetch,
  presenter: { chooseDestination: chooseCanonicalExportDestination },
});

let desktopSessionAuthenticationConfigured = false;
let normalStartupCompleted = false;
const trustedRendererEntries = new Map<number, string>();

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

async function runBackendBootstrap(): Promise<void> {
  backendLifecycleActivated = true;
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
}

async function runPendingLibraryBootstrap(): Promise<boolean> {
  if (!await libraryBootstrapRequestSpool.hasPendingRequest()) {
    return false;
  }
  const result = await libraryBootstrapBroker.handleNext();
  if (result !== null) {
    console.log(
      `[memolens-desktop] Library bootstrap ${result.status} :: ${result.request_id}`,
    );
    if (result.status === "library_authority_committed" && normalStartupCompleted) {
      configureDesktopSessionAuthentication();
      // Both activate and second-instance may share the broker's single-flight
      // result. Reuse the first live window synchronously so they cannot create
      // a duplicate. Cold startup still owns its own IPC/window promotion.
      const window = BrowserWindow.getAllWindows()[0] ?? createWindow();
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
    }
  }
  return true;
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
  const devUrl = requestedDevUrl && isLiteralLoopbackUrl(requestedDevUrl)
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

function authorityActionFailure(error: unknown): DesktopAuthorityActionResult {
  return {
    status: "failed",
    message: error instanceof DesktopAuthorityError
      ? sanitizeNativeAuthorityText(error.message, 500)
      : "MemoLens could not complete the native authority review.",
    projectAuthority: null,
  };
}

let nativeAuthorityReviewInProgress = false;

async function runNativeAuthorityReview(
  action: () => Promise<DesktopAuthorityActionResult>,
): Promise<DesktopAuthorityActionResult> {
  if (nativeAuthorityReviewInProgress) {
    return {
      status: "failed",
      message: "Another native authority review is already open.",
      projectAuthority: null,
    };
  }
  nativeAuthorityReviewInProgress = true;
  try {
    return await action();
  } finally {
    nativeAuthorityReviewInProgress = false;
  }
}

function registerProductionIpcHandlers(): void {
const productionIpcHandlers = new ProductionIpcHandlerRegistry(ipcMain);

productionIpcHandlers.handle("memolens:pick-image-folder", async (event) => {
  assertTrustedIpcSender(event, trustedRendererEntries);
  return nativeLibrarySelectionCoordinator.pick();
});

productionIpcHandlers.handle(
  "memolens:commit-library-selection",
  async (event, selectionTicket: string): Promise<DesktopSettings> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    const verified = await librarySelectionAuthority.redeemSelection(selectionTicket);
    try {
      return await commitDesktopLibrarySelection(PROJECT_ROOT, verified);
    } finally {
      await verified.release();
    }
  },
);

productionIpcHandlers.handle("memolens:get-settings", async (event): Promise<DesktopSettings> => {
  assertTrustedIpcSender(event, trustedRendererEntries);
  return loadDesktopSettings(PROJECT_ROOT);
});

productionIpcHandlers.handle(
  "memolens:list-agent-authority",
  async (event, projectId?: string | null): Promise<DesktopAgentAuthorityOverview> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    return authorityCoordinator.list(projectId);
  },
);

productionIpcHandlers.handle(
  "memolens:review-agent-pairing",
  async (event, pairingId: string, projectId: string): Promise<DesktopAuthorityActionResult> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    try {
      return await runNativeAuthorityReview(() => (
        authorityCoordinator.reviewPairing(pairingId, projectId)
      ));
    } catch (error) {
      return authorityActionFailure(error);
    }
  },
);

productionIpcHandlers.handle(
  "memolens:revoke-agent-capability",
  async (event, capabilityId: string, projectId: string): Promise<DesktopAuthorityActionResult> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    try {
      return await runNativeAuthorityReview(() => (
        authorityCoordinator.revokeCapability(capabilityId, projectId)
      ));
    } catch (error) {
      return authorityActionFailure(error);
    }
  },
);

productionIpcHandlers.handle(
  "memolens:request-decision-authority-review",
  async (
    event,
    request: DesktopDecisionReviewRequest,
  ): Promise<DesktopAuthorityActionResult> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    try {
      return await runNativeAuthorityReview(() => (
        authorityCoordinator.requestDecisionReview(request)
      ));
    } catch (error) {
      return authorityActionFailure(error);
    }
  },
);

productionIpcHandlers.handle(
  "memolens:approve-and-export-canonical-timeline",
  async (
    event,
    request: DesktopCanonicalExportApprovalRequest,
  ): Promise<DesktopCanonicalExportApprovalResult> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    return canonicalExportCoordinator.approveAndExport(request);
  },
);

productionIpcHandlers.handle(
  "memolens:save-video-artifact",
  async (
    event,
    request: DesktopArtifactSaveRequest,
  ): Promise<DesktopArtifactSaveResult> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    return videoArtifactSaveCoordinator.save(request);
  },
);

productionIpcHandlers.handle(
  "memolens:save-settings",
  async (event, settings: DesktopSettingsUpdate): Promise<DesktopSettings> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    return saveDesktopSettings(PROJECT_ROOT, settings);
  },
);

productionIpcHandlers.handle("memolens:ensure-backend", async (event) => {
  assertTrustedIpcSender(event, trustedRendererEntries);
  const settings = await loadDesktopSettings(PROJECT_ROOT);
  const status = await ensureBackendReady(PROJECT_ROOT, settings);
  if (status.state === "connected" || status.state === "started") {
    configureDesktopSessionAuthentication();
  }
  return status;
});

productionIpcHandlers.handle(
  "memolens:start-indexing",
  async (event, options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult> => {
    assertTrustedIpcSender(event, trustedRendererEntries);
    return indexingCoordinator.start(event.sender, options);
  },
);

productionIpcHandlers.handle("memolens:pause-indexing", async (event, operationId: string): Promise<boolean> => {
  assertTrustedIpcSender(event, trustedRendererEntries);
  return indexingCoordinator.pause(operationId);
});

productionIpcHandlers.handle("memolens:resume-indexing", async (event, operationId: string): Promise<boolean> => {
  assertTrustedIpcSender(event, trustedRendererEntries);
  return indexingCoordinator.resume(operationId);
});

productionIpcHandlers.handle("memolens:open-in-codex", async (event): Promise<boolean> => {
  assertTrustedIpcSender(event, trustedRendererEntries);
  const targetUrl = buildMemoLensCodexUrl(PROJECT_ROOT);
  await dispatchMemoLensLocalPluginUrl(targetUrl, PROJECT_ROOT, (validatedTargetUrl) => (
    dispatchExternalNavigation(
      validatedTargetUrl,
      (externalTargetUrl) => shell.openExternal(externalTargetUrl),
    )
  ));
  return true;
});

// Refuse to boot when a declared production invoke surface was not installed.
productionIpcHandlers.assertComplete();
}

if (hasSingleInstanceLock) app.on("second-instance", () => {
  // A second invocation may be the user's explicit "open MemoLens" gesture
  // after an Agent queued a path-free intent.  Never parse request data from
  // argv. Only a committed result may restore the primary instance's window.
  void runPendingLibraryBootstrap().catch((error: unknown) => {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[memolens-desktop] Library bootstrap broker failed :: ${message}`);
  });
});

if (hasSingleInstanceLock) app.whenReady().then(async () => {
  const { session } = require("electron") as typeof Electron.CrossProcessExports;
  let networkProfile: "online" | "offline";
  try {
    networkProfile = await configureElectronSessionNetworkPolicy(session.defaultSession);
  } catch {
    console.error("[memolens-desktop] invalid or unenforceable network policy; startup stopped");
    app.exit(1);
    return;
  }
  if (networkProfile === "offline") {
    console.log(
      "[memolens-desktop] offline boundary enabled for app fetch and Chromium session requests; OS traffic is outside this boundary",
    );
  }
  configureSessionPermissions();
  try {
    const route = await runDesktopStartupRoute({
      argv: process.argv,
      hasPendingBootstrap: await libraryBootstrapRequestSpool.hasPendingRequest(),
      async runBootstrapBroker() {
        const result = await libraryBootstrapBroker.handleNext();
        if (result !== null) {
          console.log(
            `[memolens-desktop] Library bootstrap ${result.status} :: ${result.request_id}`,
          );
        }
        return result;
      },
      registerBusinessIpc: registerProductionIpcHandlers,
      ensureBackend: runBackendBootstrap,
      createWindow,
    });
    if (route === "library_bootstrap_broker") {
      // Cancelled, expired or empty broker turns never enter business startup.
      // A committed turn has already promoted to normal and keeps its scan alive.
      app.quit();
      return;
    }
    normalStartupCompleted = true;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[memolens-desktop] startup routing failed :: ${message}`);
    app.exit(1);
    return;
  }

  app.on("activate", () => {
    void runPendingLibraryBootstrap().then((handled) => {
      if (!handled && BrowserWindow.getAllWindows().length === 0) {
        createWindow();
      }
    }).catch((error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`[memolens-desktop] Library bootstrap broker failed :: ${message}`);
    });
  });

  app.on("browser-window-created", (_, win) => {
    win.webContents.on("console-message", (_event, _level, message, line, sourceId) => {
      console.log(`[Renderer] ${message} (${sourceId}:${line})`);
    });
  });
});

let backendShutdownComplete = false;
let backendShutdownInFlight: Promise<boolean> | null = null;

app.on("before-quit", (event) => {
  if (!backendLifecycleActivated) {
    return;
  }
  if (backendShutdownComplete) {
    return;
  }
  event.preventDefault();
  if (backendShutdownInFlight !== null) {
    return;
  }
  backendShutdownInFlight = stopManagedBackend();
  void backendShutdownInFlight.then((stopped) => {
    backendShutdownInFlight = null;
    if (!stopped) {
      console.error(
        "[memolens-desktop] backend shutdown could not be confirmed; quit remains blocked",
      );
      return;
    }
    backendShutdownComplete = true;
    app.quit();
  }).catch((error: unknown) => {
    backendShutdownInFlight = null;
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[memolens-desktop] backend shutdown failed :: ${message}`);
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
