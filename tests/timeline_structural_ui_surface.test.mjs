import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceSource = await readFile(
  new URL("../src/blueprint/BlueprintProjectWorkspace.tsx", import.meta.url),
  "utf8",
);

test("canonical Timeline exposes real Split and Delete controls through structural N+1 API", () => {
  assert.match(workspaceSource, /applyCanonicalTimelineStructuralEdit/);
  assert.match(workspaceSource, />\s*Split clip\s*</);
  assert.match(workspaceSource, />\s*Delete clip\s*</);
  assert.match(workspaceSource, /op: "split_clip"/);
  assert.match(workspaceSource, /op: "delete_clip"/);
  assert.match(workspaceSource, /refreshTimelineForWorkspace\(next, request\)/);
  assert.match(workspaceSource, /append-only history/);
});

test("structural controls remain bound to exact head eligibility and never stage a fake local cut", () => {
  assert.match(workspaceSource, /!canEditTimeline/);
  assert.match(workspaceSource, /expectedTimelineHead = capturedTimeline\.head/);
  assert.match(workspaceSource, /pendingTimelineEdit !== null/);
  assert.match(workspaceSource, /pendingTimelineRestore !== null/);
  assert.doesNotMatch(workspaceSource, /stageCanonicalTimelineStructuralEdit/);
});
