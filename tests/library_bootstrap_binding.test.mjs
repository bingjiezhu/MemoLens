import assert from "node:assert/strict";
import {
  link,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  realpath,
  rm,
  stat,
  symlink,
  utimes,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  canonicalCandidateBindingJson,
  computeCandidateBindingSha256,
  LibraryBootstrapBindingStore,
} from "../electron-dist/electron/libraryBootstrapBinding.js";
import { resolveLibraryDbPathForState } from "../electron-dist/electron/desktopSettings.js";

const NOW = 1_800_000_000_000;
const REQUEST_ID = `lb_${"a".repeat(64)}`;

async function withIsolatedState(run) {
  const root = await mkdtemp(join(tmpdir(), "memolens-binding-store-"));
  const stateDir = join(root, "app-state");
  await mkdir(stateDir, { mode: 0o700 });
  try {
    await run({ root, stateDir });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

async function bindingFor(rootPath, stateDir) {
  const canonicalRoot = await realpath(rootPath);
  const identity = await stat(canonicalRoot, { bigint: true });
  return {
    canonicalRoot,
    dbPath: resolveLibraryDbPathForState(canonicalRoot, stateDir),
    device: identity.dev.toString(10),
    inode: identity.ino.toString(10),
  };
}

function sealInput(binding, overrides = {}) {
  return {
    request: {
      schema_version: "1",
      kind: "library_bootstrap_intent",
      request_id: REQUEST_ID,
      created_at_ms: NOW,
      expires_at_ms: NOW + 300_000,
    },
    nativeConfirmedAtMs: NOW + 1,
    binding,
    ...overrides,
  };
}

test("candidate binding digest matches the frozen Python Unicode vector", () => {
  const material = {
    canonical_root: "/tmp/素材 库",
    database_path: "/tmp/状态/MemoLens 数据.db",
    expected_library_root_device: "42",
    expected_library_root_inode: "9007199254740991",
  };
  assert.equal(
    canonicalCandidateBindingJson(material),
    "{\"canonical_root\":\"/tmp/素材 库\",\"database_path\":\"/tmp/状态/MemoLens 数据.db\",\"expected_library_root_device\":\"42\",\"expected_library_root_inode\":\"9007199254740991\"}",
  );
  assert.equal(
    computeCandidateBindingSha256(material),
    "7ae5b68db2ac7109d00072bd9585d7e4d00f3fbdb87f944313dcec144348145f",
  );
});

test("sealed binding is durable, closed, private, and exact replay is reused", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const libraryRoot = join(root, "素材 库");
    await mkdir(libraryRoot);
    const binding = await bindingFor(libraryRoot, stateDir);
    const store = new LibraryBootstrapBindingStore({ stateDir });
    const first = await store.seal(sealInput(binding));
    assert.equal(first.replayed, false);
    assert.equal(first.envelope.request_id, REQUEST_ID);
    assert.equal(first.envelope.canonical_root, binding.canonicalRoot);
    assert.equal(first.envelope.database_path, binding.dbPath);

    const bindingDir = join(stateDir, "library-bootstrap-bindings");
    const bindingPath = join(bindingDir, `${REQUEST_ID}.binding.json`);
    const [directoryIdentity, fileIdentity, raw] = await Promise.all([
      stat(bindingDir, { bigint: true }),
      stat(bindingPath, { bigint: true }),
      readFile(bindingPath, "utf8"),
    ]);
    assert.equal(Number(directoryIdentity.mode) & 0o777, 0o700);
    assert.equal(Number(fileIdentity.mode) & 0o777, 0o600);
    assert.equal(fileIdentity.nlink, 1n);
    assert.deepEqual(Object.keys(JSON.parse(raw)).sort(), [
      "candidate_binding_sha256",
      "canonical_root",
      "database_path",
      "expected_library_root_device",
      "expected_library_root_inode",
      "intent_created_at_ms",
      "intent_expires_at_ms",
      "kind",
      "native_confirmed_at_ms",
      "request_id",
      "schema_version",
    ]);

    const replay = await new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(binding));
    assert.equal(replay.replayed, true);
    assert.deepEqual({ ...replay.envelope }, first.envelope);
    assert.equal(await readFile(bindingPath, "utf8"), raw);
  });
});

test("request ID is a permanent no-replace locator across process restart", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const firstRoot = join(root, "first-library");
    const secondRoot = join(root, "second-library");
    await Promise.all([mkdir(firstRoot), mkdir(secondRoot)]);
    const firstBinding = await bindingFor(firstRoot, stateDir);
    const secondBinding = await bindingFor(secondRoot, stateDir);
    const bindingPath = join(
      stateDir,
      "library-bootstrap-bindings",
      `${REQUEST_ID}.binding.json`,
    );
    await new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(firstBinding));
    const frozenBytes = await readFile(bindingPath, "utf8");

    await assert.rejects(
      new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(secondBinding)),
      /conflicting permanent bootstrap binding/,
    );
    await assert.rejects(
      new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(firstBinding, {
        nativeConfirmedAtMs: NOW + 2,
      })),
      /conflicting permanent bootstrap binding/,
    );
    assert.equal(await readFile(bindingPath, "utf8"), frozenBytes);
    const loaded = await new LibraryBootstrapBindingStore({ stateDir }).load(REQUEST_ID);
    assert.equal(loaded?.canonical_root, firstBinding.canonicalRoot);
    assert.equal(loaded?.database_path, firstBinding.dbPath);
  });
});

test("restart removes safe pre-publication and post-link crash temporaries", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const libraryRoot = join(root, "chosen-library");
    await mkdir(libraryRoot);
    const binding = await bindingFor(libraryRoot, stateDir);
    const bindingDir = join(stateDir, "library-bootstrap-bindings");
    const bindingPath = join(bindingDir, `${REQUEST_ID}.binding.json`);
    const temporaryPath = join(bindingDir, `.${REQUEST_ID}.binding.json.tmp`);
    await mkdir(bindingDir, { mode: 0o700 });

    await writeFile(temporaryPath, "partial\n", { mode: 0o600, flag: "wx" });
    await utimes(temporaryPath, 0, 0);
    assert.equal(await new LibraryBootstrapBindingStore({ stateDir }).load(REQUEST_ID), null);
    await assert.rejects(lstat(temporaryPath), { code: "ENOENT" });

    await new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(binding));
    await link(bindingPath, temporaryPath);
    assert.equal((await stat(bindingPath, { bigint: true })).nlink, 2n);
    const loaded = await new LibraryBootstrapBindingStore({ stateDir }).load(REQUEST_ID);
    assert.equal(loaded?.candidate_binding_sha256.length, 64);
    await assert.rejects(lstat(temporaryPath), { code: "ENOENT" });
    assert.equal((await stat(bindingPath, { bigint: true })).nlink, 1n);
  });
});

test("binding reader fails closed on unsafe temporary and decoded duplicate keys", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const bindingDir = join(stateDir, "library-bootstrap-bindings");
    const temporaryPath = join(bindingDir, `.${REQUEST_ID}.binding.json.tmp`);
    await mkdir(bindingDir, { mode: 0o700 });
    await symlink(join(root, "missing"), temporaryPath);
    await assert.rejects(
      new LibraryBootstrapBindingStore({ stateDir }).load(REQUEST_ID),
      /unsafe bootstrap binding temporary/,
    );
    await rm(temporaryPath);

    const libraryRoot = join(root, "chosen-library");
    await mkdir(libraryRoot);
    const binding = await bindingFor(libraryRoot, stateDir);
    await new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(binding));
    const bindingPath = join(bindingDir, `${REQUEST_ID}.binding.json`);
    const raw = await readFile(bindingPath, "utf8");
    const duplicate = raw.replace(
      "{\"candidate_binding_sha256\"",
      "{\"schema\\u005fversion\":\"1\",\"candidate_binding_sha256\"",
    );
    await writeFile(bindingPath, duplicate, { encoding: "utf8", mode: 0o600 });
    await assert.rejects(
      new LibraryBootstrapBindingStore({ stateDir }).load(REQUEST_ID),
      /duplicate decoded binding member/,
    );
  });
});

test("binding store reports a live pre-publication writer as busy", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const bindingDir = join(stateDir, "library-bootstrap-bindings");
    const temporaryPath = join(bindingDir, `.${REQUEST_ID}.binding.json.tmp`);
    const libraryRoot = join(root, "chosen-library");
    await mkdir(libraryRoot);
    const binding = await bindingFor(libraryRoot, stateDir);
    await mkdir(bindingDir, { mode: 0o700 });
    await writeFile(temporaryPath, "in progress\n", { mode: 0o600, flag: "wx" });
    await assert.rejects(
      new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(binding)),
      (error) => error?.name === "LibraryBootstrapBindingBusyError",
    );
    assert.equal((await lstat(temporaryPath)).isFile(), true);
  });
});

test("binding reader detects same-inode mutation after its bounded read", async () => {
  await withIsolatedState(async ({ root, stateDir }) => {
    const libraryRoot = join(root, "chosen-library");
    await mkdir(libraryRoot);
    const binding = await bindingFor(libraryRoot, stateDir);
    const bindingPath = join(
      stateDir,
      "library-bootstrap-bindings",
      `${REQUEST_ID}.binding.json`,
    );
    await new LibraryBootstrapBindingStore({ stateDir }).seal(sealInput(binding));
    let injected = false;
    const reader = new LibraryBootstrapBindingStore({
      stateDir,
      async afterRead() {
        if (injected) return;
        injected = true;
        const raw = await readFile(bindingPath, "utf8");
        const replacement = raw.replace(
          /"candidate_binding_sha256":"[0-9a-f]/,
          '"candidate_binding_sha256":"0',
        );
        assert.equal(Buffer.byteLength(replacement), Buffer.byteLength(raw));
        await writeFile(bindingPath, replacement, { encoding: "utf8", mode: 0o600 });
      },
    });
    await assert.rejects(reader.load(REQUEST_ID), /changed while it was read/);
  });
});
