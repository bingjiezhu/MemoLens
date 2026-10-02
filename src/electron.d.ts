import type {
  DesktopBackendStatus,
  DesktopFolderSelection,
  DesktopIndexingProgress,
  DesktopIndexingResult,
  DesktopIndexingStartOptions,
  DesktopSettings,
  DesktopSettingsUpdate,
} from "./query/types";
import type {
  DesktopArtifactSaveRequest,
  DesktopArtifactSaveResult,
} from "./video/types";
import type {
  DesktopAgentAuthorityOverview,
  DesktopAuthorityActionResult,
  DesktopDecisionReviewRequest,
} from "./blueprint/authorityTypes";
import type {
  DesktopCanonicalExportApprovalRequest,
  DesktopCanonicalExportApprovalResult,
} from "./blueprint/exportTypes";

declare global {
  interface Window {
    memolensDesktop?: {
      getSettings(): Promise<DesktopSettings>;
      listAgentAuthority(projectId?: string | null): Promise<DesktopAgentAuthorityOverview>;
      reviewAgentPairing(
        pairingId: string,
        projectId: string,
      ): Promise<DesktopAuthorityActionResult>;
      revokeAgentCapability(
        capabilityId: string,
        projectId: string,
      ): Promise<DesktopAuthorityActionResult>;
      requestDecisionAuthorityReview(
        request: DesktopDecisionReviewRequest,
      ): Promise<DesktopAuthorityActionResult>;
      approveAndExportCanonicalTimeline(
        request: DesktopCanonicalExportApprovalRequest,
      ): Promise<DesktopCanonicalExportApprovalResult>;
      saveSettings(update: DesktopSettingsUpdate): Promise<DesktopSettings>;
      ensureBackend(): Promise<DesktopBackendStatus>;
      pickImageFolder(): Promise<DesktopFolderSelection | null>;
      commitLibrarySelection(selectionTicket: string): Promise<DesktopSettings>;
      startIndexing(options: DesktopIndexingStartOptions): Promise<DesktopIndexingResult>;
      pauseIndexing(operationId: string): Promise<boolean>;
      resumeIndexing(operationId: string): Promise<boolean>;
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
