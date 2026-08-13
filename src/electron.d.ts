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
      commitLibrarySelection(selection: DesktopFolderSelection): Promise<DesktopSettings>;
      startIndexing(options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult>;
      pauseIndexing(): Promise<boolean>;
      resumeIndexing(): Promise<boolean>;
      saveVideoArtifact(
        request: DesktopArtifactSaveRequest,
      ): Promise<DesktopArtifactSaveResult>;
      openInCodex(): Promise<boolean>;
      onIndexingProgress(
        callback: (progress: DesktopIndexingProgress) => void,
      ): () => void;
    };
  }
}

export {};
