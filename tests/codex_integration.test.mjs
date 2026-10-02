import assert from "node:assert/strict";
import test from "node:test";

import { readFile } from "node:fs/promises";

import {
  buildMemoLensCodexUrl,
  dispatchMemoLensLocalPluginUrl,
  isMemoLensLocalPluginUrl,
} from "../electron-dist/electron/codexIntegration.js";


test("MemoLens Codex deep link is fixed to the local plugin and marketplace", () => {
  const value = buildMemoLensCodexUrl("/tmp/Memo Lens/项目");
  const url = new URL(value);

  assert.equal(url.protocol, "codex:");
  assert.equal(url.hostname, "plugins");
  assert.equal(url.pathname, "/memolens");
  assert.equal(
    url.searchParams.get("marketplacePath"),
    "/tmp/Memo Lens/项目/.agents/plugins/marketplace.json",
  );
  assert.deepEqual([...url.searchParams.keys()], ["marketplacePath"]);
});

test("open in Codex rejects arbitrary URL scope before external dispatch", async () => {
  const projectRoot = "/tmp/MemoLens-project";
  const fixed = buildMemoLensCodexUrl(projectRoot);
  assert.equal(isMemoLensLocalPluginUrl(fixed, projectRoot), true);
  const dispatched = [];
  const dispatch = async (targetUrl) => {
    dispatched.push(targetUrl);
  };
  for (const forged of [
    "https://example.com/plugin",
    "codex://plugins/other?marketplacePath=%2Ftmp%2FMemoLens-project%2F.agents%2Fplugins%2Fmarketplace.json",
    "codex://plugins/memolens?marketplacePath=%2Ftmp%2Fother%2Fmarketplace.json",
    `${fixed}&url=https%3A%2F%2Fexample.com`,
    `${fixed}#other`,
  ]) {
    assert.equal(isMemoLensLocalPluginUrl(forged, projectRoot), false, forged);
    await assert.rejects(
      dispatchMemoLensLocalPluginUrl(forged, projectRoot, dispatch),
      /invalid local plugin installation target/,
    );
  }
  assert.deepEqual(dispatched, []);
  await dispatchMemoLensLocalPluginUrl(fixed, projectRoot, dispatch);
  assert.deepEqual(dispatched, [fixed]);

  const [mainSource, preloadSource] = await Promise.all([
    readFile(new URL("../electron/main.ts", import.meta.url), "utf-8"),
    readFile(new URL("../electron/preload.cts", import.meta.url), "utf-8"),
  ]);
  assert.match(
    mainSource,
    /"memolens:open-in-codex", async \(event\): Promise<boolean>/,
  );
  assert.match(
    mainSource,
    /dispatchMemoLensLocalPluginUrl\(targetUrl, PROJECT_ROOT/,
  );
  assert.match(
    preloadSource,
    /openInCodex\(\): Promise<boolean> \{\s*return invokeProductionIpc<boolean>\("memolens:open-in-codex"\);/,
  );
});
