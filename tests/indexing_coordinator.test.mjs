import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  DesktopIndexingCoordinator,
  collectImageFiles,
  requestImageBatch,
} from "../electron-dist/electron/indexingCoordinator.js";

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
  collect = async () => files,
  analyze = async ({ filePaths }) => ({
    indexed: filePaths.length,
    skipped: 0,
    failed: 0,
    errors: [],
  }),
  batchSize,
} = {}) {
  return new DesktopIndexingCoordinator({
    apiBase: "http://127.0.0.1:5519",
    getSessionToken: () => "desktop-token",
    resolveDbPath: (folderPath) => `${folderPath}/.memo/index.db`,
    collectImageFiles: collect,
    analyzeImageBatch: analyze,
    batchSize,
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
  const coordinator = createCoordinator({ files: [] });

  const result = await coordinator.start(sender, { folderPath: "/empty-library" });

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
  const firstStart = coordinator.start(new FakeProgressSender(), { folderPath: "/library" });

  await assert.rejects(
    coordinator.start(new FakeProgressSender(), { folderPath: "/other" }),
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

test("uses six-file batches and counts a thrown batch as failed without stopping", async () => {
  const files = Array.from({ length: 7 }, (_, index) => `/library/${index}.jpg`);
  const batchSizes = [];
  const coordinator = createCoordinator({
    files,
    analyze: async ({ filePaths }) => {
      batchSizes.push(filePaths.length);
      if (filePaths.length === 6) {
        throw new Error("backend unavailable");
      }
      return { indexed: 1, skipped: 0, failed: 0, errors: [] };
    },
  });

  const result = await coordinator.start(new FakeProgressSender(), {
    folderPath: "/library",
    dbPath: "/custom/index.db",
  });

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

  const result = await coordinator.start(new FakeProgressSender(), {
    folderPath: "/library",
  });

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
  const running = coordinator.start(sender, { folderPath: "/library" });

  await waitFor(() => batches.length === 1, "first batch did not start");
  await assert.rejects(
    coordinator.start(new FakeProgressSender(), { folderPath: "/other-library" }),
    /already running/,
  );
  assert.equal(coordinator.pause(), true);
  assert.equal(sender.events.at(-1).progress.phase, "pausing");
  assert.equal(batches.length, 1, "pause must not interrupt the active batch");

  firstBatch.resolve();
  await waitFor(
    () => sender.events.some(({ progress }) => progress.phase === "paused"),
    "job did not pause at the batch boundary",
  );
  assert.equal(batches.length, 1);

  assert.equal(coordinator.resume(), true);
  const result = await running;
  assert.equal(result.status, "completed");
  assert.equal(result.indexed, 7);
  assert.deepEqual(batches.map((batch) => batch.length), [6, 1]);
  assert.equal(sender.events.at(-1).progress.phase, "completed");
});

test("does not send progress after the renderer is destroyed", async () => {
  const sender = new FakeProgressSender();
  sender.destroyed = true;
  const result = await createCoordinator().start(sender, { folderPath: "/library" });

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
    coordinator.start(new FakeProgressSender(), { folderPath: "/library" }),
    /cannot scan library/,
  );
  assert.equal(coordinator.pause(), false);
  assert.equal(coordinator.resume(), false);

  const result = await coordinator.start(new FakeProgressSender(), { folderPath: "/library" });
  assert.equal(result.status, "empty");
  assert.equal(coordinator.pause(), false);
  assert.equal(coordinator.resume(), false);

  const brokenSender = new FakeProgressSender();
  brokenSender.send = (_channel, progress) => {
    if (progress.phase === "completed") {
      throw new Error("renderer send failed");
    }
  };
  await assert.rejects(
    coordinator.start(brokenSender, { folderPath: "/library" }),
    /renderer send failed/,
  );
  assert.equal(
    (await coordinator.start(new FakeProgressSender(), { folderPath: "/library" })).status,
    "empty",
  );
});
