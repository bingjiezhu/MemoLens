import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  DesktopIndexingCoordinator,
  collectImageFiles,
  requestImageBatch,
} from "../electron-dist/electron/indexingCoordinator.js";
import { LibrarySelectionAuthority } from "../electron-dist/electron/librarySelectionAuthority.js";

const OPERATION_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_OPERATION_ID = "22222222-2222-4222-8222-222222222222";

class FakeProgressSender {
  destroyed = false;
  events = [];

  isDestroyed() {
    return this.destroyed;
  }

  send(channel, progress) {
    this.events.push({ channel, progress: structuredClone(progress) });
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) {
      return;
    }
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error(message);
}

function createCoordinator({
  files = ["/library/a.jpg"],
  folderPath = "/library",
  dbPath = "/library/.memo/index.db",
  collect = async () => files,
  analyze = async ({ filePaths }) => ({
    indexed: filePaths.length,
    skipped: 0,
    failed: 0,
    errors: [],
  }),
  batchSize,
  createOperationId = () => OPERATION_ID,
} = {}) {
  return new DesktopIndexingCoordinator({
    apiBase: "http://127.0.0.1:5519",
    getSessionToken: () => "desktop-token",
    resolveApprovedSelection: async () => ({
      folderPath,
      dbPath,
      rootDevice: "1",
      rootInode: "2",
      verifyCurrentIdentity: async () => undefined,
    }),
    collectImageFiles: collect,
    analyzeImageBatch: analyze,
    batchSize,
    createOperationId,
  });
}

test("recursively collects only supported images in stable path order", async () => {
  const rootPath = await mkdtemp(join(tmpdir(), "memolens-index-files-"));
  try {
    await mkdir(join(rootPath, "z", "nested"), { recursive: true });
    await mkdir(join(rootPath, "a"), { recursive: true });
    await Promise.all([
      writeFile(join(rootPath, "z", "nested", "last.HEIC"), "image"),
      writeFile(join(rootPath, "a", "first.jpeg"), "image"),
      writeFile(join(rootPath, "middle.webp"), "image"),
      writeFile(join(rootPath, "ignored.mp4"), "video"),
      writeFile(join(rootPath, "ignored.txt"), "text"),
    ]);

    assert.deepEqual(await collectImageFiles(rootPath), [
      join(rootPath, "a", "first.jpeg"),
      join(rootPath, "middle.webp"),
      join(rootPath, "z", "nested", "last.HEIC"),
    ]);
  } finally {
    await rm(rootPath, { recursive: true, force: true });
  }
});

test("first start on an empty library completes with deterministic progress", async () => {
  const sender = new FakeProgressSender();
  const coordinator = createCoordinator({
    files: [],
    folderPath: "/empty-library",
    dbPath: "/empty-library/.memo/index.db",
  });

  const result = await coordinator.start(sender, {});

  assert.deepEqual(result, {
    status: "empty",
    folderPath: "/empty-library",
    dbPath: "/empty-library/.memo/index.db",
    total: 0,
    indexed: 0,
    skipped: 0,
    failed: 0,
    errors: [],
  });
  assert.deepEqual(sender.events.map(({ progress }) => progress.phase), ["running", "completed"]);
  assert.equal(sender.events[0].progress.percent, 100);
  assert.equal(sender.events.at(-1).progress.percent, 100);
});

test("holds the start lock while the recursive scan is still in progress", async () => {
  const scan = deferred();
  const coordinator = createCoordinator({ collect: () => scan.promise });
  const firstStart = coordinator.start(new FakeProgressSender(), {});

  await assert.rejects(
    coordinator.start(new FakeProgressSender(), {}),
    /already starting/,
  );

  scan.resolve([]);
  await firstStart;
});

test("HTTP batch requests preserve db_path and token and fail omitted files", async () => {
  const requests = [];
  const result = await requestImageBatch(
    {
      apiBase: "http://127.0.0.1:5519/",
      filePaths: ["/library/a.jpg", "/library/sub/b.jpg", "/library/c.jpg"],
      rootPath: "/library",
      rootDevice: "41",
      rootInode: "73",
      model: "gpt-test",
      dbPath: "/state/photo-index.db",
      reindex: true,
    },
    {
      getSessionToken: () => "secret-token",
      fetch: async (url, init) => {
        requests.push({ url, init });
        return new Response(JSON.stringify({
          data: [{ relative_path: "a.jpg" }],
          errors: [{ relative_path: "sub/b.jpg", message: "broken" }],
        }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    },
  );

  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "http://127.0.0.1:5519/v1/indexing/jobs");
  assert.equal(requests[0].init.headers["X-MemoLens-Desktop-Token"], "secret-token");
  const payload = JSON.parse(requests[0].init.body);
  assert.equal(payload.db_path, "/state/photo-index.db");
  assert.equal(payload.model, "gpt-test");
  assert.equal(payload.reindex, true);
  assert.deepEqual(payload.library_root_identity, { device: "41", inode: "73" });
  assert.deepEqual(payload.input.files, ["a.jpg", "sub/b.jpg", "c.jpg"]);
  assert.deepEqual(result, {
    indexed: 1,
    skipped: 0,
    failed: 2,
    errors: [
      "sub/b.jpg: broken",
      "c.jpg: backend did not return a result for this file",
    ],
  });
});

test("rejects renderer folderPath/dbPath before resolving authority, scanning, or fetching", async () => {
  let authorityReads = 0;
  let directoryReads = 0;
  let fetches = 0;
  const coordinator = new DesktopIndexingCoordinator({
    apiBase: "http://127.0.0.1:5519",
    getSessionToken: () => "desktop-token",
    resolveApprovedSelection: async () => {
      authorityReads += 1;
      return {
        folderPath: "/approved",
        dbPath: "/approved.db",
        rootDevice: "1",
        rootInode: "2",
        verifyCurrentIdentity: async () => undefined,
      };
    },
    collectImageFiles: async () => {
      directoryReads += 1;
      return [];
    },
    analyzeImageBatch: async () => {
      fetches += 1;
      return { indexed: 0, skipped: 0, failed: 0, errors: [] };
    },
  });

  for (const forgedOptions of [
    { folderPath: "/forged", dbPath: "/forged.db" },
    { rootDevice: "1", rootInode: "2" },
    { library_root_identity: { device: "1", inode: "2" } },
  ]) {
    await assert.rejects(
      coordinator.start(new FakeProgressSender(), forgedOptions),
      /renderer-controlled indexing paths/,
    );
  }
  assert.deepEqual({ authorityReads, directoryReads, fetches }, {
    authorityReads: 0,
    directoryReads: 0,
    fetches: 0,
  });
});

test("rejects a changed active library identity before directory scan or backend fetch", async () => {
  let directoryReads = 0;
  let fetches = 0;
  const coordinator = new DesktopIndexingCoordinator({
    apiBase: "http://127.0.0.1:5519",
    getSessionToken: () => "desktop-token",
    resolveApprovedSelection: async () => {
      throw new Error("MemoLens rejected a changed desktop library identity.");
    },
    collectImageFiles: async () => {
      directoryReads += 1;
      return ["/changed/a.jpg"];
    },
    analyzeImageBatch: async () => {
      fetches += 1;
      return { indexed: 1, skipped: 0, failed: 0, errors: [] };
    },
  });

  await assert.rejects(
    coordinator.start(new FakeProgressSender(), {}),
    /changed desktop library identity/,
  );
  assert.deepEqual({ directoryReads, fetches }, { directoryReads: 0, fetches: 0 });
});

test("root replacement after native verify has zero scan fetch progress or write effects", async () => {
  const container = await mkdtemp(join(tmpdir(), "memolens-index-root-race-"));
  const libraryPath = join(container, "photos");
  await mkdir(libraryPath);
  const authority = new LibrarySelectionAuthority();
  let verified;
  try {
    const selection = await authority.issueSelection(libraryPath);
    verified = await authority.redeemSelection(selection.selectionTicket);
    await rename(verified.canonicalRoot, `${verified.canonicalRoot}-original`);
    await mkdir(verified.canonicalRoot);

    let scans = 0;
    let fetches = 0;
    let writes = 0;
    const sender = new FakeProgressSender();
    const coordinator = new DesktopIndexingCoordinator({
      apiBase: "http://127.0.0.1:5519",
      getSessionToken: () => "desktop-token",
      resolveApprovedSelection: async () => ({
        folderPath: verified.canonicalRoot,
        dbPath: verified.dbPath,
        rootDevice: verified.device,
        rootInode: verified.inode,
        verifyCurrentIdentity: verified.verifyCurrentIdentity,
        release: verified.release,
      }),
      collectImageFiles: async () => {
        scans += 1;
        return [join(verified.canonicalRoot, "forged.jpg")];
      },
      analyzeImageBatch: async () => {
        fetches += 1;
        writes += 1;
        return { indexed: 1, skipped: 0, failed: 0, errors: [] };
      },
    });

    await assert.rejects(
      coordinator.start(sender, {}),
      /changed desktop library identity/,
    );
    assert.deepEqual({ scans, fetches, writes, progress: sender.events.length }, {
      scans: 0,
      fetches: 0,
      writes: 0,
      progress: 0,
    });
  } finally {
    await verified?.release().catch(() => undefined);
    await rm(container, { recursive: true, force: true });
  }
});

test("holds and releases the main-owned active library grant for the full indexing run", async () => {
  let releases = 0;
  let releasedWhileAnalyzing = false;
  const coordinator = new DesktopIndexingCoordinator({
    apiBase: "http://127.0.0.1:5519",
    getSessionToken: () => "desktop-token",
    resolveApprovedSelection: async () => ({
      folderPath: "/approved",
      dbPath: "/approved.db",
      rootDevice: "1",
      rootInode: "2",
      verifyCurrentIdentity: async () => undefined,
      release: async () => {
        releases += 1;
      },
    }),
    collectImageFiles: async () => ["/approved/a.jpg"],
    analyzeImageBatch: async () => {
      releasedWhileAnalyzing = releases > 0;
      return { indexed: 1, skipped: 0, failed: 0, errors: [] };
    },
  });

  const result = await coordinator.start(new FakeProgressSender(), {});
  assert.equal(result.status, "completed");
  assert.equal(releasedWhileAnalyzing, false);
  assert.equal(releases, 1);
});

test("uses six-file batches and counts a thrown batch as failed without stopping", async () => {
  const files = Array.from({ length: 7 }, (_, index) => `/library/${index}.jpg`);
  const batchSizes = [];
  const coordinator = createCoordinator({
    files,
    dbPath: "/custom/index.db",
    analyze: async ({ filePaths }) => {
      batchSizes.push(filePaths.length);
      if (filePaths.length === 6) {
        throw new Error("backend unavailable");
      }
      return { indexed: 1, skipped: 0, failed: 0, errors: [] };
    },
  });

  const result = await coordinator.start(new FakeProgressSender(), {});

  assert.deepEqual(batchSizes, [6, 1]);
  assert.equal(result.status, "partial");
  assert.equal(result.indexed, 1);
  assert.equal(result.failed, 6);
  assert.deepEqual(result.errors, ["0.jpg ... 5.jpg: backend unavailable"]);
  assert.equal(result.dbPath, "/custom/index.db");
});

test("normalizes an injected non-positive batch size to one", async () => {
  const batchSizes = [];
  const coordinator = createCoordinator({
    files: ["/library/a.jpg", "/library/b.jpg"],
    batchSize: 0,
    analyze: async ({ filePaths }) => {
      batchSizes.push(filePaths.length);
      return { indexed: filePaths.length, skipped: 0, failed: 0, errors: [] };
    },
  });

  const result = await coordinator.start(new FakeProgressSender(), {});

  assert.deepEqual(batchSizes, [1, 1]);
  assert.equal(result.status, "completed");
});

test("pause takes effect only between batches and resume continues the same job", async () => {
  const sender = new FakeProgressSender();
  const firstBatch = deferred();
  const batches = [];
  const coordinator = createCoordinator({
    files: Array.from({ length: 7 }, (_, index) => `/library/${index}.jpg`),
    analyze: async ({ filePaths }) => {
      batches.push([...filePaths]);
      if (batches.length === 1) {
        await firstBatch.promise;
      }
      return { indexed: filePaths.length, skipped: 0, failed: 0, errors: [] };
    },
  });
  const running = coordinator.start(sender, {});

  await waitFor(() => batches.length === 1, "first batch did not start");
  await assert.rejects(
    coordinator.start(new FakeProgressSender(), {}),
    /already running/,
  );
  assert.equal(coordinator.pause(OPERATION_ID), true);
  assert.equal(sender.events.at(-1).progress.phase, "pausing");
  assert.equal(batches.length, 1, "pause must not interrupt the active batch");

  firstBatch.resolve();
  await waitFor(
    () => sender.events.some(({ progress }) => progress.phase === "paused"),
    "job did not pause at the batch boundary",
  );
  assert.equal(batches.length, 1);

  assert.equal(coordinator.resume(OPERATION_ID), true);
  const result = await running;
  assert.equal(result.status, "completed");
  assert.equal(result.indexed, 7);
  assert.deepEqual(batches.map((batch) => batch.length), [6, 1]);
  assert.equal(sender.events.at(-1).progress.phase, "completed");
});

test("pause and resume reject wrong or expired indexing operation before mutation", async () => {
  const [mainSource, preloadSource] = await Promise.all([
    readFile(new URL("../electron/main.ts", import.meta.url), "utf-8"),
    readFile(new URL("../electron/preload.cts", import.meta.url), "utf-8"),
  ]);
  assert.match(
    mainSource,
    /"memolens:pause-indexing", async \(event, operationId: string\): Promise<boolean> => \{[\s\S]*indexingCoordinator\.pause\(operationId\)/,
  );
  assert.match(
    mainSource,
    /"memolens:resume-indexing", async \(event, operationId: string\): Promise<boolean> => \{[\s\S]*indexingCoordinator\.resume\(operationId\)/,
  );
  assert.match(
    preloadSource,
    /pauseIndexing\(operationId: string\): Promise<boolean> \{\s*return invokeProductionIpc<boolean>\("memolens:pause-indexing", operationId\);/,
  );
  assert.match(
    preloadSource,
    /resumeIndexing\(operationId: string\): Promise<boolean> \{\s*return invokeProductionIpc<boolean>\("memolens:resume-indexing", operationId\);/,
  );

  const sender = new FakeProgressSender();
  const firstBatch = deferred();
  let batchCalls = 0;
  const coordinator = createCoordinator({
    files: Array.from({ length: 7 }, (_, index) => `/library/${index}.jpg`),
    analyze: async ({ filePaths }) => {
      batchCalls += 1;
      if (batchCalls === 1) await firstBatch.promise;
      return { indexed: filePaths.length, skipped: 0, failed: 0, errors: [] };
    },
  });
  const running = coordinator.start(sender, {});

  await waitFor(() => batchCalls === 1, "first scoped indexing batch did not start");
  assert.equal(sender.events[0].progress.operationId, OPERATION_ID);
  const beforeWrongPause = structuredClone(sender.events);
  assert.equal(coordinator.pause(OTHER_OPERATION_ID), false);
  assert.equal(coordinator.pause("renderer-forged-job"), false);
  assert.deepEqual(sender.events, beforeWrongPause);

  assert.equal(coordinator.pause(OPERATION_ID), true);
  firstBatch.resolve();
  await waitFor(
    () => sender.events.some(({ progress }) => progress.phase === "paused"),
    "scoped operation did not pause",
  );
  const beforeWrongResume = structuredClone(sender.events);
  assert.equal(coordinator.resume(OTHER_OPERATION_ID), false);
  assert.equal(coordinator.resume(null), false);
  assert.deepEqual(sender.events, beforeWrongResume);

  assert.equal(coordinator.resume(OPERATION_ID), true);
  await running;
  const completedEvents = structuredClone(sender.events);
  assert.equal(coordinator.pause(OPERATION_ID), false);
  assert.equal(coordinator.resume(OPERATION_ID), false);
  assert.deepEqual(sender.events, completedEvents);
});

test("does not send progress after the renderer is destroyed", async () => {
  const sender = new FakeProgressSender();
  sender.destroyed = true;
  const result = await createCoordinator().start(sender, {});

  assert.equal(result.status, "completed");
  assert.deepEqual(sender.events, []);
});

test("always clears start and active-job state after scan failure and completion", async () => {
  let scanAttempts = 0;
  const coordinator = createCoordinator({
    collect: async () => {
      scanAttempts += 1;
      if (scanAttempts === 1) {
        throw new Error("cannot scan library");
      }
      return [];
    },
  });

  await assert.rejects(
    coordinator.start(new FakeProgressSender(), {}),
    /cannot scan library/,
  );
  assert.equal(coordinator.pause(OPERATION_ID), false);
  assert.equal(coordinator.resume(OPERATION_ID), false);

  const result = await coordinator.start(new FakeProgressSender(), {});
  assert.equal(result.status, "empty");
  assert.equal(coordinator.pause(OPERATION_ID), false);
  assert.equal(coordinator.resume(OPERATION_ID), false);

  const brokenSender = new FakeProgressSender();
  brokenSender.send = (_channel, progress) => {
    if (progress.phase === "completed") {
      throw new Error("renderer send failed");
    }
  };
  await assert.rejects(
    coordinator.start(brokenSender, {}),
    /renderer send failed/,
  );
  assert.equal(
    (await coordinator.start(new FakeProgressSender(), {})).status,
    "empty",
  );
});
