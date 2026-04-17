import { createRequire } from "node:module";

import type {
  DesktopBackendStatus,
  DesktopFolderSelection,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
  DesktopSettings,
} from "../src/query/types.js";

const require = createRequire(import.meta.url);
const { contextBridge, ipcRenderer } = require("electron") as typeof Electron.Renderer;

contextBridge.exposeInMainWorld("memolensDesktop", {
  getSettings(): Promise<DesktopSettings> {
    return ipcRenderer.invoke("memolens:get-settings");
  },
  saveSettings(settings: DesktopSettings): Promise<DesktopSettings> {
    return ipcRenderer.invoke("memolens:save-settings", settings);
  },
  ensureBackend(): Promise<DesktopBackendStatus> {
    return ipcRenderer.invoke("memolens:ensure-backend");
  },
  pickImageFolder(): Promise<DesktopFolderSelection | null> {
    return ipcRenderer.invoke("memolens:pick-image-folder");
  },
  startIndexing(options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult> {
    return ipcRenderer.invoke("memolens:start-indexing", options);
  },
  pauseIndexing(): Promise<boolean> {
    return ipcRenderer.invoke("memolens:pause-indexing");
  },
  resumeIndexing(): Promise<boolean> {
    return ipcRenderer.invoke("memolens:resume-indexing");
  },
  onIndexingProgress(callback: (progress: DesktopIndexingProgress) => void): () => void {
    const listener = (_event: Electron.IpcRendererEvent, progress: DesktopIndexingProgress) => {
      callback(progress);
    };
    ipcRenderer.on("memolens:indexing-progress", listener);
    return () => {
      ipcRenderer.removeListener("memolens:indexing-progress", listener);
    };
  },
});
