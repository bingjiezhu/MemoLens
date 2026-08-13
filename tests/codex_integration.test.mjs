import assert from "node:assert/strict";
import test from "node:test";

import { buildMemoLensCodexUrl } from "../electron-dist/electron/codexIntegration.js";


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
