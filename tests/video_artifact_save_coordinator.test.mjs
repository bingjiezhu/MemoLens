import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  stat,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  VideoArtifactSaveCoordinator,
} from "../electron-dist/electron/videoArtifactSaveCoordinator.js";

const ARTIFACT_URL = "http://127.0.0.1:5519/v1/renders/render-1/download";
const TEMPORARY_ID = "11111111-1111-4111-8111-111111111111";

function proofFor(bytes) {
  return {
    renderJobId: "render-1",
    artifactUrl: ARTIFACT_URL,
    suggestedFilename: "Grounded memory.mp4",
    expectedSha256: createHash("sha256").update(bytes).digest("hex"),
    expectedSizeBytes: bytes.length,
  };
}

function artifactResponse(bytes, {
  url = ARTIFACT_URL,
  status = 200,
  sha256 = createHash("sha256").update(bytes).digest("hex"),
  contentLength = String(bytes.length),
} = {}) {
  const response = new Response(bytes, {
    status,
    headers: {
      "Content-Length": contentLength,
      ETag: `"${sha256}"`,
    },
  });
  Object.defineProperty(response, "url", { value: url });
  return response;
}

function harness({
  selection = { cancelled: true },
  ensureBackendTrusted,
  fetchImpl,
  maximumBytes,
} = {}) {
  const state = {
    destinationChoices: [],
    trustChecks: 0,
    fetches: [],
  };
  const coordinator = new VideoArtifactSaveCoordinator({
    backendUrl: "http://127.0.0.1:5519",
    maximumBytes,
    createTemporaryId: () => TEMPORARY_ID,
    getSessionToken: () => "desktop-session-secret",
    chooseDestination: async (suggestedFilename) => {
      state.destinationChoices.push(suggestedFilename);
      return selection;
    },
    ensureBackendTrusted: async () => {
      state.trustChecks += 1;
      if (ensureBackendTrusted) await ensureBackendTrusted();
    },
    fetchImpl: async (url, init) => {
      state.fetches.push({ url: String(url), init });
      if (fetchImpl) return fetchImpl(url, init);
      throw new Error("unexpected fetch");
    },
  });
  return { coordinator, state };
}

async function withCanonicalTemporaryDirectory(run) {
  const rawRoot = await mkdtemp(join(tmpdir(), "memolens-video-save-"));
  const root = await realpath(rawRoot);
  try {
    await run(root);
  } finally {
    await rm(rawRoot, { recursive: true, force: true });
  }
}

async function partFiles(directory) {
  return (await readdir(directory)).filter((name) => name.includes(".memolens-") && name.endsWith(".part"));
}

test("native cancellation performs zero trust, network, or filesystem writes for video save", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const bytes = Buffer.from("verified video");
    const { coordinator, state } = harness();
    const result = await coordinator.save(proofFor(bytes));

    assert.equal(result.status, "cancelled");
    assert.equal(state.destinationChoices.length, 1);
    assert.equal(state.trustChecks, 0);
    assert.equal(state.fetches.length, 0);
    assert.deepEqual(await readdir(root), []);
  });
});

test("save-video-artifact IPC delegates to the main-owned artifact coordinator", async () => {
  const mainSource = await readFile(new URL("../electron/main.ts", import.meta.url), "utf-8");
  assert.match(
    mainSource,
    /"memolens:save-video-artifact",[\s\S]*?assertTrustedIpcSender\(event, trustedRendererEntries\);[\s\S]*?videoArtifactSaveCoordinator\.save\(request\)/,
  );
  assert.doesNotMatch(mainSource, /async function saveCompletedRenderArtifact/);
});

test("malformed and oversized artifact requests fail before native destination authority", async () => {
  const bytes = Buffer.from("12345");
  const { coordinator, state } = harness({ maximumBytes: 4 });
  for (const request of [
    proofFor(bytes),
    { ...proofFor(Buffer.from("1234")), artifactUrl: "https://example.com/steal" },
    {
      ...proofFor(Buffer.from("1234")),
      renderJobId: "render-other",
    },
    { ...proofFor(Buffer.from("1234")), destinationPath: "/tmp/forged.mp4" },
  ]) {
    const result = await coordinator.save(request);
    assert.equal(result.status, "failed");
  }
  assert.equal(state.destinationChoices.length, 0);
  assert.equal(state.trustChecks, 0);
  assert.equal(state.fetches.length, 0);
});

test("relative and symlink-parent destinations fail before trust, fetch, or artifact write", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const realDirectory = join(root, "real");
    const aliasDirectory = join(root, "alias");
    await mkdir(realDirectory);
    await symlink(realDirectory, aliasDirectory, "dir");
    const bytes = Buffer.from("verified video");

    for (const filePath of ["relative.mp4", join(aliasDirectory, "output.mp4")]) {
      const { coordinator, state } = harness({
        selection: { cancelled: false, filePath },
      });
      const result = await coordinator.save(proofFor(bytes));
      assert.equal(result.status, "failed");
      assert.equal(state.trustChecks, 0);
      assert.equal(state.fetches.length, 0);
    }
    assert.deepEqual(await readdir(realDirectory), []);
  });
});

test("existing and symbolic-link targets are never fetched, published, or overwritten", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const bytes = Buffer.from("verified video");
    const existingPath = join(root, "existing.mp4");
    const victimPath = join(root, "victim.mp4");
    const linkPath = join(root, "link.mp4");
    await writeFile(existingPath, "original-existing");
    await writeFile(victimPath, "original-victim");
    await symlink(victimPath, linkPath);

    for (const [filePath, expectedStatus] of [
      [existingPath, "exists"],
      [linkPath, "failed"],
    ]) {
      const { coordinator, state } = harness({
        selection: { cancelled: false, filePath },
      });
      const result = await coordinator.save(proofFor(bytes));
      assert.equal(result.status, expectedStatus);
      assert.equal(state.trustChecks, 0);
      assert.equal(state.fetches.length, 0);
    }
    assert.equal(await readFile(existingPath, "utf-8"), "original-existing");
    assert.equal(await readFile(victimPath, "utf-8"), "original-victim");
    assert.equal((await stat(linkPath)).isFile(), true);
    assert.deepEqual(await partFiles(root), []);
  });
});

test("destination parent replacement after native choice fails before fetch and publishes nowhere", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const parent = join(root, "destination");
    const replacedParent = join(root, "destination-replaced");
    await mkdir(parent);
    const destinationPath = join(parent, "output.mp4");
    const bytes = Buffer.from("verified video");
    const { coordinator, state } = harness({
      selection: { cancelled: false, filePath: destinationPath },
      ensureBackendTrusted: async () => {
        await rename(parent, replacedParent);
        await mkdir(parent);
      },
    });

    const result = await coordinator.save(proofFor(bytes));
    assert.equal(result.status, "failed");
    assert.equal(state.trustChecks, 1);
    assert.equal(state.fetches.length, 0);
    assert.deepEqual(await readdir(parent), []);
    assert.deepEqual(await readdir(replacedParent), []);
  });
});

test("backend trust loss after native destination choice fails before fetch or staging", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const destinationPath = join(root, "untrusted.mp4");
    const bytes = Buffer.from("verified video");
    const { coordinator, state } = harness({
      selection: { cancelled: false, filePath: destinationPath },
      ensureBackendTrusted: async () => {
        throw new Error("backend identity changed");
      },
    });

    const result = await coordinator.save(proofFor(bytes));
    assert.equal(result.status, "failed");
    assert.equal(state.trustChecks, 1);
    assert.equal(state.fetches.length, 0);
    assert.deepEqual(await readdir(root), []);
  });
});

test("a destination target created after native choice is never overwritten", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const destinationPath = join(root, "raced.mp4");
    const bytes = Buffer.from("verified video");
    const proof = proofFor(bytes);
    const { coordinator, state } = harness({
      selection: { cancelled: false, filePath: destinationPath },
      fetchImpl: async () => {
        await writeFile(destinationPath, "attacker-created");
        return artifactResponse(bytes, { sha256: proof.expectedSha256 });
      },
    });

    const result = await coordinator.save(proof);
    assert.equal(result.status, "exists");
    assert.equal(state.fetches.length, 1);
    assert.equal(await readFile(destinationPath, "utf-8"), "attacker-created");
    assert.deepEqual(await partFiles(root), []);
  });
});

test("integrity mismatch and overlong streams remove staging and publish no destination", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const expectedBytes = Buffer.from("good");
    const proof = proofFor(expectedBytes);
    const cases = [
      {
        name: "digest mismatch",
        maximumBytes: 100,
        responseBytes: Buffer.from("evil"),
      },
      {
        name: "overlong stream",
        maximumBytes: expectedBytes.length,
        responseBytes: Buffer.from("extra"),
      },
    ];
    for (const current of cases) {
      const destinationPath = join(root, `${current.name.replace(/ /g, "-")}.mp4`);
      const { coordinator } = harness({
        maximumBytes: current.maximumBytes,
        selection: { cancelled: false, filePath: destinationPath },
        fetchImpl: async () => artifactResponse(current.responseBytes, {
          sha256: proof.expectedSha256,
          contentLength: String(proof.expectedSizeBytes),
        }),
      });
      const result = await coordinator.save(proof);
      assert.equal(result.status, "failed", current.name);
      await assert.rejects(stat(destinationPath), { code: "ENOENT" });
    }
    assert.deepEqual(await partFiles(root), []);
  });
});

test("fetch failure and response URL confusion publish no destination or staging file", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const bytes = Buffer.from("verified video");
    const proof = proofFor(bytes);
    const fetchCases = [
      async () => { throw new TypeError("socket failed"); },
      async () => artifactResponse(bytes, {
        url: "http://127.0.0.1:5519/v1/renders/other/download",
        sha256: proof.expectedSha256,
      }),
    ];
    for (const [index, fetchImpl] of fetchCases.entries()) {
      const destinationPath = join(root, `failed-${index}.mp4`);
      const { coordinator } = harness({
        selection: { cancelled: false, filePath: destinationPath },
        fetchImpl,
      });
      const result = await coordinator.save(proof);
      assert.equal(result.status, "failed");
      await assert.rejects(stat(destinationPath), { code: "ENOENT" });
    }
    assert.deepEqual(await partFiles(root), []);
  });
});

test("verified bytes publish once with exclusive mode and path-free renderer result", async () => {
  await withCanonicalTemporaryDirectory(async (root) => {
    const bytes = Buffer.from("verified video");
    const proof = proofFor(bytes);
    const destinationPath = join(root, "final-output.mp4");
    const { coordinator, state } = harness({
      selection: { cancelled: false, filePath: destinationPath },
      fetchImpl: async () => artifactResponse(bytes, { sha256: proof.expectedSha256 }),
    });

    const result = await coordinator.save(proof);
    assert.deepEqual(result, {
      status: "saved",
      filename: "final-output.mp4",
      message: "Video saved without changing any source media.",
    });
    assert.deepEqual(await readFile(destinationPath), bytes);
    assert.equal((await stat(destinationPath)).mode & 0o777, 0o600);
    assert.deepEqual(await partFiles(root), []);
    assert.equal(state.fetches.length, 1);
    assert.equal(state.fetches[0].url, ARTIFACT_URL);
    assert.equal(state.fetches[0].init.redirect, "error");
    assert.equal(
      new Headers(state.fetches[0].init.headers).get("X-MemoLens-Desktop-Token"),
      "desktop-session-secret",
    );
    assert.equal(JSON.stringify(result).includes(root), false);
  });
});
