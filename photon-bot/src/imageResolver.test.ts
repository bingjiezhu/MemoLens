import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  resolveImageBatch,
  resolveImagePath,
  revalidateResolvedImagePath,
} from "./imageResolver.js";

async function createFilesystemFixture(t: test.TestContext): Promise<{
  fixtureRoot: string;
  libraryRoot: string;
  validPath: string;
}> {
  const fixtureRoot = await fs.promises.mkdtemp(
    path.join(os.tmpdir(), "memolens-image-resolver-test-"),
  );
  t.after(async () => {
    await fs.promises.rm(fixtureRoot, { force: true, recursive: true });
  });

  const libraryRoot = path.join(fixtureRoot, "library");
  const nestedDir = path.join(libraryRoot, "nested");
  await fs.promises.mkdir(nestedDir, { recursive: true });
  const validPath = path.join(nestedDir, "valid.jpg");
  await fs.promises.writeFile(validPath, "regular image placeholder");

  return { fixtureRoot, libraryRoot, validPath };
}

test("resolves only a symlink-free regular file below the real library root", async (t) => {
  const fixture = await createFilesystemFixture(t);
  const resolved = resolveImagePath(fixture.libraryRoot, "nested/valid.jpg");

  assert.equal(resolved, fs.realpathSync.native(fixture.validPath));
  assert.equal(
    revalidateResolvedImagePath(fixture.libraryRoot, resolved),
    fs.realpathSync.native(fixture.validPath),
  );
});

test("rejects absolute paths, traversal, the root itself, and non-regular targets", async (t) => {
  const fixture = await createFilesystemFixture(t);

  assert.throws(() => resolveImagePath(fixture.libraryRoot, fixture.validPath));
  assert.throws(() => resolveImagePath(fixture.libraryRoot, "../outside.jpg"));
  assert.throws(() =>
    resolveImagePath(fixture.libraryRoot, "nested/../nested/valid.jpg"),
  );
  assert.throws(() => resolveImagePath(fixture.libraryRoot, "."));
  assert.throws(() => resolveImagePath(fixture.libraryRoot, "nested"));
});

test("rejects a file symlink and a directory-component symlink, including links outside root", async (t) => {
  const fixture = await createFilesystemFixture(t);
  const outsideDir = path.join(fixture.fixtureRoot, "outside");
  const outsidePath = path.join(outsideDir, "private.jpg");
  await fs.promises.mkdir(outsideDir);
  await fs.promises.writeFile(outsidePath, "private bytes");

  await fs.promises.symlink(outsidePath, path.join(fixture.libraryRoot, "linked.jpg"));
  await fs.promises.symlink(outsideDir, path.join(fixture.libraryRoot, "linked-dir"));

  assert.throws(() => resolveImagePath(fixture.libraryRoot, "linked.jpg"), /symbolic link/);
  assert.throws(
    () => resolveImagePath(fixture.libraryRoot, "linked-dir/private.jpg"),
    /symbolic link/,
  );
});

test("rejects a configured library root that is itself a symlink", async (t) => {
  const fixture = await createFilesystemFixture(t);
  const linkedRoot = path.join(fixture.fixtureRoot, "library-link");
  await fs.promises.symlink(fixture.libraryRoot, linkedRoot);

  assert.throws(
    () => resolveImagePath(linkedRoot, "nested/valid.jpg"),
    /real directory, not a symlink/,
  );
});

test("batch resolution reports rejected paths without consuming past the safe-image limit", async (t) => {
  const fixture = await createFilesystemFixture(t);
  const secondPath = path.join(fixture.libraryRoot, "nested", "second.jpg");
  await fs.promises.writeFile(secondPath, "second regular image placeholder");

  const result = resolveImageBatch(
    fixture.libraryRoot,
    ["../outside.jpg", "nested/valid.jpg", "nested/second.jpg"],
    1,
  );

  assert.deepEqual(result.imagePaths, [fs.realpathSync.native(fixture.validPath)]);
  assert.deepEqual(result.missingRelativePaths, ["../outside.jpg"]);
  assert.equal(result.consumedCount, 2);
});
