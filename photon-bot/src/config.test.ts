import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { loadConfig } from "./config.js";

test("managed SQLite discovery preserves explicit paths and refuses ambiguous libraries", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "memolens-photon-managed-db-"));
  const keys = [
    "BACKEND_BASE_URL", "BACKEND_SEND_PATH_OVERRIDES", "DISCORD_ALLOWED_USER_IDS",
    "DISCORD_BOT_TOKEN", "DISCORD_EXPECTED_BOT_USER_ID", "IMAGE_LIBRARY_DIR",
    "MEMOLENS_APP_STATE_DIR", "SQLITE_DB_PATH",
  ] as const;
  const previous = new Map(keys.map((key) => [key, process.env[key]]));
  const stateDir = path.join(tempRoot, "state");
  const library = path.join(tempRoot, "photos");
  const storage = path.join(stateDir, "storage");
  fs.mkdirSync(storage, { recursive: true });
  fs.mkdirSync(library);
  const desktopDb = path.join(tempRoot, "desktop.db");
  const backendDb = path.join(tempRoot, "backend.db");
  const explicitDb = path.join(tempRoot, "explicit.db");
  const hashedDb = path.join(storage, `photo-index-${"a".repeat(24)}.db`);
  const secondHashedDb = path.join(storage, `photo-index-${"b".repeat(24)}.db`);
  const legacyDb = path.join(storage, "photo_index.db");
  try {
    process.env.BACKEND_BASE_URL = "http://127.0.0.1:5519";
    process.env.BACKEND_SEND_PATH_OVERRIDES = "true";
    process.env.DISCORD_ALLOWED_USER_IDS = "fixture-user";
    process.env.DISCORD_BOT_TOKEN = "fixture-token";
    process.env.DISCORD_EXPECTED_BOT_USER_ID = "123456789012345678";
    process.env.IMAGE_LIBRARY_DIR = library;
    process.env.MEMOLENS_APP_STATE_DIR = stateDir;
    process.env.SQLITE_DB_PATH = "";
    for (const file of [desktopDb, backendDb, explicitDb, hashedDb]) fs.writeFileSync(file, "");
    fs.writeFileSync(path.join(stateDir, "desktop-settings.json"), JSON.stringify({ defaultDbPath: desktopDb }));
    fs.writeFileSync(path.join(stateDir, "backend-settings.json"), JSON.stringify({ db_path: backendDb }));
    assert.equal(loadConfig().dbPath, desktopDb);
    process.env.SQLITE_DB_PATH = explicitDb;
    assert.equal(loadConfig().dbPath, explicitDb);
    process.env.SQLITE_DB_PATH = path.join(tempRoot, "missing-explicit.db");
    assert.throws(() => loadConfig(), /SQLITE_DB_PATH does not exist/);
    delete process.env.SQLITE_DB_PATH;
    fs.writeFileSync(path.join(stateDir, "desktop-settings.json"), "malformed");
    assert.equal(loadConfig().dbPath, backendDb);
    fs.writeFileSync(path.join(stateDir, "backend-settings.json"), "null");
    assert.equal(loadConfig().dbPath, hashedDb);
    fs.writeFileSync(secondHashedDb, "");
    fs.writeFileSync(path.join(library, "photo_index.db"), "private-library-lookalike");
    assert.throws(() => loadConfig(), /SQLITE_DB_PATH must be set/);
    fs.writeFileSync(legacyDb, "");
    assert.equal(loadConfig().dbPath, legacyDb);
    process.env.BACKEND_SEND_PATH_OVERRIDES = "false";
    assert.equal(loadConfig().dbPath, null);
    process.env.DISCORD_ALLOWED_USER_IDS = "";
    assert.throws(() => loadConfig(), /empty user allowlist/);
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    fs.rmSync(tempRoot, { force: true, recursive: true });
  }
});

test("BACKEND_BASE_URL is a literal-loopback startup boundary in online mode", () => {
  const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "memolens-photon-config-"));
  const keys = [
    "BACKEND_BASE_URL",
    "BACKEND_SEND_PATH_OVERRIDES",
    "DISCORD_ALLOWED_USER_IDS",
    "DISCORD_BOT_TOKEN",
    "DISCORD_EXPECTED_BOT_USER_ID",
    "IMAGE_LIBRARY_DIR",
    "MEMOLENS_NETWORK_PROFILE",
  ] as const;
  const previous = new Map(keys.map((key) => [key, process.env[key]]));

  try {
    process.env.BACKEND_SEND_PATH_OVERRIDES = "false";
    process.env.DISCORD_ALLOWED_USER_IDS = "fixture-user";
    process.env.DISCORD_BOT_TOKEN = "fixture-token";
    process.env.DISCORD_EXPECTED_BOT_USER_ID = "123456789012345678";
    process.env.IMAGE_LIBRARY_DIR = tempRoot;
    process.env.MEMOLENS_NETWORK_PROFILE = "online";
    process.env.BACKEND_BASE_URL = "https://provider.example.invalid";

    assert.throws(
      () => loadConfig(),
      /BACKEND_BASE_URL must be an absolute HTTP\(S\) URL with a literal loopback host/,
    );

    process.env.BACKEND_BASE_URL = "http://127.0.0.1:5519/";
    assert.equal(loadConfig().backendBaseUrl, "http://127.0.0.1:5519");
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    fs.rmSync(tempRoot, { force: true, recursive: true });
  }
});
