import assert from "node:assert/strict";
import test from "node:test";

import { deriveVideoWorkflow } from "../src/video/workflow.ts";

test("starts at idea and locks every later step", () => {
  const workflow = deriveVideoWorkflow({
    idea: false,
    materials: false,
    brief: false,
    timeline: false,
    preview: false,
    save: false,
  });

  assert.equal(workflow.currentId, "idea");
  assert.deepEqual(
    workflow.steps.map(({ id, status, canOpen }) => ({ id, status, canOpen })),
    [
      { id: "idea", status: "current", canOpen: true },
      { id: "materials", status: "locked", canOpen: false },
      { id: "brief", status: "locked", canOpen: false },
      { id: "timeline", status: "locked", canOpen: false },
      { id: "preview", status: "locked", canOpen: false },
      { id: "save", status: "locked", canOpen: false },
    ],
  );
});

test("keeps completed steps reviewable and exposes one current step", () => {
  const workflow = deriveVideoWorkflow({
    idea: true,
    materials: true,
    brief: true,
    timeline: false,
    preview: false,
    save: false,
  });

  assert.equal(workflow.currentId, "timeline");
  assert.deepEqual(
    workflow.steps.map(({ status }) => status),
    ["complete", "complete", "complete", "current", "locked", "locked"],
  );
  assert.equal(workflow.steps[0].canOpen, true);
  assert.equal(workflow.steps[4].canOpen, false);
});

test("finished workflows keep Save as the review destination", () => {
  const workflow = deriveVideoWorkflow({
    idea: true,
    materials: true,
    brief: true,
    timeline: true,
    preview: true,
    save: true,
  });

  assert.equal(workflow.currentId, "save");
  assert.ok(workflow.steps.every((step) => step.status === "complete"));
  assert.ok(workflow.steps.every((step) => step.canOpen));
});
