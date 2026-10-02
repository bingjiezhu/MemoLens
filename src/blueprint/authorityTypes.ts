export const BLUEPRINT_DECISION_UNITS = [
  "intent_goal",
  "intent_stance",
  "script",
  "creative_direction",
  "output",
  "material_constraints",
  "references",
  "techniques",
] as const;

export type BlueprintDecisionUnitName = typeof BLUEPRINT_DECISION_UNITS[number];
export type BlueprintDecisionAuthorityState = "confirmed" | "unverified";

export interface DesktopPairingSummary {
  pairingId: string;
  projectId: string;
  pairedSubjectId: string;
  claimedClientLabel: string;
  vendorIdentityVerified: false;
  requestedActions: string[];
  requestedTtlSeconds: number;
  requestedMaxOperations: number;
  shortCode: string;
  observedRevision: number;
  expiresAt: string;
  status: "pending" | "rejected" | "expired";
}

export interface DesktopAgentCapabilitySummary {
  capabilityId: string;
  projectId: string;
  pairedSubjectId: string;
  claimedClientLabel: string;
  vendorIdentityVerified: false;
  actions: string[];
  issuedAt: string;
  expiresAt: string;
  maxOperations: number;
  usedOperations: number;
  remainingOperations: number;
  status: "active" | "revoked" | "expired" | "exhausted";
}

export interface DesktopDecisionUnitAuthority {
  name: BlueprintDecisionUnitName;
  label: string;
  state: BlueprintDecisionAuthorityState;
  confirmedAt: string | null;
}

export interface DesktopProjectDecisionAuthority {
  projectId: string;
  revision: number;
  state: "unverified" | "partially_confirmed" | "confirmed";
  confirmedDecisionUnitCount: number;
  decisionUnitCount: 8;
  asOfRevision: number;
  decisionUnits: DesktopDecisionUnitAuthority[];
}

export interface DesktopAgentAuthorityOverview {
  pairings: DesktopPairingSummary[];
  capabilities: DesktopAgentCapabilitySummary[];
  projectAuthority: DesktopProjectDecisionAuthority | null;
  refreshedAt: string;
}

export interface DesktopAuthorityActionResult {
  status: "completed" | "cancelled" | "rejected" | "failed";
  message: string;
  projectAuthority: DesktopProjectDecisionAuthority | null;
}

export interface DesktopDecisionReviewRequest {
  projectId: string;
  operation: "confirm" | "revoke";
  decisionUnits: BlueprintDecisionUnitName[];
}
