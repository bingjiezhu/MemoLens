import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  defaultPreviewFilename,
  formatJobStage,
  formatMediaScore,
  isActiveJobStatus,
  isCancellableJobStatus,
  isSuccessfulRenderStatus,
  isUsableJobStatus,
  summarizeMediaJobs,
} from "../src/video/jobModel.ts";


function job(id, status, progress) {
  return { id, status, progress };
}

test("job status predicates keep lifecycle semantics explicit", () => {
  assert.equal(isActiveJobStatus("queued"), true);
  assert.equal(isActiveJobStatus("cancelling"), true);
  assert.equal(isActiveJobStatus("interrupted"), false);
  assert.equal(isCancellableJobStatus("running"), true);
  assert.equal(isCancellableJobStatus("cancelling"), false);
  assert.equal(isUsableJobStatus("partial"), true);
  assert.equal(isSuccessfulRenderStatus("partial"), false);
  assert.equal(isSuccessfulRenderStatus("completed"), true);
});

test("media job summary preserves rounded aggregate progress and status counts", () => {
  assert.deepEqual(summarizeMediaJobs([]), {
    progress: 0,
    active: 0,
    failed: 0,
    completed: 0,
  });
  assert.deepEqual(summarizeMediaJobs([
    job("a", "running", 25),
    job("b", "failed", 50),
    job("c", "partial", 100),
  ]), {
    progress: 58,
    active: 1,
    failed: 1,
    completed: 1,
  });
});

test("presentation helpers are deterministic and bounded", () => {
  assert.equal(formatJobStage("encoder_unavailable"), "Encoder unavailable");
  assert.equal(formatMediaScore(-1), "0%");
  assert.equal(formatMediaScore(0.505), "51%");
  assert.equal(formatMediaScore(2), "100%");
  assert.equal(defaultPreviewFilename({ project_id: "project", revision: 7 }), "memolens-project-r7.mp4");
});

test("hidden or background Video Workbench gates every periodic poller", async () => {
  const source = await readFile(new URL("../src/VideoWorkbench.tsx", import.meta.url), "utf8");

  assert.match(source, /root\.closest\("\[hidden\]"\) === null/);
  assert.match(source, /document\.addEventListener\("visibilitychange", refresh\)/);
  assert.match(source, /attributeFilter: \["hidden"\]/);
  assert.equal(
    source.match(/if \(!pollingAllowed \|\|/g)?.length,
    3,
    "Blueprint, media-index, and render polling effects must all share the visibility gate",
  );
  assert.match(
    source,
    /data-video-workbench-polling-root="canonical"\s*>\s*<div className="section-block video-workbench">\{projectOpenPanel\}<\/div>\s*\{pollingAllowed \? \(/,
    "the nested canonical workspace must not retain its own pollers while hidden",
  );
});
