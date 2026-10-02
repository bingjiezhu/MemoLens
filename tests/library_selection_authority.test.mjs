import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, readdir, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  commitDesktopLibrarySelection,
  loadApprovedDesktopLibraryBinding,
  loadDesktopSettings,
  saveDesktopSettings,
} from "../electron-dist/electron/desktopSettings.js";
import {
  LIBRARY_SELECTION_TICKET_TTL_MS,
  LibrarySelectionAuthority,
  NativeLibrarySelectionCoordinator,
} from "../electron-dist/electron/librarySelectionAuthority.js";

async function withIsolatedDesktopState(run) {
  const appStateDir = await mkdtemp(join(tmpdir(), "memolens-library-authority-"));
  const prior = process.env.MEMOLENS_APP_STATE_DIR;
  process.env.MEMOLENS_APP_STATE_DIR = appStateDir;
  try {
    await run(appStateDir);
  } finally {
    if (prior === undefined) delete process.env.MEMOLENS_APP_STATE_DIR;
    else process.env.MEMOLENS_APP_STATE_DIR = prior;
    await rm(appStateDir, { recursive: true, force: true });
  }
}

test("preload and main expose only an opaque selection ticket as commit authority", async () => {
  const [mainSource, preloadSource] = await Promise.all([
    readFile(new URL("../electron/main.ts", import.meta.url), "utf-8"),
    readFile(new URL("../electron/preload.cts", import.meta.url), "utf-8"),
  ]);
  assert.match(
    mainSource,
    /"memolens:commit-library-selection",\s*async \(event, selectionTicket: string\)/,
  );
  assert.match(
    mainSource,
    /redeemSelection\(selectionTicket\)/,
  );
  assert.match(
    preloadSource,
    /commitLibrarySelection\(selectionTicket: string\): Promise<DesktopSettings>/,
  );
  assert.doesNotMatch(
    preloadSource,
    /commitLibrarySelection\(selection: DesktopFolderSelection\)/,
  );
});

test("native folder cancellation and renderer path injection issue no forged selection authority", async () => {
  const [mainSource, preloadSource] = await Promise.all([
    readFile(new URL("../electron/main.ts", import.meta.url), "utf-8"),
    readFile(new URL("../electron/preload.cts", import.meta.url), "utf-8"),
  ]);
  assert.match(mainSource, /new NativeLibrarySelectionCoordinator\(\{/);
  assert.match(mainSource, /async chooseFolder\(\) \{[\s\S]*dialog\.showOpenDialog\(\{/);
  assert.match(
    mainSource,
    /"memolens:pick-image-folder", async \(event\) => \{[\s\S]*return nativeLibrarySelectionCoordinator\.pick\(\);/,
  );
  assert.match(
    preloadSource,
    /pickImageFolder\(\): Promise<DesktopFolderSelection \| null> \{\s*return invokeProductionIpc<DesktopFolderSelection \| null>\("memolens:pick-image-folder"\);/,
  );

  await withIsolatedDesktopState(async (appStateDir) => {
    const approvedPath = join(appStateDir, "approved-photos");
    const forgedPath = join(appStateDir, "renderer-forged-photos");
    await Promise.all([mkdir(approvedPath), mkdir(forgedPath)]);
    const authority = new LibrarySelectionAuthority();
    let nativeChoice = null;
    let nativeChoices = 0;
    const coordinator = new NativeLibrarySelectionCoordinator({
      authority,
      async chooseFolder() {
        nativeChoices += 1;
        return nativeChoice;
      },
    });

    assert.equal(await coordinator.pick(forgedPath), null);
    assert.equal(nativeChoices, 1);
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);

    nativeChoice = approvedPath;
    const selection = await coordinator.pick(forgedPath);
    assert.ok(selection);
    assert.equal(selection.folderPath, await realpath(approvedPath));
    assert.notEqual(selection.folderPath, await realpath(forgedPath));
    const verified = await authority.redeemSelection(selection.selectionTicket);
    await verified.release();
    await assert.rejects(
      authority.redeemSelection(selection.selectionTicket),
      /invalid or already-used library selection ticket/,
    );
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
  });
});

test("native folder selection rejects non-directory identity before issuing a ticket", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    const selectedFile = join(appStateDir, "not-a-library.txt");
    await writeFile(selectedFile, "not a directory");
    const authority = new LibrarySelectionAuthority();
    const coordinator = new NativeLibrarySelectionCoordinator({
      authority,
      async chooseFolder() {
        return selectedFile;
      },
    });

    await assert.rejects(
      coordinator.pick(),
      /not a directory/,
    );
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
    assert.equal((await loadDesktopSettings(process.cwd())).libraryConfigured, false);
  });
});

test("desktop settings store rejects renderer path or root injection before filesystem mutation", async () => {
  const [mainSource, preloadSource] = await Promise.all([
    readFile(new URL("../electron/main.ts", import.meta.url), "utf-8"),
    readFile(new URL("../electron/preload.cts", import.meta.url), "utf-8"),
  ]);
  assert.match(
    mainSource,
    /"memolens:get-settings", async \(event\): Promise<DesktopSettings>/,
  );
  assert.match(
    preloadSource,
    /getSettings\(\): Promise<DesktopSettings> \{\s*return invokeProductionIpc<DesktopSettings>\("memolens:get-settings"\);/,
  );

  await withIsolatedDesktopState(async (appStateDir) => {
    const forgedPath = join(appStateDir, "renderer-owned-settings.json");
    for (const forgedUpdate of [
      { autoStartBackend: false, settingsPath: forgedPath },
      { autoStartBackend: false, libraryRoot: join(appStateDir, "forged-library") },
      { autoStartBackend: false, pythonCommand: "/tmp/renderer-python" },
    ]) {
      await assert.rejects(
        saveDesktopSettings(process.cwd(), forgedUpdate),
        /renderer-controlled library or runtime settings/,
      );
    }
    assert.deepEqual(await readdir(appStateDir), []);

    await saveDesktopSettings(process.cwd(), { autoStartBackend: false });
    assert.deepEqual(await readdir(appStateDir), ["desktop-settings.json"]);
    assert.equal((await loadDesktopSettings(process.cwd())).autoStartBackend, false);
  });
});

test("desktop settings publication fsyncs file before rename and parent after rename", async () => {
  const source = await readFile(new URL("../electron/desktopSettings.ts", import.meta.url), "utf-8");
  const start = source.indexOf("async function writePersistedSettings");
  const end = source.indexOf("\nfunction mutatePersistedSettings", start);
  assert.ok(start >= 0 && end > start);
  const writer = source.slice(start, end);
  const fileSyncIndex = writer.indexOf("await temporaryHandle.sync()");
  const renameIndex = writer.indexOf("await rename(temporaryPath, settingsPath)");
  const directorySyncIndex = writer.indexOf("await directoryHandle.sync()");
  assert.match(writer, /constants\.O_EXCL[\s\S]*constants\.O_NOFOLLOW/);
  assert.ok(fileSyncIndex >= 0 && fileSyncIndex < renameIndex);
  assert.ok(renameIndex < directorySyncIndex);

  await withIsolatedDesktopState(async (appStateDir) => {
    await saveDesktopSettings(process.cwd(), { autoStartBackend: false });
    const settingsPath = join(appStateDir, "desktop-settings.json");
    const details = await stat(settingsPath);
    assert.equal(details.mode & 0o777, 0o600);
    assert.deepEqual(await readdir(appStateDir), ["desktop-settings.json"]);
  });
});

test("native library selection ticket commits once and binds persisted device/inode identity", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    const libraryPath = join(appStateDir, "photos");
    await mkdir(libraryPath);
    const authority = new LibrarySelectionAuthority();
    const selection = await authority.issueSelection(libraryPath);

    const verified = await authority.redeemSelection(selection.selectionTicket);
    try {
      const committed = await commitDesktopLibrarySelection(process.cwd(), verified);
      assert.equal(committed.libraryConfigured, true);
      assert.equal(committed.defaultLibraryDir, selection.folderPath);
      assert.equal(committed.defaultDbPath, selection.dbPath);
    } finally {
      await verified.release();
    }

    const persisted = await loadApprovedDesktopLibraryBinding();
    assert.deepEqual(persisted, {
      canonicalRoot: selection.folderPath,
      dbPath: selection.dbPath,
      device: verified.device,
      inode: verified.inode,
    });
    await assert.rejects(
      authority.redeemSelection(selection.selectionTicket),
      /invalid or already-used library selection ticket/,
    );
    assert.equal((await loadDesktopSettings(process.cwd())).libraryConfigured, true);
  });
});

test("forged and expired library tickets fail before any desktop settings write", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    let now = 10_000;
    let ticketIndex = 0;
    const authority = new LibrarySelectionAuthority({
      now: () => now,
      createTicket: () => `ticket-${String(++ticketIndex).padStart(40, "0")}`,
    });
    const libraryPath = join(appStateDir, "photos");
    await mkdir(libraryPath);

    await assert.rejects(
      authority.redeemSelection("forged-ticket-with-more-than-thirty-two-bytes"),
      /invalid or already-used library selection ticket/,
    );
    const selection = await authority.issueSelection(libraryPath);
    now += LIBRARY_SELECTION_TICKET_TTL_MS;
    await assert.rejects(
      authority.redeemSelection(selection.selectionTicket),
      /expired library selection ticket/,
    );
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
    assert.equal((await loadDesktopSettings(process.cwd())).libraryConfigured, false);
  });
});

test("root replacement invalidates a native ticket before settings are persisted", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    const libraryPath = join(appStateDir, "photos");
    await mkdir(libraryPath);
    const authority = new LibrarySelectionAuthority();
    const selection = await authority.issueSelection(libraryPath);

    await rename(libraryPath, `${libraryPath}-replaced`);
    await mkdir(libraryPath);
    await assert.rejects(
      authority.redeemSelection(selection.selectionTicket),
      /changed desktop library identity/,
    );
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
    assert.equal((await loadDesktopSettings(process.cwd())).libraryConfigured, false);
  });
});

test("tampered derived database binding fails before desktop settings mutation", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    const libraryPath = join(appStateDir, "photos");
    await mkdir(libraryPath);
    const authority = new LibrarySelectionAuthority();
    const selection = await authority.issueSelection(libraryPath);
    const verified = await authority.redeemSelection(selection.selectionTicket);
    try {
      await assert.rejects(
        commitDesktopLibrarySelection(process.cwd(), {
          ...verified,
          dbPath: join(appStateDir, "attacker-controlled.db"),
        }),
        /unverified desktop library binding/,
      );
    } finally {
      await verified.release();
    }
    assert.equal(await loadApprovedDesktopLibraryBinding(), null);
    assert.equal((await loadDesktopSettings(process.cwd())).libraryConfigured, false);
  });
});

test("concurrent low-impact settings writes cannot clobber an approved library binding", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    const libraryPath = join(appStateDir, "photos");
    await mkdir(libraryPath);
    const authority = new LibrarySelectionAuthority();
    const selection = await authority.issueSelection(libraryPath);
    const verified = await authority.redeemSelection(selection.selectionTicket);
    try {
      await Promise.all([
        commitDesktopLibrarySelection(process.cwd(), verified),
        saveDesktopSettings(process.cwd(), { autoStartBackend: false }),
      ]);
    } finally {
      await verified.release();
    }

    const settings = await loadDesktopSettings(process.cwd());
    assert.equal(settings.autoStartBackend, false);
    assert.equal(settings.libraryConfigured, true);
    assert.equal(settings.defaultLibraryDir, selection.folderPath);
    assert.equal(settings.defaultDbPath, selection.dbPath);
  });
});

test("root replacement invalidates the persisted active binding before reuse", async () => {
  await withIsolatedDesktopState(async (appStateDir) => {
    const libraryPath = join(appStateDir, "photos");
    await mkdir(libraryPath);
    const authority = new LibrarySelectionAuthority();
    const selection = await authority.issueSelection(libraryPath);
    const verified = await authority.redeemSelection(selection.selectionTicket);
    try {
      await commitDesktopLibrarySelection(process.cwd(), verified);
    } finally {
      await verified.release();
    }

    const persisted = await loadApprovedDesktopLibraryBinding();
    assert.ok(persisted);
    await rename(libraryPath, `${libraryPath}-replaced`);
    await mkdir(libraryPath);
    await assert.rejects(
      authority.verifyActiveBinding(persisted),
      /changed desktop library identity/,
    );
  });
});
