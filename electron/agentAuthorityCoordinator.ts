import { randomUUID } from "node:crypto";

import type {
  BlueprintDecisionUnitName,
  DesktopAgentAuthorityOverview,
  DesktopAgentCapabilitySummary,
  DesktopAuthorityActionResult,
  DesktopDecisionReviewRequest,
  DesktopDecisionUnitAuthority,
  DesktopPairingSummary,
  DesktopProjectDecisionAuthority,
} from "../src/blueprint/authorityTypes.js";

const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;
const SHA256 = /^[0-9a-f]{64}$/;
// A legal Blueprint may occupy 1 MiB before its HTTP/projection envelope is
// added. Keep the authority reader bounded without rejecting Core-valid rows.
const API_RESPONSE_BYTE_LIMIT = 2 * 1024 * 1024;
const DECISION_CONTENT_BYTE_LIMIT = 1024 * 1024;
export const AGENT_AUTHORITY_REQUEST_TIMEOUT_MS = 12_000;
export const NATIVE_DECISION_PAGE_CODE_POINT_LIMIT = 1_200;
const CAPABILITY_ACTIONS = [
  "blueprint.commit_proposal",
  "blueprint.restore_revision",
  "timeline.apply_edit",
  "timeline.apply_structural_edit",
  "timeline.restore_revision",
  "timeline.preview_media",
] as const;
const DECISION_UNITS = [
  "intent_goal",
  "intent_stance",
  "script",
  "creative_direction",
  "output",
  "material_constraints",
  "references",
  "techniques",
] as const satisfies readonly BlueprintDecisionUnitName[];
const DECISION_UNIT_LABELS: Record<BlueprintDecisionUnitName, string> = {
  intent_goal: "Goal, audience, and platform",
  intent_stance: "Creator stance",
  script: "Script",
  creative_direction: "Creative direction",
  output: "Output format",
  material_constraints: "Material constraints",
  references: "References",
  techniques: "Techniques",
};

type FetchLike = typeof fetch;
type NativePairingChoice = "approve" | "reject" | "cancel";

interface NativeTimelineHeadPresentation {
  timelineId: string;
  revision: number;
  revisionSha256: string;
  timelineContentSha256: string;
  operationId: string;
  blueprintRevision: number;
  coverageRevision: number;
}

interface NativePairingPresentation {
  pairingId: string;
  projectId: string;
  pairedSubjectId: string;
  claimedClientLabel: string;
  vendorIdentityVerified: false;
  requestedActions: string[];
  ttlSeconds: number;
  maxOperations: number;
  shortCode: string;
  observedRevision: number;
  observedTimelineHead: NativeTimelineHeadPresentation | null;
  expiresAt: string;
}

export interface NativeDecisionPresentation {
  projectId: string;
  operation: "confirm" | "revoke";
  observedRevision: number;
  decisionUnits: Array<{
    name: BlueprintDecisionUnitName;
    label: string;
    /** Complete canonical JSON derived from the exact main-fetched unit content. */
    content: string;
  }>;
}

export type NativeDecisionReviewStep =
  | {
    kind: "content";
    projectId: string;
    operation: "confirm" | "revoke";
    observedRevision: number;
    unitName: BlueprintDecisionUnitName;
    unitLabel: string;
    unitIndex: number;
    unitCount: number;
    pageIndex: number;
    pageCount: number;
    pageContent: string;
  }
  | {
    kind: "confirmation";
    projectId: string;
    operation: "confirm" | "revoke";
    observedRevision: number;
    decisionUnits: ReadonlyArray<{
      name: BlueprintDecisionUnitName;
      label: string;
    }>;
  };

export interface NativeAuthorityPresenter {
  reviewPairing(presentation: NativePairingPresentation): Promise<NativePairingChoice>;
  confirmCapabilityRevoke(capability: DesktopAgentCapabilitySummary): Promise<boolean>;
  reviewDecision(presentation: NativeDecisionPresentation): Promise<boolean>;
}

export interface AgentAuthorityCoordinatorOptions {
  apiBase: string;
  ensureBackendTrusted(forceIdentityProof?: boolean): Promise<void>;
  getMainAuthorityToken(): string;
  getDesktopSessionToken(): string;
  presenter: NativeAuthorityPresenter;
  fetchImpl?: FetchLike;
  now?: () => Date;
}

interface PairingPresentationWire {
  display: NativePairingPresentation;
  presentationDigest: string;
}

interface DecisionPresentationWire {
  display: NativeDecisionPresentation;
  presentation: Record<string, unknown>;
  presentationDigest: string;
}

interface CapabilityRevocationPresentationWire {
  display: DesktopAgentCapabilitySummary;
  presentation: Record<string, unknown>;
  presentationDigest: string;
}

export class DesktopAuthorityError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "DesktopAuthorityError";
    this.code = code;
  }
}

/** Strip format controls that can visually reorder or hide native-dialog facts. */
export function sanitizeNativeAuthorityText(value: string, maximum = 2_000): string {
  return value
    .normalize("NFKC")
    .replace(/[\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]/g, "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maximum);
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return value as Record<string, unknown>;
}

function exactRecord(
  value: unknown,
  label: string,
  expectedKeys: readonly string[],
): Record<string, unknown> {
  const row = record(value, label);
  const keys = Object.keys(row).sort();
  const expected = [...expectedKeys].sort();
  if (
    keys.length !== expected.length
    || keys.some((key, index) => key !== expected[index])
  ) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return row;
}

function array(value: unknown, label: string, maximum = 256): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return value;
}

function textValue(value: unknown, label: string, maximum = 500): string {
  if (typeof value !== "string" || value.length === 0 || value.length > maximum) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return value;
}

function identifier(value: unknown, label: string): string {
  const normalized = textValue(value, label, 200);
  if (!IDENTIFIER.test(normalized)) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return normalized;
}

function digest(value: unknown, label: string): string {
  const normalized = textValue(value, label, 64);
  if (!SHA256.test(normalized)) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return normalized;
}

function integer(value: unknown, label: string, minimum: number, maximum: number): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum || Number(value) > maximum) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return Number(value);
}

function timestamp(value: unknown, label: string): string {
  const normalized = textValue(value, label, 64);
  if (!Number.isFinite(Date.parse(normalized))) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return normalized;
}

function stringList(value: unknown, label: string, maximum = 32): string[] {
  const rows = array(value, label, maximum);
  const result = rows.map((item, index) => textValue(item, `${label}[${index}]`, 100));
  if (new Set(result).size !== result.length) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return result;
}

function capabilityActions(value: unknown, label: string): string[] {
  const result = stringList(value, label, CAPABILITY_ACTIONS.length);
  const expected = CAPABILITY_ACTIONS.filter((action) => result.includes(action));
  if (
    result.length === 0
    || result.length !== expected.length
    || result.some((action, index) => action !== expected[index])
  ) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return result;
}

function pairingBlueprintHead(
  value: unknown,
  projectId: string,
): {
  revision: number;
  contentSha256: string;
  semanticSha256: string;
  operationId: string;
} {
  const head = exactRecord(value, "Pairing observed Blueprint head", [
    "project_id",
    "revision",
    "content_sha256",
    "semantic_sha256",
    "operation_id",
  ]);
  if (identifier(head.project_id, "Observed Blueprint project id") !== projectId) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Pairing observed Blueprint project identity changed.",
    );
  }
  return {
    revision: integer(head.revision, "Observed Blueprint revision", 1, 1_000_000),
    contentSha256: digest(head.content_sha256, "Observed Blueprint content digest"),
    semanticSha256: digest(head.semantic_sha256, "Observed Blueprint semantic digest"),
    operationId: identifier(head.operation_id, "Observed Blueprint operation id"),
  };
}

function pairingTimelineHead(
  value: unknown,
  blueprintHead: ReturnType<typeof pairingBlueprintHead>,
): NativeTimelineHeadPresentation {
  const head = exactRecord(value, "Pairing observed Timeline head", [
    "revision",
    "revision_sha256",
    "timeline_id",
    "timeline_content_sha256",
    "blueprint_binding",
    "coverage_binding",
    "operation_id",
  ]);
  const blueprint = exactRecord(head.blueprint_binding, "Timeline Blueprint binding", [
    "revision",
    "content_sha256",
    "semantic_sha256",
    "operation_id",
  ]);
  const coverage = exactRecord(head.coverage_binding, "Timeline Coverage binding", [
    "revision",
    "content_sha256",
    "evidence_manifest_sha256",
    "operation_id",
  ]);
  const blueprintRevision = integer(
    blueprint.revision,
    "Timeline Blueprint binding revision",
    1,
    1_000_000,
  );
  const blueprintContentSha256 = digest(
    blueprint.content_sha256,
    "Timeline Blueprint binding content digest",
  );
  const blueprintSemanticSha256 = digest(
    blueprint.semantic_sha256,
    "Timeline Blueprint binding semantic digest",
  );
  const blueprintOperationId = identifier(
    blueprint.operation_id,
    "Timeline Blueprint binding operation id",
  );
  if (
    blueprintRevision !== blueprintHead.revision
    || blueprintContentSha256 !== blueprintHead.contentSha256
    || blueprintSemanticSha256 !== blueprintHead.semanticSha256
    || blueprintOperationId !== blueprintHead.operationId
  ) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Pairing Timeline and Blueprint observations do not match.",
    );
  }
  const coverageRevision = integer(
    coverage.revision,
    "Timeline Coverage binding revision",
    1,
    1_000_000,
  );
  digest(coverage.content_sha256, "Timeline Coverage binding content digest");
  digest(
    coverage.evidence_manifest_sha256,
    "Timeline Coverage binding evidence digest",
  );
  identifier(coverage.operation_id, "Timeline Coverage binding operation id");
  return {
    timelineId: identifier(head.timeline_id, "Observed Timeline id"),
    revision: integer(head.revision, "Observed Timeline revision", 1, 1_000_000),
    revisionSha256: digest(head.revision_sha256, "Observed Timeline revision digest"),
    timelineContentSha256: digest(
      head.timeline_content_sha256,
      "Observed Timeline content digest",
    ),
    operationId: identifier(head.operation_id, "Observed Timeline operation id"),
    blueprintRevision,
    coverageRevision,
  };
}

function claimedClientLabel(value: unknown): string {
  const normalized = sanitizeNativeAuthorityText(
    textValue(value, "Claimed client label", 200),
    120,
  );
  if (!normalized) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Claimed client label is not safely displayable.",
    );
  }
  return normalized;
}

function decisionUnitName(value: unknown, label: string): BlueprintDecisionUnitName {
  const normalized = textValue(value, label, 32);
  if (!(DECISION_UNITS as readonly string[]).includes(normalized)) {
    throw new DesktopAuthorityError("invalid_authority_response", `${label} is malformed.`);
  }
  return normalized as BlueprintDecisionUnitName;
}

function normalizeRequestedUnits(value: unknown): BlueprintDecisionUnitName[] {
  const rows = array(value, "decisionUnits", DECISION_UNITS.length);
  if (rows.length === 0) {
    throw new DesktopAuthorityError(
      "invalid_blueprint_decision_units",
      "Choose at least one decision unit.",
    );
  }
  const normalized = rows.map((item, index) => decisionUnitName(item, `decisionUnits[${index}]`));
  const expected = DECISION_UNITS.filter((name) => normalized.includes(name));
  if (
    normalized.length !== new Set(normalized).size
    || normalized.some((name, index) => name !== expected[index])
  ) {
    throw new DesktopAuthorityError(
      "invalid_blueprint_decision_units",
      "Decision units must be unique and in Blueprint contract order.",
    );
  }
  return normalized;
}

function malformedDecisionContent(): never {
  throw new DesktopAuthorityError(
    "invalid_authority_response",
    "Decision presentation content is malformed.",
  );
}

function canonicalJsonValue(value: unknown, ancestors: Set<object>): unknown {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value) || Object.is(value, -0)) malformedDecisionContent();
    return value;
  }
  if (typeof value !== "object") malformedDecisionContent();
  if (ancestors.has(value)) malformedDecisionContent();

  ancestors.add(value);
  try {
    if (Array.isArray(value)) {
      const keys = Object.keys(value);
      if (
        keys.length !== value.length
        || keys.some((key, index) => key !== String(index))
        || Reflect.ownKeys(value).length !== value.length + 1
      ) {
        malformedDecisionContent();
      }
      return value.map((item) => canonicalJsonValue(item, ancestors));
    }

    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) malformedDecisionContent();
    const keys = Object.keys(value).sort();
    if (Reflect.ownKeys(value).length !== keys.length) malformedDecisionContent();
    const canonical: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
    for (const key of keys) {
      canonical[key] = canonicalJsonValue(
        (value as Record<string, unknown>)[key],
        ancestors,
      );
    }
    return canonical;
  } finally {
    ancestors.delete(value);
  }
}

function jsonUnicodeEscape(character: string): string {
  const codePoint = character.codePointAt(0);
  if (codePoint === undefined) malformedDecisionContent();
  if (codePoint <= 0xffff) return `\\u${codePoint.toString(16).padStart(4, "0")}`;
  const scalar = codePoint - 0x10000;
  const high = 0xd800 + (scalar >> 10);
  const low = 0xdc00 + (scalar & 0x3ff);
  return `\\u${high.toString(16)}\\u${low.toString(16)}`;
}

async function boundedResponseText(response: Response, signal: AbortSignal): Promise<string> {
  const declared = response.headers.get("content-length");
  if (declared !== null) {
    if (!/^(0|[1-9][0-9]*)$/.test(declared)) {
      await response.body?.cancel().catch(() => undefined);
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        "Authority response length is malformed.",
      );
    }
    const declaredBytes = Number(declared);
    if (!Number.isSafeInteger(declaredBytes) || declaredBytes > API_RESPONSE_BYTE_LIMIT) {
      await response.body?.cancel().catch(() => undefined);
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        "Authority response is too large.",
      );
    }
  }
  if (response.body === null) return "";

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let receivedBytes = 0;
  const cancelReader = () => {
    void reader.cancel(signal.reason).catch(() => undefined);
  };
  if (signal.aborted) {
    cancelReader();
  } else {
    signal.addEventListener("abort", cancelReader, { once: true });
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      receivedBytes += value.byteLength;
      if (receivedBytes > API_RESPONSE_BYTE_LIMIT) {
        await reader.cancel();
        throw new DesktopAuthorityError(
          "invalid_authority_response",
          "Authority response is too large.",
        );
      }
      chunks.push(value);
    }
  } finally {
    signal.removeEventListener("abort", cancelReader);
    reader.releaseLock();
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(
      Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), receivedBytes),
    );
  } catch {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Authority response is not valid UTF-8.",
    );
  }
}

/**
 * Produce a complete, deterministic and lossless JSON view for native review.
 * Formatting controls are represented as visible JSON escapes instead of being
 * deleted, so two contents cannot become indistinguishable through sanitizing.
 */
export function canonicalizeNativeDecisionContent(value: unknown): string {
  const canonical = canonicalJsonValue(value, new Set<object>());
  const compact = JSON.stringify(canonical);
  if (
    typeof compact !== "string"
    || Buffer.byteLength(compact, "utf8") > DECISION_CONTENT_BYTE_LIMIT
  ) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Decision presentation content is too large.",
    );
  }
  const rendered = JSON.stringify(canonical, null, 2);
  if (typeof rendered !== "string") malformedDecisionContent();
  return rendered.replace(/[\p{Cc}\p{Cf}\u2028\u2029]/gu, (character) => (
    character === "\n" ? character : jsonUnicodeEscape(character)
  ));
}

/** Split without deleting, collapsing, or splitting a Unicode code point. */
export function paginateNativeDecisionContent(
  content: string,
  maximumCodePoints = NATIVE_DECISION_PAGE_CODE_POINT_LIMIT,
): string[] {
  if (!Number.isSafeInteger(maximumCodePoints) || maximumCodePoints < 1) {
    throw new Error("Native decision page size must be a positive safe integer.");
  }
  const codePoints = Array.from(content);
  if (codePoints.length === 0) return [""];
  const pages: string[] = [];
  for (let offset = 0; offset < codePoints.length; offset += maximumCodePoints) {
    pages.push(codePoints.slice(offset, offset + maximumCodePoints).join(""));
  }
  return pages;
}

/**
 * Main-process review protocol: every exact-content page must be accepted in
 * order, followed by a distinct final authority gesture. Any cancellation
 * fails closed before the coordinator submits the authority command.
 */
export async function conductNativeDecisionReview(
  presentation: NativeDecisionPresentation,
  presentStep: (step: NativeDecisionReviewStep) => Promise<boolean>,
): Promise<boolean> {
  for (const [unitIndex, unit] of presentation.decisionUnits.entries()) {
    const pages = paginateNativeDecisionContent(unit.content);
    for (const [pageIndex, pageContent] of pages.entries()) {
      const shouldContinue = await presentStep({
        kind: "content",
        projectId: presentation.projectId,
        operation: presentation.operation,
        observedRevision: presentation.observedRevision,
        unitName: unit.name,
        unitLabel: unit.label,
        unitIndex,
        unitCount: presentation.decisionUnits.length,
        pageIndex,
        pageCount: pages.length,
        pageContent,
      });
      if (!shouldContinue) return false;
    }
  }
  return presentStep({
    kind: "confirmation",
    projectId: presentation.projectId,
    operation: presentation.operation,
    observedRevision: presentation.observedRevision,
    decisionUnits: presentation.decisionUnits.map(({ name, label }) => ({ name, label })),
  });
}

function safeErrorMessage(payload: unknown, status: number): DesktopAuthorityError {
  try {
    const root = record(payload, "Error response");
    const error = root.error === undefined ? root : record(root.error, "Error response");
    const code = typeof error.code === "string" && IDENTIFIER.test(error.code)
      ? error.code
      : "authority_request_failed";
    const message = typeof error.message === "string" && error.message.length <= 500
      ? error.message
      : `MemoLens authority request failed (${status}).`;
    return new DesktopAuthorityError(code, message);
  } catch {
    return new DesktopAuthorityError(
      "authority_request_failed",
      `MemoLens authority request failed (${status}).`,
    );
  }
}

function pairingSummary(value: unknown): DesktopPairingSummary {
  const row = record(value, "Pairing");
  const head = record(row.observed_head, "Pairing observed head");
  const status = textValue(row.status, "Pairing status", 20);
  if (!(["pending", "rejected", "expired"] as const).includes(
    status as "pending" | "rejected" | "expired",
  )) {
    throw new DesktopAuthorityError("invalid_authority_response", "Pairing status is malformed.");
  }
  if (row.vendor_identity_verified !== false) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Pairing vendor identity claim is malformed.",
    );
  }
  if (row.user_authority_verified !== false) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Pairing user authority claim is malformed.",
    );
  }
  return {
    pairingId: identifier(row.pairing_id, "Pairing id"),
    projectId: identifier(row.project_id, "Pairing project id"),
    pairedSubjectId: identifier(row.paired_subject_id, "Paired subject id"),
    claimedClientLabel: claimedClientLabel(row.claimed_client_label),
    vendorIdentityVerified: false,
    requestedActions: capabilityActions(row.requested_actions, "Requested actions"),
    requestedTtlSeconds: integer(row.requested_ttl_seconds, "Requested TTL", 60, 1800),
    requestedMaxOperations: integer(row.requested_max_operations, "Requested operation limit", 1, 32),
    shortCode: textValue(row.short_code, "Pairing short code", 32),
    observedRevision: integer(head.revision, "Observed revision", 1, 1_000_000),
    expiresAt: timestamp(row.expires_at, "Pairing expiry"),
    status: status as DesktopPairingSummary["status"],
  };
}

function capabilitySummary(value: unknown): DesktopAgentCapabilitySummary {
  const row = record(value, "Capability");
  const status = textValue(row.status, "Capability status", 20);
  if (!(["active", "revoked", "expired", "exhausted"] as const).includes(
    status as DesktopAgentCapabilitySummary["status"],
  )) {
    throw new DesktopAuthorityError("invalid_authority_response", "Capability status is malformed.");
  }
  if (row.vendor_identity_verified !== false) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Capability vendor identity claim is malformed.",
    );
  }
  if (row.user_authority_verified !== false) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Capability user authority claim is malformed.",
    );
  }
  const maxOperations = integer(row.max_operations, "Capability operation limit", 1, 32);
  const usedOperations = integer(
    row.used_operations ?? row.operations_used,
    "Capability used operations",
    0,
    32,
  );
  const remainingOperations = integer(
    row.remaining_operations,
    "Capability remaining operations",
    0,
    32,
  );
  if (
    usedOperations > maxOperations
    || remainingOperations !== maxOperations - usedOperations
    || (status === "active" && remainingOperations === 0)
    || (status === "exhausted" && remainingOperations !== 0)
  ) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Capability operation accounting is inconsistent.",
    );
  }
  return {
    capabilityId: identifier(row.capability_id ?? row.id, "Capability id"),
    projectId: identifier(row.project_id, "Capability project id"),
    pairedSubjectId: identifier(row.paired_subject_id, "Paired subject id"),
    claimedClientLabel: claimedClientLabel(row.claimed_client_label),
    vendorIdentityVerified: false,
    actions: capabilityActions(row.actions, "Capability actions"),
    issuedAt: timestamp(row.issued_at, "Capability issue time"),
    expiresAt: timestamp(row.expires_at, "Capability expiry"),
    maxOperations,
    usedOperations,
    remainingOperations,
    status: status as DesktopAgentCapabilitySummary["status"],
  };
}

function capabilityRevocationPresentation(
  value: unknown,
): CapabilityRevocationPresentationWire {
  const root = record(value, "Capability revocation presentation response");
  const presentation = record(root.presentation, "Capability revocation presentation");
  return {
    display: capabilitySummary(presentation.capability),
    presentation,
    presentationDigest: digest(
      root.presentation_sha256,
      "Capability revocation presentation digest",
    ),
  };
}

function authorityProjection(value: unknown, projectId: string): DesktopProjectDecisionAuthority {
  const root = record(value, "Blueprint response");
  if (root.project_id !== projectId) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Blueprint authority project identity changed.",
    );
  }
  const creationAuthority = record(root.creation_authority, "Blueprint creation authority");
  if (
    creationAuthority.state !== "unverified"
    || creationAuthority.verified !== false
  ) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Blueprint creation authority is malformed.",
    );
  }
  const projection = record(root.authority_projection, "Blueprint authority projection");
  digest(projection.as_of_content_sha256, "Authority projection content digest");
  const asOfRevision = integer(
    projection.as_of_revision,
    "Authority projection revision",
    1,
    1_000_000,
  );
  const unitMap = record(projection.decision_units, "Blueprint decision units");
  const keys = Object.keys(unitMap);
  if (keys.length !== DECISION_UNITS.length || DECISION_UNITS.some((name) => !(name in unitMap))) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Blueprint authority projection is incomplete.",
    );
  }
  const units: DesktopDecisionUnitAuthority[] = DECISION_UNITS.map((name) => {
    const unit = record(unitMap[name], `Decision unit ${name}`);
    digest(unit.semantic_sha256, `Decision unit ${name} semantic digest`);
    if (unit.state !== "confirmed" && unit.state !== "unverified") {
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        `Decision unit ${name} authority state is malformed.`,
      );
    }
    if (
      unit.verified !== undefined
      && unit.verified !== (unit.state === "confirmed")
    ) {
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        `Decision unit ${name} authority claims are inconsistent.`,
      );
    }
    if (unit.confirmed !== (unit.state === "confirmed")) {
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        `Decision unit ${name} confirmation claims are inconsistent.`,
      );
    }
    const state = unit.state;
    let confirmedAt: string | null = null;
    if (state === "confirmed") {
      const active = record(
        unit.active_confirmation,
        `Decision unit ${name} active confirmation`,
      );
      identifier(active.event_id, `Decision unit ${name} confirmation id`);
      const observedRevision = integer(
        active.observed_revision,
        `Decision unit ${name} confirmation revision`,
        1,
        1_000_000,
      );
      if (observedRevision > asOfRevision) {
        throw new DesktopAuthorityError(
          "invalid_authority_response",
          `Decision unit ${name} confirmation points beyond the authority projection.`,
        );
      }
      digest(
        active.presentation_sha256,
        `Decision unit ${name} confirmation presentation digest`,
      );
      confirmedAt = timestamp(
        active.confirmed_at,
        `Decision unit ${name} confirmation time`,
      );
    } else if (unit.active_confirmation !== null) {
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        `Decision unit ${name} has inactive confirmation metadata.`,
      );
    }
    return {
      name,
      label: DECISION_UNIT_LABELS[name],
      state,
      confirmedAt,
    };
  });
  const state = textValue(projection.state, "Blueprint authority state", 30);
  if (!(["unverified", "partially_confirmed", "confirmed"] as const).includes(
    state as DesktopProjectDecisionAuthority["state"],
  )) {
    throw new DesktopAuthorityError("invalid_authority_response", "Blueprint authority state is malformed.");
  }
  const confirmedCount = units.filter((unit) => unit.state === "confirmed").length;
  const suppliedCount = integer(
    projection.confirmed_decision_unit_count,
    "Confirmed decision unit count",
    0,
    DECISION_UNITS.length,
  );
  const suppliedTotal = integer(
    projection.decision_unit_count,
    "Decision unit count",
    DECISION_UNITS.length,
    DECISION_UNITS.length,
  );
  if (suppliedCount !== confirmedCount || suppliedTotal !== DECISION_UNITS.length) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Blueprint authority counts are inconsistent.",
    );
  }
  const expectedState = confirmedCount === 0
    ? "unverified"
    : confirmedCount === DECISION_UNITS.length
      ? "confirmed"
      : "partially_confirmed";
  if (state !== expectedState) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Blueprint authority state conflicts with its decision units.",
    );
  }
  const revision = integer(
    root.revision ?? projection.as_of_revision,
    "Blueprint revision",
    1,
    1_000_000,
  );
  if (asOfRevision !== revision) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Blueprint revision and authority projection do not match.",
    );
  }
  return {
    projectId,
    revision,
    state: state as DesktopProjectDecisionAuthority["state"],
    confirmedDecisionUnitCount: confirmedCount,
    decisionUnitCount: 8,
    asOfRevision,
    decisionUnits: units,
  };
}

function pairingPresentation(value: unknown): PairingPresentationWire {
  const root = record(value, "Pairing presentation response");
  const row = record(root.presentation, "Pairing presentation");
  if (row.vendor_identity_verified !== undefined && row.vendor_identity_verified !== false) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      "Pairing vendor identity claim is malformed.",
    );
  }
  const projectId = identifier(row.project_id, "Pairing project id");
  const requestedActions = capabilityActions(row.actions, "Requested actions");
  const blueprintHead = pairingBlueprintHead(row.observed_head, projectId);
  const requestsTimelineScope = requestedActions.some((action) => (
    action === "timeline.apply_edit"
    || action === "timeline.apply_structural_edit"
    || action === "timeline.restore_revision"
    || action === "timeline.preview_media"
  ));
  if (requestsTimelineScope !== (row.observed_timeline_head !== undefined)) {
    throw new DesktopAuthorityError(
      "invalid_authority_response",
      requestsTimelineScope
        ? "A Timeline-scoped pairing must bind the observed canonical Timeline head."
        : "A Blueprint-only pairing must not include a Timeline head.",
    );
  }
  const observedTimelineHead = requestsTimelineScope
    ? pairingTimelineHead(row.observed_timeline_head, blueprintHead)
    : null;
  return {
    presentationDigest: digest(
      root.presentation_sha256,
      "Pairing presentation digest",
    ),
    display: {
      pairingId: identifier(row.pairing_id, "Pairing id"),
      projectId,
      pairedSubjectId: identifier(row.paired_subject_id, "Paired subject id"),
      claimedClientLabel: claimedClientLabel(row.claimed_client_label),
      vendorIdentityVerified: false,
      requestedActions,
      ttlSeconds: integer(
        row.ttl_seconds,
        "Pairing TTL",
        60,
        1800,
      ),
      maxOperations: integer(
        row.max_operations,
        "Pairing operation limit",
        1,
        32,
      ),
      shortCode: textValue(root.display_code, "Pairing short code", 32),
      observedRevision: blueprintHead.revision,
      observedTimelineHead,
      expiresAt: timestamp(root.expires_at, "Pairing presentation expiry"),
    },
  };
}

function decisionPresentation(value: unknown): DecisionPresentationWire {
  const root = record(value, "Decision presentation response");
  const row = record(root.presentation, "Decision presentation");
  const operation = textValue(row.authority_operation, "Decision operation", 16);
  if (operation !== "confirm" && operation !== "revoke") {
    throw new DesktopAuthorityError("invalid_authority_response", "Decision operation is malformed.");
  }
  const head = exactRecord(row.observed_head, "Decision observed head", [
    "revision",
    "content_sha256",
  ]);
  digest(head.content_sha256, "Decision observed Blueprint content digest");
  const units = array(row.decision_units, "Decision presentation units", DECISION_UNITS.length)
    .map((value, index) => {
      const unit = record(value, `Decision presentation unit ${index}`);
      const name = decisionUnitName(unit.name, `Decision presentation unit ${index} name`);
      // Validate the binding without ever forwarding the digest to the renderer.
      digest(unit.semantic_sha256, `Decision presentation unit ${index} digest`);
      if (!("content" in unit)) {
        throw new DesktopAuthorityError(
          "invalid_authority_response",
          `Decision presentation unit ${index} content is missing.`,
        );
      }
      return {
        name,
        label: DECISION_UNIT_LABELS[name],
        content: canonicalizeNativeDecisionContent(unit.content),
      };
    });
  normalizeRequestedUnits(units.map((unit) => unit.name));
  return {
    presentation: row,
    presentationDigest: digest(
      root.presentation_sha256,
      "Decision presentation digest",
    ),
    display: {
      projectId: identifier(row.project_id, "Decision project id"),
      operation,
      observedRevision: integer(head.revision, "Decision observed revision", 1, 1_000_000),
      decisionUnits: units,
    },
  };
}

export class AgentAuthorityCoordinator {
  private readonly apiBase: string;
  private readonly ensureBackendTrusted: (forceIdentityProof?: boolean) => Promise<void>;
  private readonly getMainAuthorityToken: () => string;
  private readonly getDesktopSessionToken: () => string;
  private readonly presenter: NativeAuthorityPresenter;
  private readonly fetchImpl: FetchLike;
  private readonly now: () => Date;

  constructor(options: AgentAuthorityCoordinatorOptions) {
    const parsed = new URL(options.apiBase);
    if (
      parsed.protocol !== "http:"
      || parsed.hostname !== "127.0.0.1"
      || parsed.username
      || parsed.password
      || parsed.pathname !== "/"
      || parsed.search
      || parsed.hash
    ) {
      throw new Error("Agent authority coordinator requires an exact IPv4 loopback API base.");
    }
    this.apiBase = parsed.origin;
    this.ensureBackendTrusted = options.ensureBackendTrusted;
    this.getMainAuthorityToken = options.getMainAuthorityToken;
    this.getDesktopSessionToken = options.getDesktopSessionToken;
    this.presenter = options.presenter;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.now = options.now ?? (() => new Date());
  }

  async list(projectId?: string | null): Promise<DesktopAgentAuthorityOverview> {
    await this.ensureBackendTrusted(false);
    const [pairingPayload, capabilityPayload] = await Promise.all([
      this.mainRequest("/v1/main/agent/pairings"),
      this.mainRequest("/v1/main/agent/capabilities"),
    ]);
    const pairingRoot = record(pairingPayload, "Pairing list response");
    const capabilityRoot = record(capabilityPayload, "Capability list response");
    const pairings = array(pairingRoot.pairings, "Pairings").map(pairingSummary);
    const capabilities = array(capabilityRoot.capabilities, "Capabilities").map(capabilitySummary);
    const normalizedProject = projectId ? this.requireIdentifier(projectId, "Project id") : null;
    const projectAuthority = normalizedProject
      ? await this.readProjectAuthority(normalizedProject)
      : null;
    return {
      pairings,
      capabilities,
      projectAuthority,
      refreshedAt: this.now().toISOString(),
    };
  }

  async reviewPairing(
    pairingId: string,
    expectedProjectId: string,
  ): Promise<DesktopAuthorityActionResult> {
    const normalizedPairing = this.requireIdentifier(pairingId, "Pairing id");
    const normalizedProject = this.requireIdentifier(expectedProjectId, "Project id");
    await this.ensureBackendTrusted(true);
    const wire = pairingPresentation(await this.mainRequest(
      `/v1/main/agent/pairings/${encodeURIComponent(normalizedPairing)}/presentation`,
    ));
    if (
      wire.display.pairingId !== normalizedPairing
      || wire.display.projectId !== normalizedProject
    ) {
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        "Pairing presentation target identity changed.",
      );
    }
    const choice = await this.presenter.reviewPairing(wire.display);
    if (choice === "cancel") {
      return {
        status: "cancelled",
        message: "Pairing review was cancelled. No authority changed.",
        projectAuthority: null,
      };
    }
    await this.ensureBackendTrusted(true);
    const action = choice === "approve" ? "approve" : "reject";
    await this.mainRequest(
      `/v1/main/agent/pairings/${encodeURIComponent(normalizedPairing)}/${action}`,
      {
        presentation_sha256: wire.presentationDigest,
        native_gesture_nonce: randomUUID(),
      },
    );
    return {
      status: choice === "approve" ? "completed" : "rejected",
      message: choice === "approve"
        ? "The Agent is paired only for the reviewed project, actions, and time window."
        : "The pairing request was rejected.",
      projectAuthority: choice === "approve"
        ? await this.readProjectAuthority(wire.display.projectId)
        : null,
    };
  }

  async revokeCapability(
    capabilityId: string,
    expectedProjectId: string,
  ): Promise<DesktopAuthorityActionResult> {
    const normalizedCapability = this.requireIdentifier(capabilityId, "Capability id");
    const normalizedProject = this.requireIdentifier(expectedProjectId, "Project id");
    await this.ensureBackendTrusted(true);
    const wire = capabilityRevocationPresentation(await this.mainRequest(
      `/v1/main/agent/capabilities/${encodeURIComponent(normalizedCapability)}/revoke/presentation`,
    ));
    const capability = wire.display;
    if (
      capability.capabilityId !== normalizedCapability
      || capability.projectId !== normalizedProject
      || capability.status !== "active"
    ) {
      throw new DesktopAuthorityError(
        "agent_pairing_revoked",
        "That Agent capability is no longer active.",
      );
    }
    if (!(await this.presenter.confirmCapabilityRevoke(capability))) {
      return {
        status: "cancelled",
        message: "Capability revocation was cancelled. No authority changed.",
        projectAuthority: null,
      };
    }
    await this.ensureBackendTrusted(true);
    await this.mainRequest(
      `/v1/main/agent/capabilities/${encodeURIComponent(normalizedCapability)}/revoke`,
      {
        presentation: wire.presentation,
        presentation_sha256: wire.presentationDigest,
        native_gesture_nonce: randomUUID(),
      },
    );
    return {
      status: "completed",
      message: "The Agent capability was revoked. Completed project revisions remain available.",
      projectAuthority: await this.readProjectAuthority(capability.projectId),
    };
  }

  async requestDecisionReview(
    request: DesktopDecisionReviewRequest,
  ): Promise<DesktopAuthorityActionResult> {
    if (
      request === null
      || typeof request !== "object"
      || Array.isArray(request)
      || Object.keys(request).sort().join(",") !== "decisionUnits,operation,projectId"
    ) {
      throw new DesktopAuthorityError("invalid_authority_request", "Decision review request is malformed.");
    }
    const projectId = this.requireIdentifier(request.projectId, "Project id");
    if (request.operation !== "confirm" && request.operation !== "revoke") {
      throw new DesktopAuthorityError("invalid_authority_request", "Decision review action is malformed.");
    }
    const units = normalizeRequestedUnits(request.decisionUnits);
    await this.ensureBackendTrusted(true);
    const presentation = decisionPresentation(await this.mainRequest(
      `/v1/main/creative/projects/${encodeURIComponent(projectId)}/blueprint/authority/presentation`,
      { operation: request.operation, decision_units: units },
    ));
    if (
      presentation.display.projectId !== projectId
      || presentation.display.operation !== request.operation
      || presentation.display.decisionUnits.length !== units.length
      || presentation.display.decisionUnits.some((unit, index) => unit.name !== units[index])
    ) {
      throw new DesktopAuthorityError(
        "invalid_authority_response",
        "Decision presentation does not match the requested review.",
      );
    }
    if (!(await this.presenter.reviewDecision(presentation.display))) {
      return {
        status: "cancelled",
        message: "Decision review was cancelled. No authority changed.",
        projectAuthority: await this.readProjectAuthority(projectId),
      };
    }
    // A native dialog can remain open while the backend restarts or the head
    // advances. Re-prove backend identity and let Core enforce exact-head CAS.
    await this.ensureBackendTrusted(true);
    const result = await this.mainRequest(
      `/v1/main/creative/projects/${encodeURIComponent(projectId)}/blueprint/authority/${request.operation}`,
      {
        presentation: presentation.presentation,
        presentation_sha256: presentation.presentationDigest,
        native_gesture_nonce: randomUUID(),
      },
      { "Idempotency-Key": `authority-${randomUUID()}` },
    );
    const projection = authorityProjection(result, projectId);
    return {
      status: "completed",
      message: request.operation === "confirm"
        ? "The selected decisions were confirmed for this exact Blueprint content."
        : "The selected decision authority was revoked; Blueprint content was not changed.",
      projectAuthority: projection,
    };
  }

  private requireIdentifier(value: unknown, label: string): string {
    if (typeof value !== "string" || !IDENTIFIER.test(value)) {
      throw new DesktopAuthorityError("invalid_authority_request", `${label} is invalid.`);
    }
    return value;
  }

  private async readProjectAuthority(projectId: string): Promise<DesktopProjectDecisionAuthority> {
    const payload = await this.desktopReadRequest(
      `/v1/creative/projects/${encodeURIComponent(projectId)}/blueprint`,
    );
    return authorityProjection(payload, projectId);
  }

  private async mainRequest(
    path: string,
    body?: Record<string, unknown>,
    extraHeaders?: Readonly<Record<string, string>>,
  ): Promise<unknown> {
    return this.request(
      path,
      "X-MemoLens-Main-Authority",
      this.getMainAuthorityToken(),
      body,
      extraHeaders,
    );
  }

  private async desktopReadRequest(path: string): Promise<unknown> {
    return this.request(path, "X-MemoLens-Desktop-Token", this.getDesktopSessionToken());
  }

  private async request(
    path: string,
    credentialHeader: "X-MemoLens-Main-Authority" | "X-MemoLens-Desktop-Token",
    credential: string,
    body?: Record<string, unknown>,
    extraHeaders?: Readonly<Record<string, string>>,
  ): Promise<unknown> {
    if (!path.startsWith("/v1/") || path.includes("?") || path.includes("#")) {
      throw new DesktopAuthorityError("invalid_authority_request", "Authority endpoint is invalid.");
    }
    if (
      extraHeaders
      && Object.keys(extraHeaders).some((name) => name.toLowerCase() !== "idempotency-key")
    ) {
      throw new DesktopAuthorityError(
        "invalid_authority_request",
        "Authority request headers are invalid.",
      );
    }
    const headers: Record<string, string> = {
      Accept: "application/json",
      ...extraHeaders,
      [credentialHeader]: credential,
    };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const controller = new AbortController();
    const timeoutError = new DesktopAuthorityError(
      "authority_request_timeout",
      "MemoLens authority request timed out.",
    );
    let reachedDeadline = false;
    let timeoutId: ReturnType<typeof setTimeout> | null = null;
    const deadline = new Promise<never>((_resolve, reject) => {
      timeoutId = setTimeout(() => {
        reachedDeadline = true;
        controller.abort(timeoutError);
        reject(timeoutError);
      }, AGENT_AUTHORITY_REQUEST_TIMEOUT_MS);
    });
    const exchange = async (): Promise<unknown> => {
      const response = await this.fetchImpl(`${this.apiBase}${path}`, {
        method: body === undefined ? "GET" : "POST",
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: "error",
        signal: controller.signal,
      });
      const responseText = await boundedResponseText(response, controller.signal);
      let payload: unknown;
      try {
        payload = JSON.parse(responseText) as unknown;
      } catch {
        throw new DesktopAuthorityError("invalid_authority_response", "Authority response is not JSON.");
      }
      if (!response.ok) {
        throw safeErrorMessage(payload, response.status);
      }
      return payload;
    };
    try {
      return await Promise.race([exchange(), deadline]);
    } catch (error) {
      if (reachedDeadline) throw timeoutError;
      throw error;
    } finally {
      if (timeoutId !== null) clearTimeout(timeoutId);
    }
  }
}
