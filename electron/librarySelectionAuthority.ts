import { randomBytes } from "node:crypto";
import { constants } from "node:fs";
import { open, realpath } from "node:fs/promises";
import { normalize, resolve } from "node:path";

import { resolveLibraryDbPath } from "./desktopSettings.js";
import type { DesktopFolderSelection } from "../src/query/types.js";

export const LIBRARY_SELECTION_TICKET_TTL_MS = 2 * 60 * 1000;

export interface ApprovedDesktopLibraryBinding {
  canonicalRoot: string;
  dbPath: string;
  device: string;
  inode: string;
}

export interface VerifiedDesktopLibraryBinding extends ApprovedDesktopLibraryBinding {
  verifyCurrentIdentity(): Promise<void>;
  release(): Promise<void>;
}

interface PendingSelection extends ApprovedDesktopLibraryBinding {
  expiresAtMs: number;
}

export interface LibrarySelectionAuthorityOptions {
  now?: () => number;
  createTicket?: () => string;
  ticketTtlMs?: number;
}

export interface NativeLibrarySelectionCoordinatorOptions {
  authority: Pick<LibrarySelectionAuthority, "issueSelection">;
  chooseFolder(): Promise<string | null>;
}

/**
 * Bridges one real native folder choice to one opaque main-process ticket.
 *
 * The public method intentionally accepts no renderer path.  Cancellation
 * returns before ticket issuance, and all path authority comes from the
 * injected native chooser owned by Electron main.
 */
export class NativeLibrarySelectionCoordinator {
  constructor(private readonly options: NativeLibrarySelectionCoordinatorOptions) {}

  async pick(): Promise<DesktopFolderSelection | null> {
    const folderPath = await this.options.chooseFolder();
    if (folderPath === null) {
      return null;
    }
    return this.options.authority.issueSelection(folderPath);
  }
}

function normalizeCanonicalPath(filePath: string): string {
  const canonical = normalize(resolve(filePath)).normalize("NFC");
  return process.platform === "win32" ? canonical.toLowerCase() : canonical;
}

function safeDirectoryOpenFlags(): number {
  return constants.O_RDONLY
    | (constants.O_DIRECTORY ?? 0)
    | (constants.O_NOFOLLOW ?? 0);
}

function assertBindingShape(binding: ApprovedDesktopLibraryBinding): void {
  if (
    typeof binding?.canonicalRoot !== "string"
    || typeof binding?.dbPath !== "string"
    || typeof binding?.device !== "string"
    || typeof binding?.inode !== "string"
    || binding.device.length === 0
    || binding.inode.length === 0
  ) {
    throw new Error("MemoLens rejected an incomplete desktop library identity.");
  }
  const canonicalRoot = normalizeCanonicalPath(binding.canonicalRoot);
  if (canonicalRoot !== binding.canonicalRoot) {
    throw new Error("MemoLens rejected a non-canonical desktop library root.");
  }
  if (resolveLibraryDbPath(canonicalRoot) !== resolve(binding.dbPath)) {
    throw new Error("MemoLens rejected an unexpected desktop library database path.");
  }
}

async function inspectDirectory(filePath: string): Promise<VerifiedDesktopLibraryBinding> {
  const canonicalRoot = normalizeCanonicalPath(await realpath(resolve(filePath)));
  const handle = await open(canonicalRoot, safeDirectoryOpenFlags());
  try {
    const identity = await handle.stat({ bigint: true });
    if (!identity.isDirectory()) {
      throw new Error("MemoLens rejected a library selection that is not a directory.");
    }
    const device = identity.dev.toString(10);
    const inode = identity.ino.toString(10);
    let released = false;
    return {
      canonicalRoot,
      dbPath: resolveLibraryDbPath(canonicalRoot),
      device,
      inode,
      async verifyCurrentIdentity() {
        if (released) {
          throw new Error("MemoLens rejected a released desktop library grant.");
        }
        const heldIdentity = await handle.stat({ bigint: true });
        if (
          !heldIdentity.isDirectory()
          || heldIdentity.dev.toString(10) !== device
          || heldIdentity.ino.toString(10) !== inode
        ) {
          throw new Error("MemoLens rejected a changed desktop library identity.");
        }

        let currentCanonicalRoot: string;
        try {
          currentCanonicalRoot = normalizeCanonicalPath(await realpath(canonicalRoot));
        } catch {
          throw new Error("MemoLens rejected a changed desktop library identity.");
        }
        if (currentCanonicalRoot !== canonicalRoot) {
          throw new Error("MemoLens rejected a changed desktop library identity.");
        }
        const currentHandle = await open(canonicalRoot, safeDirectoryOpenFlags());
        try {
          const currentIdentity = await currentHandle.stat({ bigint: true });
          if (
            !currentIdentity.isDirectory()
            || currentIdentity.dev.toString(10) !== device
            || currentIdentity.ino.toString(10) !== inode
          ) {
            throw new Error("MemoLens rejected a changed desktop library identity.");
          }
        } finally {
          await currentHandle.close();
        }
      },
      async release() {
        if (!released) {
          released = true;
          await handle.close();
        }
      },
    };
  } catch (error) {
    await handle.close().catch(() => undefined);
    throw error;
  }
}

/**
 * Main-process-only authority for native folder selections.
 *
 * A renderer receives a display path and an opaque, short-lived ticket.  The
 * ticket is consumed before filesystem verification, cannot be replayed, and
 * never lets renderer-controlled paths become persisted or scanned authority.
 */
export class LibrarySelectionAuthority {
  private readonly pendingSelections = new Map<string, PendingSelection>();
  private readonly now: () => number;
  private readonly createTicket: () => string;
  private readonly ticketTtlMs: number;

  constructor(options: LibrarySelectionAuthorityOptions = {}) {
    this.now = options.now ?? Date.now;
    this.createTicket = options.createTicket ?? (() => randomBytes(32).toString("base64url"));
    this.ticketTtlMs = options.ticketTtlMs ?? LIBRARY_SELECTION_TICKET_TTL_MS;
    if (!Number.isSafeInteger(this.ticketTtlMs) || this.ticketTtlMs <= 0) {
      throw new Error("MemoLens library selection ticket TTL must be a positive integer.");
    }
  }

  async issueSelection(filePath: string): Promise<DesktopFolderSelection> {
    const verified = await inspectDirectory(filePath);
    try {
      this.pruneExpired();
      let selectionTicket = this.createTicket();
      while (this.pendingSelections.has(selectionTicket)) {
        selectionTicket = this.createTicket();
      }
      if (selectionTicket.length < 32) {
        throw new Error("MemoLens refused to issue a weak library selection ticket.");
      }
      this.pendingSelections.set(selectionTicket, {
        canonicalRoot: verified.canonicalRoot,
        dbPath: verified.dbPath,
        device: verified.device,
        inode: verified.inode,
        expiresAtMs: this.now() + this.ticketTtlMs,
      });
      return {
        folderPath: verified.canonicalRoot,
        dbPath: verified.dbPath,
        selectionTicket,
      };
    } finally {
      await verified.release();
    }
  }

  async redeemSelection(selectionTicket: unknown): Promise<VerifiedDesktopLibraryBinding> {
    if (typeof selectionTicket !== "string") {
      throw new Error("MemoLens rejected an invalid library selection ticket.");
    }
    const pending = this.pendingSelections.get(selectionTicket);
    if (pending === undefined) {
      throw new Error("MemoLens rejected an invalid or already-used library selection ticket.");
    }

    // Consume before any asynchronous verification so concurrent commits and
    // retries cannot use the same native user action twice.
    this.pendingSelections.delete(selectionTicket);
    if (pending.expiresAtMs <= this.now()) {
      throw new Error("MemoLens rejected an expired library selection ticket.");
    }
    return this.verifyActiveBinding(pending);
  }

  async verifyActiveBinding(
    binding: ApprovedDesktopLibraryBinding,
  ): Promise<VerifiedDesktopLibraryBinding> {
    assertBindingShape(binding);
    const verified = await inspectDirectory(binding.canonicalRoot);
    if (
      verified.canonicalRoot !== binding.canonicalRoot
      || verified.dbPath !== binding.dbPath
      || verified.device !== binding.device
      || verified.inode !== binding.inode
    ) {
      await verified.release();
      throw new Error("MemoLens rejected a changed desktop library identity.");
    }
    return verified;
  }

  private pruneExpired(): void {
    const currentTime = this.now();
    for (const [ticket, selection] of this.pendingSelections) {
      if (selection.expiresAtMs <= currentTime) {
        this.pendingSelections.delete(ticket);
      }
    }
  }
}
