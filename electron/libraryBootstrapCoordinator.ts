import type { DesktopBackendStatus } from "../src/query/types.js";
import type { LibraryBootstrapCommitResult } from "./backendManager.js";
import type {
  CandidateLibraryBootstrapBindingEnvelope,
  LibraryBootstrapBindingStore,
} from "./libraryBootstrapBinding.js";
import type { ClaimedBootstrapRequest } from "./libraryBootstrapBroker.js";
import type {
  ApprovedDesktopLibraryBinding,
  LibrarySelectionAuthority,
  VerifiedDesktopLibraryBinding,
} from "./librarySelectionAuthority.js";

export interface LibraryBootstrapCoordinatorOptions {
  bindingStore: Pick<LibraryBootstrapBindingStore, "seal" | "load">;
  authority: Pick<LibrarySelectionAuthority, "verifyActiveBinding">;
  now?: () => number;
  ensureCandidate(
    envelope: CandidateLibraryBootstrapBindingEnvelope,
  ): Promise<DesktopBackendStatus>;
  commitCore(
    envelope: CandidateLibraryBootstrapBindingEnvelope,
  ): Promise<LibraryBootstrapCommitResult>;
  verifyActive(
    envelope: CandidateLibraryBootstrapBindingEnvelope,
  ): Promise<void>;
  commitProjection(binding: ApprovedDesktopLibraryBinding): Promise<unknown>;
}

function bindingFromEnvelope(
  envelope: CandidateLibraryBootstrapBindingEnvelope,
): ApprovedDesktopLibraryBinding {
  return {
    canonicalRoot: envelope.canonical_root,
    dbPath: envelope.database_path,
    device: envelope.expected_library_root_device,
    inode: envelope.expected_library_root_inode,
  };
}

function assertEnvelopeMatchesClaim(
  envelope: CandidateLibraryBootstrapBindingEnvelope,
  claimed: ClaimedBootstrapRequest,
): void {
  if (
    envelope.request_id !== claimed.request.request_id
    || envelope.intent_created_at_ms !== claimed.request.created_at_ms
    || envelope.intent_expires_at_ms !== claimed.request.expires_at_ms
  ) {
    throw new Error("MemoLens rejected a sealed binding for a different bootstrap intent.");
  }
}

/**
 * V20 Library bootstrap transaction.
 *
 * The sealed request-to-binding locator is the restart truth. Desktop settings
 * are written only after Core commit and exact active-runtime proof, so they
 * remain a repairable projection rather than bootstrap authority.
 */
export class LibraryBootstrapCoordinator {
  private readonly now: () => number;

  constructor(private readonly options: LibraryBootstrapCoordinatorOptions) {
    this.now = options.now ?? Date.now;
  }

  async resume(
    claimed: ClaimedBootstrapRequest,
  ): Promise<"library_authority_committed" | null> {
    const envelope = await this.options.bindingStore.load(claimed.request.request_id);
    if (envelope === null) return null;
    assertEnvelopeMatchesClaim(envelope, claimed);
    const verified = await this.options.authority.verifyActiveBinding(
      bindingFromEnvelope(envelope),
    );
    try {
      return await this.complete(envelope, verified);
    } finally {
      await verified.release();
    }
  }

  async commit(
    verified: VerifiedDesktopLibraryBinding,
    claimed: ClaimedBootstrapRequest,
  ): Promise<"library_authority_committed"> {
    const nativeConfirmedAtMs = this.now();
    if (
      nativeConfirmedAtMs < claimed.request.created_at_ms
      || nativeConfirmedAtMs >= claimed.request.expires_at_ms
    ) {
      throw new Error("MemoLens Library bootstrap intent expired before native confirmation.");
    }
    await verified.verifyCurrentIdentity();
    const sealed = await this.options.bindingStore.seal({
      request: claimed.request,
      nativeConfirmedAtMs,
      binding: verified,
    });
    assertEnvelopeMatchesClaim(sealed.envelope, claimed);
    return this.complete(sealed.envelope, verified);
  }

  private async complete(
    envelope: CandidateLibraryBootstrapBindingEnvelope,
    verified: VerifiedDesktopLibraryBinding,
  ): Promise<"library_authority_committed"> {
    await verified.verifyCurrentIdentity();
    const candidate = await this.options.ensureCandidate(envelope);
    if (candidate.state !== "connected" && candidate.state !== "started") {
      throw new Error("MemoLens could not prove the exact Library bootstrap candidate runtime.");
    }

    // Revalidate after process startup, immediately before the permanent Core write.
    await verified.verifyCurrentIdentity();
    const committed = await this.options.commitCore(envelope);
    if (
      committed.response.request_id !== envelope.request_id
      || committed.response.status !== "library_authority_committed"
    ) {
      throw new Error("MemoLens rejected a mismatched Library bootstrap Core result.");
    }
    await this.options.verifyActive(envelope);

    // The active runtime pins and proves the exact Core database. This final
    // native identity check prevents a renamed/replaced path becoming the UI
    // projection after commit.
    await verified.verifyCurrentIdentity();
    await this.options.commitProjection(bindingFromEnvelope(envelope));
    return "library_authority_committed";
  }
}
