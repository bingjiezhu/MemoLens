import type {
  DesktopBackendStatus,
  DesktopFolderSelection,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
  DesktopSettings,
  DesktopSettingsUpdate,
} from "./types";

function getDesktopApi() {
  return window.memolensDesktop ?? null;
}

export function isElectronShell(): boolean {
  return navigator.userAgent.toLowerCase().includes("electron");
}

export function isDesktopRuntime(): boolean {
  return getDesktopApi() !== null;
}

export async function getDesktopSettings(): Promise<DesktopSettings | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.getSettings();
}

export async function saveDesktopSettings(
  update: DesktopSettingsUpdate,
): Promise<DesktopSettings | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.saveSettings(update);
}

export async function ensureDesktopBackend(): Promise<DesktopBackendStatus | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.ensureBackend();
}

export async function pickLocalImageFolder(): Promise<DesktopFolderSelection | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.pickImageFolder();
}

export async function commitLocalLibrarySelection(
  selectionTicket: string,
): Promise<DesktopSettings | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.commitLibrarySelection(selectionTicket);
}

export async function startLocalIndexing(
  options: DesktopIndexingStartOptions,
): Promise<DesktopIndexingResult | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.startIndexing(options);
}

export async function pauseLocalIndexing(operationId: string): Promise<boolean | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.pauseIndexing(operationId);
}

export async function resumeLocalIndexing(operationId: string): Promise<boolean | null> {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.resumeIndexing(operationId);
}

export function subscribeToIndexingProgress(
  callback: (progress: DesktopIndexingProgress) => void,
): (() => void) | null {
  const api = getDesktopApi();
  if (api === null) {
    return null;
  }
  return api.onIndexingProgress(callback);
}

export async function openMemoLensInCodex(): Promise<boolean> {
  const api = getDesktopApi();
  if (api === null) {
    return false;
  }
  return api.openInCodex();
}
