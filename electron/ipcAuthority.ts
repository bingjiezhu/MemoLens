import { fileURLToPath } from "node:url";

export interface TrustedIpcFrame {
  readonly url: string;
}

export interface TrustedIpcSender {
  readonly id: number;
  readonly mainFrame: TrustedIpcFrame;
}

export interface TrustedIpcInvokeEvent {
  readonly sender: TrustedIpcSender;
  readonly senderFrame: TrustedIpcFrame | null;
}

/**
 * Renderer authority is tied to the exact entry document registered when the
 * BrowserWindow was created. Hash-only changes remain within that document;
 * origin, path, query, file, sender, and frame drift do not.
 */
export function isTrustedRendererNavigation(targetUrl: string, entryUrl: string): boolean {
  try {
    const target = new URL(targetUrl);
    const entry = new URL(entryUrl);
    if (entry.protocol === "file:") {
      return target.protocol === "file:"
        && fileURLToPath(target) === fileURLToPath(entry)
        && target.search === entry.search;
    }
    return target.origin === entry.origin
      && target.pathname === entry.pathname
      && target.search === entry.search;
  } catch {
    return false;
  }
}

/**
 * First boundary for every renderer-to-main production handler.
 *
 * The registry is supplied by the BrowserWindow owner instead of being kept
 * in this module, which makes the decision pure and prevents hidden global
 * authority from leaking into tests or future windows.
 */
export function assertTrustedIpcSender(
  event: TrustedIpcInvokeEvent,
  trustedRendererEntries: ReadonlyMap<number, string>,
): void {
  const expectedEntryUrl = trustedRendererEntries.get(event.sender.id);
  if (
    expectedEntryUrl === undefined
    || event.senderFrame === null
    || event.senderFrame !== event.sender.mainFrame
    || !isTrustedRendererNavigation(event.senderFrame.url, expectedEntryUrl)
  ) {
    throw new Error("Rejected IPC call from an untrusted renderer frame.");
  }
}
