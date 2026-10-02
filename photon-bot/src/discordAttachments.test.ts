import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import sharp from "sharp";

import {
  buildMessagePayload,
  prepareAttachmentsForDiscord,
  type DiscordImageEncoder,
} from "./discord.js";

async function createImageFixture(t: test.TestContext): Promise<{
  fixtureRoot: string;
  libraryRoot: string;
  sourcePath: string;
  tempRoot: string;
}> {
  const fixtureRoot = await fs.promises.mkdtemp(
    path.join(os.tmpdir(), "memolens-discord-attachment-test-"),
  );
  t.after(async () => {
    await fs.promises.rm(fixtureRoot, { force: true, recursive: true });
  });

  const libraryRoot = path.join(fixtureRoot, "library");
  const tempRoot = path.join(fixtureRoot, "temp");
  await fs.promises.mkdir(libraryRoot);
  await fs.promises.mkdir(tempRoot);
  const sourcePath = path.join(libraryRoot, "private-original.png");
  await sharp({
    create: {
      width: 32,
      height: 16,
      channels: 4,
      background: { r: 20, g: 120, b: 220, alpha: 0.5 },
    },
  })
    .png()
    .toFile(sourcePath);

  return { fixtureRoot, libraryRoot, sourcePath, tempRoot };
}

async function assertTempRootIsEmpty(tempRoot: string): Promise<void> {
  assert.deepEqual(await fs.promises.readdir(tempRoot), []);
}

test("Discord payload contains only a newly encoded JPEG Buffer and a generic name", async (t) => {
  const fixture = await createImageFixture(t);
  const originalBytes = await fs.promises.readFile(fixture.sourcePath);
  const result = await prepareAttachmentsForDiscord(
    fixture.libraryRoot,
    [fixture.sourcePath],
    16,
    { tempRoot: fixture.tempRoot },
  );

  assert.equal(result.rejectedCount, 0);
  assert.equal(result.attachments.length, 1);
  const prepared = result.attachments[0]!;
  assert.notDeepEqual(prepared.uploadBytes, originalBytes);
  assert.equal(prepared.uploadBytes[0], 0xff);
  assert.equal(prepared.uploadBytes[1], 0xd8);
  assert.equal(prepared.uploadName, "memolens-image-1.jpg");
  assert.equal(prepared.uploadName.includes(path.basename(fixture.sourcePath)), false);

  const metadata = await sharp(prepared.uploadBytes).metadata();
  assert.equal(metadata.format, "jpeg");
  assert.equal(metadata.width, 16);

  const payload = buildMessagePayload({ content: "safe", attachments: result.attachments });
  assert.equal(payload.files.length, 1);
  assert.equal(Buffer.isBuffer(payload.files[0]!.attachment), true);
  assert.notEqual(payload.files[0]!.attachment, fixture.sourcePath);
  assert.equal(payload.files[0]!.name, "memolens-image-1.jpg");
  await assertTempRootIsEmpty(fixture.tempRoot);
});

test("a symlink outside the library is rejected before any attachment is built", async (t) => {
  const fixture = await createImageFixture(t);
  const outsidePath = path.join(fixture.fixtureRoot, "outside-private.png");
  await fs.promises.copyFile(fixture.sourcePath, outsidePath);
  const linkPath = path.join(fixture.libraryRoot, "outside-link.png");
  await fs.promises.symlink(outsidePath, linkPath);

  const result = await prepareAttachmentsForDiscord(
    fixture.libraryRoot,
    [linkPath],
    16,
    { tempRoot: fixture.tempRoot },
  );
  const payload = buildMessagePayload({ content: "safe", attachments: result.attachments });

  assert.equal(result.rejectedCount, 1);
  assert.equal(result.attachments.length, 0);
  assert.deepEqual(payload.files, []);
  await assertTempRootIsEmpty(fixture.tempRoot);
});

test("a corrupt file disguised as an image fails closed without original bytes or path", async (t) => {
  const fixture = await createImageFixture(t);
  const corruptPath = path.join(fixture.libraryRoot, "disguised.jpg");
  const corruptBytes = Buffer.from("not actually an image\n", "utf8");
  await fs.promises.writeFile(corruptPath, corruptBytes);

  const result = await prepareAttachmentsForDiscord(
    fixture.libraryRoot,
    [corruptPath],
    16,
    { tempRoot: fixture.tempRoot },
  );
  const payload = buildMessagePayload({ content: "safe", attachments: result.attachments });

  assert.equal(result.rejectedCount, 1);
  assert.deepEqual(result.attachments, []);
  assert.deepEqual(payload.files, []);
  assert.equal(JSON.stringify(payload).includes(corruptPath), false);
  assert.equal(JSON.stringify(payload).includes(corruptBytes.toString("hex")), false);
  await assertTempRootIsEmpty(fixture.tempRoot);
});

test("an encoder failure never falls back to the original file", async (t) => {
  const fixture = await createImageFixture(t);
  const originalBytes = await fs.promises.readFile(fixture.sourcePath);
  const failingEncoder: DiscordImageEncoder = async (sourceBytes) => {
    assert.deepEqual(sourceBytes, originalBytes);
    throw new Error("simulated resize failure");
  };

  const result = await prepareAttachmentsForDiscord(
    fixture.libraryRoot,
    [fixture.sourcePath],
    16,
    { encoder: failingEncoder, tempRoot: fixture.tempRoot },
  );
  const payload = buildMessagePayload({ content: "safe", attachments: result.attachments });

  assert.equal(result.rejectedCount, 1);
  assert.deepEqual(result.attachments, []);
  assert.deepEqual(payload.files, []);
  await assertTempRootIsEmpty(fixture.tempRoot);
});

test("an encoder that copies or disguises the original is rejected as non-JPEG", async (t) => {
  const fixture = await createImageFixture(t);
  const copyingEncoder: DiscordImageEncoder = async (sourceBytes, outputPath) => {
    await fs.promises.writeFile(outputPath, sourceBytes);
  };

  const result = await prepareAttachmentsForDiscord(
    fixture.libraryRoot,
    [fixture.sourcePath],
    16,
    { encoder: copyingEncoder, tempRoot: fixture.tempRoot },
  );

  assert.equal(result.rejectedCount, 1);
  assert.deepEqual(result.attachments, []);
  await assertTempRootIsEmpty(fixture.tempRoot);
});

test("a re-encoded JPEG above the byte cap is omitted and its temp artifact is removed", async (t) => {
  const fixture = await createImageFixture(t);
  const result = await prepareAttachmentsForDiscord(
    fixture.libraryRoot,
    [fixture.sourcePath],
    16,
    { maxAttachmentBytes: 8, tempRoot: fixture.tempRoot },
  );
  const payload = buildMessagePayload({ content: "safe", attachments: result.attachments });

  assert.equal(result.rejectedCount, 1);
  assert.deepEqual(result.attachments, []);
  assert.deepEqual(payload.files, []);
  await assertTempRootIsEmpty(fixture.tempRoot);
});
