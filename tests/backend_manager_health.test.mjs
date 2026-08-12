import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import test from "node:test";

import {
  DESKTOP_SESSION_TOKEN,
  verifyBackendHealthPayload,
} from "../electron-dist/electron/backendManager.js";
import {
  loadDesktopSettings,
  resolveLibraryDbPath,
  saveDesktopSettings,
} from "../electron-dist/electron/desktopSettings.js";

const challenge = "ab".repeat(32);
const identity = {
  status: "ok",
  service: "memolens-backend",
  api_version: "1",
};

test("rejects arbitrary 2xx and spoofed MemoLens identity", () => {
  assert.equal(verifyBackendHealthPayload({ status: "ok" }, challenge), false);
  assert.equal(
    verifyBackendHealthPayload({ ...identity, challenge_proof: "00".repeat(32) }, challenge),
    false,
  );
});

test("accepts only a valid session-token challenge proof", () => {
  const challengeProof = createHmac("sha256", DESKTOP_SESSION_TOKEN)
    .update(challenge)
    .digest("hex");
  assert.equal(
    verifyBackendHealthPayload({ ...identity, challenge_proof: challengeProof }, challenge),
    true,
  );
  assert.equal(
    verifyBackendHealthPayload({ ...identity, challenge_proof: `${challengeProof}junk` }, challenge),
    false,
  );
});

test("uses an opaque stable app-state database and persists the selected library", async () => {
  const appStateDir = await mkdtemp(join(tmpdir(), "memolens-desktop-state-"));
  const previousAppStateDir = process.env.MEMOLENS_APP_STATE_DIR;
  process.env.MEMOLENS_APP_STATE_DIR = appStateDir;

  try {
    const libraryPath = join(appStateDir, "outside", "photos");
    const equivalentPath = join(libraryPath, "child", "..");
    const dbPath = resolveLibraryDbPath(libraryPath);
    assert.equal(dbPath, resolveLibraryDbPath(equivalentPath));
    assert.equal(dirname(dbPath), join(appStateDir, "storage"));
    assert.match(basename(dbPath), /^photo-index-[0-9a-f]{24}\.db$/);
    assert.equal(basename(dbPath).includes("photos"), false);

    await saveDesktopSettings(process.cwd(), {
      pythonCommand: "python3",
      autoStartBackend: true,
      defaultLibraryDir: libraryPath,
      defaultDbPath: dbPath,
    });
    const restored = await loadDesktopSettings(process.cwd());
    assert.equal(restored.defaultLibraryDir, libraryPath);
    assert.equal(restored.defaultDbPath, dbPath);
  } finally {
    if (previousAppStateDir === undefined) {
      delete process.env.MEMOLENS_APP_STATE_DIR;
    } else {
      process.env.MEMOLENS_APP_STATE_DIR = previousAppStateDir;
    }
    await rm(appStateDir, { recursive: true, force: true });
  }
});
