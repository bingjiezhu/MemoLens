import { createHash, randomUUID } from "node:crypto";
import { isAbsolute, resolve } from "node:path";

import {
  normalizeCanonicalExportCommandResult,
  normalizeCanonicalExportPackageBasename,
  normalizeCanonicalExportPresentationEnvelope,
  normalizeDesktopCanonicalExportApprovalRequest,
} from "../src/blueprint/exportModel.js";
import type {
  CanonicalExportPresentation,
  DesktopCanonicalExportApprovalRequest,
  DesktopCanonicalExportApprovalResult,
} from "../src/blueprint/exportTypes.js";

const API_RESPONSE_BYTE_LIMIT = 2 * 1024 * 1024;
export const CANONICAL_EXPORT_REQUEST_TIMEOUT_MS = 12_000;

type FetchLike = typeof fetch;

export type NativeCanonicalExportDestination =
  | { cancelled: true }
  | {
      cancelled: false;
      canonicalPath: string;
      selectionIdentity: { device: string; inode: string };
      release(): Promise<void>;
    };

export interface NativeCanonicalExportPresenter {
  chooseDestination(
    presentation: CanonicalExportPresentation,
    presentationSha256: string,
    suggestedPackageName: string,
  ): Promise<NativeCanonicalExportDestination>;
}

export interface CanonicalExportCoordinatorOptions {
  apiBase: string;
  ensureBackendTrusted(forceIdentityProof?: boolean): Promise<void>;
  getMainAuthorityToken(): string;
  presenter: NativeCanonicalExportPresenter;
  fetchImpl?: FetchLike;
  createNonce?: () => string;
}

export class CanonicalExportCoordinatorError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "CanonicalExportCoordinatorError";
    this.code = code;
  }
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value) || Object.is(value, -0)) {
      throw new CanonicalExportCoordinatorError(
        "invalid_export_response",
        "The canonical export presentation is malformed.",
      );
    }
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value !== "object") {
    throw new CanonicalExportCoordinatorError(
      "invalid_export_response",
      "The canonical export presentation is malformed.",
    );
  }
  const row = value as Record<string, unknown>;
  return `{${Object.keys(row).sort().map((key) => (
    `${JSON.stringify(key)}:${canonicalJson(row[key])}`
  )).join(",")}}`;
}

export function canonicalExportPresentationSha256(
  presentation: CanonicalExportPresentation,
): string {
  return createHash("sha256").update(canonicalJson(presentation), "utf8").digest("hex");
}

async function boundedResponseJson(response: Response, signal: AbortSignal): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (declared !== null) {
    if (!/^(?:0|[1-9][0-9]*)$/.test(declared)) {
      await response.body?.cancel().catch(() => undefined);
      throw new CanonicalExportCoordinatorError(
        "invalid_export_response",
        "The canonical export service returned an invalid response.",
      );
    }
    const size = Number(declared);
    if (!Number.isSafeInteger(size) || size > API_RESPONSE_BYTE_LIMIT) {
      await response.body?.cancel().catch(() => undefined);
      throw new CanonicalExportCoordinatorError(
        "invalid_export_response",
        "The canonical export service response exceeded its safety limit.",
      );
    }
  }

  if (response.body === null) {
    const text = await response.text();
    if (Buffer.byteLength(text, "utf8") > API_RESPONSE_BYTE_LIMIT) {
      throw new CanonicalExportCoordinatorError(
        "invalid_export_response",
        "The canonical export service response exceeded its safety limit.",
      );
    }
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new CanonicalExportCoordinatorError(
        "invalid_export_response",
        "The canonical export service response is not JSON.",
      );
    }
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      if (signal.aborted) throw signal.reason;
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > API_RESPONSE_BYTE_LIMIT) {
        await reader.cancel().catch(() => undefined);
        throw new CanonicalExportCoordinatorError(
          "invalid_export_response",
          "The canonical export service response exceeded its safety limit.",
        );
      }
      chunks.push(next.value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), total);
  try {
    return JSON.parse(bytes.toString("utf8")) as unknown;
  } catch {
    throw new CanonicalExportCoordinatorError(
      "invalid_export_response",
      "The canonical export service response is not JSON.",
    );
  }
}

function safeHttpFailure(status: number): CanonicalExportCoordinatorError {
  if (status >= 500) {
    return new CanonicalExportCoordinatorError(
      "canonical_export_service_failed",
      "The local canonical export service failed.",
    );
  }
  if (status === 409 || status === 412) {
    return new CanonicalExportCoordinatorError(
      "canonical_export_stale",
      "The exact Timeline or export authority changed. Refresh before approving again.",
    );
  }
  if (status === 401 || status === 403) {
    return new CanonicalExportCoordinatorError(
      "canonical_export_trust_failed",
      "MemoLens could not verify the local canonical export service.",
    );
  }
  return new CanonicalExportCoordinatorError(
    "canonical_export_request_failed",
    `The canonical export service rejected the request (${status}).`,
  );
}

function canonicalDestinationPath(value: unknown): string {
  if (
    typeof value !== "string"
    || value.length === 0
    || value.length > 4096
    || value.includes("\u0000")
    || !isAbsolute(value)
  ) {
    throw new CanonicalExportCoordinatorError(
      "invalid_native_destination",
      "MemoLens rejected an invalid native export destination.",
    );
  }
  const canonical = resolve(value);
  if (canonical !== value) {
    throw new CanonicalExportCoordinatorError(
      "invalid_native_destination",
      "MemoLens rejected a non-canonical native export destination.",
    );
  }
  return canonical;
}

function canonicalDestinationIdentity(
  value: unknown,
): { device: string; inode: string } {
  if (
    typeof value !== "object"
    || value === null
    || Array.isArray(value)
    || Object.keys(value).sort().join(",") !== "device,inode"
  ) {
    throw new CanonicalExportCoordinatorError(
      "invalid_native_destination",
      "MemoLens rejected an invalid native export directory identity.",
    );
  }
  const row = value as Record<string, unknown>;
  if (
    typeof row.device !== "string"
    || !/^(?:0|[1-9][0-9]{0,30})$/.test(row.device)
    || typeof row.inode !== "string"
    || !/^[1-9][0-9]{0,30}$/.test(row.inode)
  ) {
    throw new CanonicalExportCoordinatorError(
      "invalid_native_destination",
      "MemoLens rejected an invalid native export directory identity.",
    );
  }
  return { device: row.device, inode: row.inode };
}

function safeFailureMessage(error: unknown): string {
  return error instanceof CanonicalExportCoordinatorError
    ? error.message.slice(0, 500)
    : "MemoLens could not complete the native canonical export approval.";
}

function isKnownCommandRejection(error: unknown): boolean {
  return error instanceof CanonicalExportCoordinatorError
    && [
      "canonical_export_stale",
      "canonical_export_trust_failed",
      "canonical_export_request_failed",
    ].includes(error.code);
}

function outcomeUnknown(error: unknown): CanonicalExportCoordinatorError {
  const detail = error instanceof CanonicalExportCoordinatorError
    ? error.message
    : "The local export service connection ended before MemoLens received a trustworthy result.";
  return new CanonicalExportCoordinatorError(
    "canonical_export_outcome_unknown",
    `The export command outcome is unknown. Refresh the canonical workspace before approving another export. ${detail}`,
  );
}

function presentationMatchesCapturedIntent(
  request: DesktopCanonicalExportApprovalRequest,
  presentation: CanonicalExportPresentation,
): boolean {
  const expected = request.expectedTimelineBinding;
  const current = presentation.timeline_binding;
  return presentation.database_uuid === request.databaseUuid
    && current.revision === expected.revision
    && current.revision_sha256 === expected.revisionSha256
    && current.timeline_id === expected.timelineId
    && current.timeline_content_sha256 === expected.timelineContentSha256
    && current.source_bindings_sha256 === expected.sourceBindingsSha256;
}

export class CanonicalExportCoordinator {
  private readonly apiBase: string;
  private readonly ensureBackendTrusted: (forceIdentityProof?: boolean) => Promise<void>;
  private readonly getMainAuthorityToken: () => string;
  private readonly presenter: NativeCanonicalExportPresenter;
  private readonly fetchImpl: FetchLike;
  private readonly createNonce: () => string;
  private reviewInProgress = false;

  constructor(options: CanonicalExportCoordinatorOptions) {
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
      throw new Error("Canonical export coordinator requires an exact IPv4 loopback API base.");
    }
    this.apiBase = parsed.origin;
    this.ensureBackendTrusted = options.ensureBackendTrusted;
    this.getMainAuthorityToken = options.getMainAuthorityToken;
    this.presenter = options.presenter;
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.createNonce = options.createNonce ?? randomUUID;
  }

  async approveAndExport(
    request: DesktopCanonicalExportApprovalRequest,
  ): Promise<DesktopCanonicalExportApprovalResult> {
    if (this.reviewInProgress) {
      return {
        status: "failed",
        message: "Another canonical export approval is already open.",
        job: null,
      };
    }
    this.reviewInProgress = true;
    try {
      return await this.runApproval(normalizeDesktopCanonicalExportApprovalRequest(request));
    } catch (error) {
      return {
        status: error instanceof CanonicalExportCoordinatorError
          && error.code === "canonical_export_outcome_unknown"
          ? "unknown"
          : "failed",
        message: safeFailureMessage(error),
        job: null,
      };
    } finally {
      this.reviewInProgress = false;
    }
  }

  private async runApproval(
    request: DesktopCanonicalExportApprovalRequest,
  ): Promise<DesktopCanonicalExportApprovalResult> {
    await this.ensureBackendTrusted(true);
    const envelope = normalizeCanonicalExportPresentationEnvelope(await this.mainRequest(
      `/v1/main/creative/projects/${encodeURIComponent(request.projectId)}/exports/presentation`,
      {},
    ));
    if (
      envelope.presentation.project_id !== request.projectId
      || canonicalExportPresentationSha256(envelope.presentation) !== envelope.presentation_sha256
    ) {
      throw new CanonicalExportCoordinatorError(
        "invalid_export_response",
        "The canonical export presentation did not match its exact project and digest.",
      );
    }
    if (!presentationMatchesCapturedIntent(request, envelope.presentation)) {
      throw new CanonicalExportCoordinatorError(
        "canonical_export_stale",
        "The captured database or exact Timeline changed. Refresh before approving export.",
      );
    }

    const selection = await this.presenter.chooseDestination(
      envelope.presentation,
      envelope.presentation_sha256,
      request.suggestedPackageName,
    );
    if (selection.cancelled) {
      return {
        status: "cancelled",
        message: "Canonical export approval was cancelled. No export job was created.",
        job: null,
      };
    }
    try {
      const canonicalPath = canonicalDestinationPath(selection.canonicalPath);
      const selectionIdentity = canonicalDestinationIdentity(
        selection.selectionIdentity,
      );
      const packageBasename = normalizeCanonicalExportPackageBasename(
        request.suggestedPackageName,
      );

      // The native dialog may remain open while the managed backend restarts.
      // Re-prove its identity, fetch the rotated credential only afterwards,
      // and keep the selected directory inode open until Core has compared it.
      await this.ensureBackendTrusted(true);
      const nativeGestureNonce = this.createNonce();
      const idempotencyKey = `canonical-export-${this.createNonce()}`;
      let rawResult: unknown;
      try {
        rawResult = await this.mainRequest(
          `/v1/main/creative/projects/${encodeURIComponent(request.projectId)}/exports`,
          {
            presentation: envelope.presentation,
            presentation_sha256: envelope.presentation_sha256,
            native_gesture_nonce: nativeGestureNonce,
            destination: {
              canonical_path: canonicalPath,
              package_basename: packageBasename,
              selection_identity: selectionIdentity,
            },
          },
          { "Idempotency-Key": idempotencyKey },
        );
      } catch (error) {
        if (isKnownCommandRejection(error)) throw error;
        throw outcomeUnknown(error);
      }
      let result;
      try {
        result = normalizeCanonicalExportCommandResult(rawResult);
      } catch (error) {
        throw outcomeUnknown(error);
      }
      const job = result.job;
      if (
        result.project_id !== request.projectId
        || job.presentation_sha256 !== envelope.presentation_sha256
        || job.timeline_binding.revision !== envelope.presentation.timeline_binding.revision
        || job.timeline_binding.revision_sha256
          !== envelope.presentation.timeline_binding.revision_sha256
        || job.timeline_binding.timeline_content_sha256
          !== envelope.presentation.timeline_binding.timeline_content_sha256
        || job.timeline_binding.source_bindings_sha256
          !== envelope.presentation.timeline_binding.source_bindings_sha256
        || job.output_binding.profile !== envelope.presentation.profile
        || job.output_binding.package_basename !== packageBasename
        || job.status !== "requested"
      ) {
        throw outcomeUnknown(new CanonicalExportCoordinatorError(
          "invalid_export_response",
          "The canonical export job did not match the exact approved presentation.",
        ));
      }
      return {
        status: "submitted",
        message: "The exact 1080p silent hard-cut export was approved and queued.",
        job: {
          jobId: job.id,
          status: job.status,
          packageBasename: job.output_binding.package_basename,
        },
      };
    } finally {
      await selection.release().catch(() => undefined);
    }
  }

  private async mainRequest(
    path: string,
    body: Record<string, unknown>,
    extraHeaders?: Readonly<Record<string, string>>,
  ): Promise<unknown> {
    if (!path.startsWith("/v1/main/") || path.includes("?") || path.includes("#")) {
      throw new CanonicalExportCoordinatorError(
        "invalid_export_request",
        "The canonical export endpoint is invalid.",
      );
    }
    if (
      extraHeaders
      && Object.keys(extraHeaders).some((name) => name.toLowerCase() !== "idempotency-key")
    ) {
      throw new CanonicalExportCoordinatorError(
        "invalid_export_request",
        "The canonical export request headers are invalid.",
      );
    }
    const controller = new AbortController();
    const timeoutError = new CanonicalExportCoordinatorError(
      "canonical_export_timeout",
      "The canonical export service did not respond in time.",
    );
    const timeout = setTimeout(() => controller.abort(timeoutError), CANONICAL_EXPORT_REQUEST_TIMEOUT_MS);
    try {
      const response = await this.fetchImpl(`${this.apiBase}${path}`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...extraHeaders,
          "X-MemoLens-Main-Authority": this.getMainAuthorityToken(),
        },
        body: JSON.stringify(body),
        redirect: "error",
        signal: controller.signal,
      });
      const payload = await boundedResponseJson(response, controller.signal);
      if (!response.ok) throw safeHttpFailure(response.status);
      return payload;
    } catch (error) {
      if (controller.signal.aborted) throw timeoutError;
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}
