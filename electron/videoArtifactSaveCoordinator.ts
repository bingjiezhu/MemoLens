import { randomUUID } from "node:crypto";
import { constants, createWriteStream } from "node:fs";
import { link, lstat, open, realpath, unlink } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, normalize, resolve } from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

import {
  ArtifactIntegrityTracker,
  normalizeSha256Etag,
  parseArtifactIntegrityProof,
} from "./artifactIntegrity.js";
import type {
  DesktopArtifactSaveRequest,
  DesktopArtifactSaveResult,
} from "../src/video/types.js";

export const VIDEO_ARTIFACT_MAXIMUM_BYTES = 100 * 1024 * 1024 * 1024;
export const VIDEO_ARTIFACT_SAVE_TIMEOUT_MS = 30 * 60 * 1000;

export type NativeVideoArtifactDestination =
  | { cancelled: true }
  | { cancelled: false; filePath: string };

export interface VideoArtifactSaveCoordinatorOptions {
  backendUrl: string;
  ensureBackendTrusted(): Promise<void>;
  getSessionToken(): string;
  chooseDestination(suggestedFilename: string): Promise<NativeVideoArtifactDestination>;
  fetchImpl?: typeof fetch;
  maximumBytes?: number;
  timeoutMs?: number;
  createTemporaryId?: () => string;
}

interface DestinationGrant {
  destinationPath: string;
  presentedFilename: string;
  parentPath: string;
  device: string;
  inode: string;
  release(): Promise<void>;
}

class DestinationExistsError extends Error {}

function safeDirectoryOpenFlags(): number {
  return constants.O_RDONLY
    | (constants.O_DIRECTORY ?? 0)
    | (constants.O_NOFOLLOW ?? 0);
}

export function sanitizeVideoExportFilename(value: unknown): string {
  const cleaned = String(value ?? "")
    .normalize("NFKC")
    .replace(/[\u200B-\u200F\u202A-\u202E\u2060-\u206F]/g, "")
    .replace(/[\\/\u0000-\u001f\u007f<>:"|?*]+/g, "-")
    .replace(/^\.+/, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 116);
  const stem = cleaned.replace(/\.mp4$/i, "").replace(/\.[^.]+$/, "").trim();
  return `${stem || "memolens-export"}.mp4`;
}

function sanitizePresentedFilename(value: string): string {
  const cleaned = value
    .normalize("NFKC")
    .replace(/[\u200B-\u200F\u202A-\u202E\u2060-\u206F]/g, "")
    .replace(/[\\/\u0000-\u001f\u007f<>:"|?*]+/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 180);
  return cleaned || "memolens-export.mp4";
}

export function isTrustedRenderArtifactUrl(rawUrl: unknown, backendUrl: string): boolean {
  if (typeof rawUrl !== "string" || rawUrl.length === 0 || rawUrl.length > 2048) return false;
  try {
    const artifactUrl = new URL(rawUrl);
    const backend = new URL(backendUrl);
    return artifactUrl.origin === backend.origin
      && artifactUrl.username === ""
      && artifactUrl.password === ""
      && artifactUrl.search === ""
      && artifactUrl.hash === ""
      && artifactUrl.toString() === rawUrl
      && /^\/v1\/renders\/[A-Za-z0-9_-]+\/download$/.test(artifactUrl.pathname);
  } catch {
    return false;
  }
}

function renderJobIdFromArtifactUrl(rawUrl: string): string | null {
  try {
    const match = new URL(rawUrl).pathname.match(/^\/v1\/renders\/([A-Za-z0-9_-]+)\/download$/);
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

function normalizeRequest(
  rawRequest: unknown,
  backendUrl: string,
  maximumBytes: number,
): DesktopArtifactSaveRequest & { expectedSha256: string; expectedSizeBytes: number } {
  if (
    rawRequest === null
    || typeof rawRequest !== "object"
    || Array.isArray(rawRequest)
  ) {
    throw new Error("invalid_artifact_request");
  }
  const record = rawRequest as Record<string, unknown>;
  if (
    Object.keys(record).sort().join(",")
      !== "artifactUrl,expectedSha256,expectedSizeBytes,renderJobId,suggestedFilename"
    || !isTrustedRenderArtifactUrl(record.artifactUrl, backendUrl)
    || typeof record.renderJobId !== "string"
    || !/^[A-Za-z0-9_-]+$/.test(record.renderJobId)
    || record.renderJobId.length > 128
    || renderJobIdFromArtifactUrl(record.artifactUrl as string) !== record.renderJobId
    || typeof record.suggestedFilename !== "string"
    || record.suggestedFilename.length > 512
  ) {
    throw new Error("invalid_artifact_request");
  }
  const proof = parseArtifactIntegrityProof(
    record.expectedSha256,
    record.expectedSizeBytes,
    maximumBytes,
  );
  if (proof === null) throw new Error("invalid_artifact_request");
  return {
    renderJobId: record.renderJobId as string,
    artifactUrl: record.artifactUrl as string,
    suggestedFilename: record.suggestedFilename,
    expectedSha256: proof.sha256,
    expectedSizeBytes: proof.sizeBytes,
  };
}

async function pathEntryType(filePath: string): Promise<"absent" | "symlink" | "existing"> {
  try {
    const entry = await lstat(filePath);
    return entry.isSymbolicLink() ? "symlink" : "existing";
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === "ENOENT") return "absent";
    throw error;
  }
}

function normalizeDestinationPath(rawPath: unknown): string {
  if (
    typeof rawPath !== "string"
    || rawPath.length === 0
    || rawPath.length > 4096
    || rawPath.includes("\u0000")
    || !isAbsolute(rawPath)
  ) {
    throw new Error("unsafe_destination");
  }
  const canonicalInput = normalize(resolve(rawPath)).normalize("NFC");
  if (canonicalInput !== rawPath) throw new Error("unsafe_destination");
  return canonicalInput.toLowerCase().endsWith(".mp4")
    ? canonicalInput
    : `${canonicalInput}.mp4`;
}

async function acquireDestinationGrant(rawPath: unknown): Promise<DestinationGrant> {
  const destinationPath = normalizeDestinationPath(rawPath);
  const parentPath = dirname(destinationPath);
  const canonicalParent = normalize(await realpath(parentPath)).normalize("NFC");
  if (canonicalParent !== parentPath) throw new Error("unsafe_destination");
  const handle = await open(parentPath, safeDirectoryOpenFlags());
  try {
    const identity = await handle.stat({ bigint: true });
    if (!identity.isDirectory()) throw new Error("unsafe_destination");
    return {
      destinationPath,
      presentedFilename: sanitizePresentedFilename(basename(destinationPath)),
      parentPath,
      device: identity.dev.toString(10),
      inode: identity.ino.toString(10),
      release: async () => handle.close(),
    };
  } catch (error) {
    await handle.close().catch(() => undefined);
    throw error;
  }
}

async function assertDestinationGrantCurrent(grant: DestinationGrant): Promise<void> {
  const canonicalParent = normalize(await realpath(grant.parentPath)).normalize("NFC");
  if (canonicalParent !== grant.parentPath) throw new Error("destination_changed");
  const handle = await open(grant.parentPath, safeDirectoryOpenFlags());
  try {
    const identity = await handle.stat({ bigint: true });
    if (
      !identity.isDirectory()
      || identity.dev.toString(10) !== grant.device
      || identity.ino.toString(10) !== grant.inode
    ) {
      throw new Error("destination_changed");
    }
  } finally {
    await handle.close();
  }
}

function strictContentLength(response: Response): number | null {
  const value = response.headers.get("content-length");
  if (value === null || !/^(?:0|[1-9][0-9]*)$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function failed(message: string): DesktopArtifactSaveResult {
  return { status: "failed", filename: null, message };
}

export class VideoArtifactSaveCoordinator {
  private readonly maximumBytes: number;
  private readonly timeoutMs: number;
  private readonly createTemporaryId: () => string;

  constructor(private readonly options: VideoArtifactSaveCoordinatorOptions) {
    this.maximumBytes = options.maximumBytes ?? VIDEO_ARTIFACT_MAXIMUM_BYTES;
    this.timeoutMs = options.timeoutMs ?? VIDEO_ARTIFACT_SAVE_TIMEOUT_MS;
    this.createTemporaryId = options.createTemporaryId ?? randomUUID;
    if (!Number.isSafeInteger(this.maximumBytes) || this.maximumBytes <= 0) {
      throw new Error("MemoLens video artifact byte limit must be a positive integer.");
    }
    if (!Number.isSafeInteger(this.timeoutMs) || this.timeoutMs <= 0) {
      throw new Error("MemoLens video artifact timeout must be a positive integer.");
    }
  }

  async save(rawRequest: unknown): Promise<DesktopArtifactSaveResult> {
    let request: ReturnType<typeof normalizeRequest>;
    try {
      request = normalizeRequest(rawRequest, this.options.backendUrl, this.maximumBytes);
    } catch {
      return failed("MemoLens rejected an incomplete or untrusted render artifact proof.");
    }

    const selection = await this.options.chooseDestination(
      sanitizeVideoExportFilename(request.suggestedFilename),
    );
    if (selection.cancelled) {
      return { status: "cancelled", filename: null, message: "Video save was cancelled." };
    }

    let grant: DestinationGrant;
    try {
      grant = await acquireDestinationGrant(selection.filePath);
    } catch {
      return failed("MemoLens rejected an unsafe video save destination.");
    }

    let temporaryPath: string | null = null;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const initialEntryType = await pathEntryType(grant.destinationPath);
      if (initialEntryType === "existing") {
        return {
          status: "exists",
          filename: grant.presentedFilename,
          message: "That file already exists. Choose a new filename; MemoLens never overwrites by default.",
        };
      }
      if (initialEntryType === "symlink") {
        return failed("MemoLens rejected a symbolic-link video destination.");
      }

      await this.options.ensureBackendTrusted();
      await assertDestinationGrantCurrent(grant);
      const trustedEntryType = await pathEntryType(grant.destinationPath);
      if (trustedEntryType === "existing") throw new DestinationExistsError();
      if (trustedEntryType === "symlink") {
        throw new Error("unsafe_destination_symlink");
      }

      const temporaryId = this.createTemporaryId();
      if (!/^[A-Za-z0-9_-]{16,128}$/.test(temporaryId)) {
        throw new Error("unsafe_temporary_id");
      }
      temporaryPath = join(
        grant.parentPath,
        `.${basename(grant.destinationPath)}.memolens-${temporaryId}.part`,
      );

      const fetchImpl = this.options.fetchImpl ?? fetch;
      const response = await fetchImpl(request.artifactUrl, {
        headers: { "X-MemoLens-Desktop-Token": this.options.getSessionToken() },
        redirect: "error",
        signal: controller.signal,
      });
      if (response.url !== request.artifactUrl || !response.ok || response.body === null) {
        throw new Error(`download_status_${response.status}`);
      }
      const declaredSize = strictContentLength(response);
      if (declaredSize === null || declaredSize !== request.expectedSizeBytes) {
        throw new Error("integrity_size");
      }
      if (normalizeSha256Etag(response.headers.get("etag")) !== request.expectedSha256) {
        throw new Error("integrity_etag");
      }

      const integrityTracker = new ArtifactIntegrityTracker(
        {
          sha256: request.expectedSha256,
          sizeBytes: request.expectedSizeBytes,
        },
        this.maximumBytes,
      );
      const byteLimit = new Transform({
        transform(chunk: Buffer, _encoding, callback) {
          try {
            integrityTracker.update(chunk);
          } catch (error) {
            controller.abort();
            callback(error instanceof Error ? error : new Error("integrity_stream"));
            return;
          }
          callback(null, chunk);
        },
      });

      await pipeline(
        Readable.fromWeb(response.body as Parameters<typeof Readable.fromWeb>[0]),
        byteLimit,
        createWriteStream(temporaryPath, { flags: "wx", mode: 0o600 }),
      );
      if (!integrityTracker.verify()) throw new Error("integrity_bytes");

      const finalEntryType = await pathEntryType(grant.destinationPath);
      if (finalEntryType !== "absent") throw new DestinationExistsError();
      await assertDestinationGrantCurrent(grant);
      await link(temporaryPath, grant.destinationPath);
      await unlink(temporaryPath);
      temporaryPath = null;
      return {
        status: "saved",
        filename: grant.presentedFilename,
        message: "Video saved without changing any source media.",
      };
    } catch (error) {
      if (temporaryPath !== null) await unlink(temporaryPath).catch(() => undefined);
      const code = (error as NodeJS.ErrnoException)?.code;
      if (error instanceof DestinationExistsError || code === "EEXIST") {
        return {
          status: "exists",
          filename: grant.presentedFilename,
          message: "That file already exists. Choose a new filename; MemoLens never overwrites by default.",
        };
      }
      const reason = error instanceof Error ? error.message : "";
      if (reason.includes("100 GB desktop safety limit")) {
        return failed("Render artifact exceeds the 100 GB desktop safety limit.");
      }
      if (reason.startsWith("integrity_")) {
        return failed("Video integrity verification failed; no destination file was published.");
      }
      if (controller.signal.aborted) {
        return failed("Video save timed out before the artifact finished downloading.");
      }
      if (/^download_status_[0-9]+$/.test(reason)) {
        return failed(`Render download failed with status ${reason.slice("download_status_".length)}.`);
      }
      return failed("Video could not be saved. Choose a new filename and try again.");
    } finally {
      clearTimeout(timeout);
      await grant.release().catch(() => undefined);
    }
  }
}
