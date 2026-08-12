import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  ArtifactIntegrityTracker,
  normalizeSha256Etag,
  parseArtifactIntegrityProof,
} from "../electron-dist/electron/artifactIntegrity.js";

test("accepts only a bounded SHA-256 and integer size proof", () => {
  const bytes = Buffer.from("verified MemoLens preview");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  assert.deepEqual(parseArtifactIntegrityProof(sha256.toUpperCase(), bytes.length, 100), {
    sha256,
    sizeBytes: bytes.length,
  });
  assert.equal(parseArtifactIntegrityProof("nope", bytes.length, 100), null);
  assert.equal(parseArtifactIntegrityProof(sha256, 0, 100), null);
  assert.equal(parseArtifactIntegrityProof(sha256, 101, 100), null);
});

test("normalizes strong and weak quoted SHA-256 ETags", () => {
  const sha256 = "a".repeat(64);
  assert.equal(normalizeSha256Etag(`"${sha256}"`), sha256);
  assert.equal(normalizeSha256Etag(`W/"${sha256.toUpperCase()}"`), sha256);
  assert.equal(normalizeSha256Etag(null), "");
});

test("verifies streamed bytes against exact size and digest", () => {
  const bytes = Buffer.from("verified MemoLens preview");
  const proof = {
    sha256: createHash("sha256").update(bytes).digest("hex"),
    sizeBytes: bytes.length,
  };
  const tracker = new ArtifactIntegrityTracker(proof, 100);
  tracker.update(bytes.subarray(0, 8));
  tracker.update(bytes.subarray(8));
  assert.equal(tracker.verify(), true);

  const tampered = new ArtifactIntegrityTracker(proof, 100);
  tampered.update(Buffer.from("tampered MemoLens preview"));
  assert.equal(tampered.verify(), false);
});

test("stops streaming at the hard byte limit", () => {
  const tracker = new ArtifactIntegrityTracker(
    { sha256: "a".repeat(64), sizeBytes: 4 },
    3,
  );
  assert.throws(() => tracker.update(Buffer.from("four")), /safety limit/);
});
