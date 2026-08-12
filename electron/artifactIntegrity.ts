import { createHash } from "node:crypto";

export interface ArtifactIntegrityProof {
  sha256: string;
  sizeBytes: number;
}

export function parseArtifactIntegrityProof(
  sha256: unknown,
  sizeBytes: unknown,
  maximumBytes: number,
): ArtifactIntegrityProof | null {
  const normalizedSha256 = String(sha256 ?? "").toLowerCase();
  const normalizedSize = Number(sizeBytes);
  if (
    !/^[a-f0-9]{64}$/.test(normalizedSha256)
    || !Number.isSafeInteger(normalizedSize)
    || normalizedSize <= 0
    || normalizedSize > maximumBytes
  ) {
    return null;
  }
  return { sha256: normalizedSha256, sizeBytes: normalizedSize };
}

export function normalizeSha256Etag(value: string | null): string {
  return String(value ?? "")
    .replace(/^W\//i, "")
    .replace(/^\"|\"$/g, "")
    .toLowerCase();
}

export class ArtifactIntegrityTracker {
  readonly #expected: ArtifactIntegrityProof;
  readonly #maximumBytes: number;
  readonly #hasher = createHash("sha256");
  #receivedBytes = 0;
  #finalized = false;

  constructor(expected: ArtifactIntegrityProof, maximumBytes: number) {
    this.#expected = expected;
    this.#maximumBytes = maximumBytes;
  }

  update(chunk: Buffer): void {
    if (this.#finalized) {
      throw new Error("Artifact integrity tracker is already finalized.");
    }
    this.#receivedBytes += chunk.length;
    if (this.#receivedBytes > this.#maximumBytes) {
      throw new Error("Render artifact exceeds the 100 GB desktop safety limit.");
    }
    this.#hasher.update(chunk);
  }

  verify(): boolean {
    if (this.#finalized) {
      return false;
    }
    this.#finalized = true;
    return this.#receivedBytes === this.#expected.sizeBytes
      && this.#hasher.digest("hex") === this.#expected.sha256;
  }
}
