import type {
  DesktopBackendStatus,
  DesktopFolderSelection,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
  DesktopSettings,
} from "./query/types";
import type {
  DesktopArtifactSaveRequest,
  DesktopArtifactSaveResult,
} from "./video/types";

declare global {
  interface Window {
    memolensDesktop?: {
      getSettings(): Promise<DesktopSettings>;
      saveSettings(settings: DesktopSettings): Promise<DesktopSettings>;
      ensureBackend(): Promise<DesktopBackendStatus>;
      pickImageFolder(): Promise<DesktopFolderSelection | null>;
      startIndexing(options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult>;
      pauseIndexing(): Promise<boolean>;
      resumeIndexing(): Promise<boolean>;
      saveVideoArtifact(
        request: DesktopArtifactSaveRequest,
      ): Promise<DesktopArtifactSaveResult>;
      onIndexingProgress(
        callback: (progress: DesktopIndexingProgress) => void,
      ): () => void;
    };
  }
}

export {};
