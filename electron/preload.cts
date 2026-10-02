import type {
  DesktopBackendStatus,
  DesktopFolderSelection,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
  DesktopSettings,
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
  DesktopCanonicalExportApprovalRequest,
  DesktopCanonicalExportApprovalResult,
} from "../src/blueprint/exportTypes.js";
import type {
  ElectronInvokeChannel,
  ElectronOutboundEventChannel,
} from "./productionSurfaceRegistry.js";

// Sandboxed preload scripts run in Electron's restricted CommonJS context.
// `electron` is one of the explicitly supported modules there; Node built-ins
// such as `node:module` and ESM imports are intentionally unavailable.
const { contextBridge, ipcRenderer } = require("electron") as typeof Electron.Renderer;

function invokeProductionIpc<Result>(
  channel: ElectronInvokeChannel,
  ...args: unknown[]
): Promise<Result> {
  return ipcRenderer.invoke(channel, ...args) as Promise<Result>;
}

function subscribeProductionIpcEvent<Payload>(
  channel: ElectronOutboundEventChannel,
  callback: (payload: Payload) => void,
): () => void {
  const listener = (_event: Electron.IpcRendererEvent, payload: Payload) => {
    callback(payload);
  };
  ipcRenderer.on(channel, listener);
  return () => {
    ipcRenderer.removeListener(channel, listener);
  };
}

contextBridge.exposeInMainWorld("memolensDesktop", {
  getSettings(): Promise<DesktopSettings> {
    return invokeProductionIpc<DesktopSettings>("memolens:get-settings");
  },
  listAgentAuthority(projectId?: string | null): Promise<DesktopAgentAuthorityOverview> {
    return invokeProductionIpc<DesktopAgentAuthorityOverview>(
      "memolens:list-agent-authority",
      projectId ?? null,
    );
  },
  reviewAgentPairing(
    pairingId: string,
    projectId: string,
  ): Promise<DesktopAuthorityActionResult> {
    return invokeProductionIpc<DesktopAuthorityActionResult>(
      "memolens:review-agent-pairing",
      pairingId,
      projectId,
    );
  },
  revokeAgentCapability(
    capabilityId: string,
    projectId: string,
  ): Promise<DesktopAuthorityActionResult> {
    return invokeProductionIpc<DesktopAuthorityActionResult>(
      "memolens:revoke-agent-capability",
      capabilityId,
      projectId,
    );
  },
  requestDecisionAuthorityReview(
    request: DesktopDecisionReviewRequest,
  ): Promise<DesktopAuthorityActionResult> {
    return invokeProductionIpc<DesktopAuthorityActionResult>(
      "memolens:request-decision-authority-review",
      request,
    );
  },
  approveAndExportCanonicalTimeline(
    request: DesktopCanonicalExportApprovalRequest,
  ): Promise<DesktopCanonicalExportApprovalResult> {
    return invokeProductionIpc<DesktopCanonicalExportApprovalResult>(
      "memolens:approve-and-export-canonical-timeline",
      request,
    );
  },
  saveSettings(update: DesktopSettingsUpdate): Promise<DesktopSettings> {
    return invokeProductionIpc<DesktopSettings>("memolens:save-settings", update);
  },
  ensureBackend(): Promise<DesktopBackendStatus> {
    return invokeProductionIpc<DesktopBackendStatus>("memolens:ensure-backend");
  },
  pickImageFolder(): Promise<DesktopFolderSelection | null> {
    return invokeProductionIpc<DesktopFolderSelection | null>("memolens:pick-image-folder");
  },
  commitLibrarySelection(selectionTicket: string): Promise<DesktopSettings> {
    return invokeProductionIpc<DesktopSettings>(
      "memolens:commit-library-selection",
      selectionTicket,
    );
  },
  startIndexing(options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult> {
    return invokeProductionIpc<DesktopIndexingResult>("memolens:start-indexing", options);
  },
  pauseIndexing(operationId: string): Promise<boolean> {
    return invokeProductionIpc<boolean>("memolens:pause-indexing", operationId);
  },
  resumeIndexing(operationId: string): Promise<boolean> {
    return invokeProductionIpc<boolean>("memolens:resume-indexing", operationId);
  },
  saveVideoArtifact(request: DesktopArtifactSaveRequest): Promise<DesktopArtifactSaveResult> {
    return invokeProductionIpc<DesktopArtifactSaveResult>(
      "memolens:save-video-artifact",
      request,
    );
  },
  openInCodex(): Promise<boolean> {
    return invokeProductionIpc<boolean>("memolens:open-in-codex");
  },
  onIndexingProgress(callback: (progress: DesktopIndexingProgress) => void): () => void {
    return subscribeProductionIpcEvent<DesktopIndexingProgress>(
      "memolens:indexing-progress",
      callback,
    );
  },
});
