import assert from "node:assert/strict";
import test from "node:test";

import {
  VideoApiError,
  applyTimelineInstruction,
  cancelRenderJob,
  changeMediaJob,
  createCreativeBrief,
  createTimeline,
  fetchCreativeProject,
  fetchMediaJob,
  fetchRecentMediaJobs,
  fetchRecentRenderJobs,
  fetchRenderJob,
  fetchTimeline,
  fetchVideoCapabilities,
  fetchVideoSegment,
  importVideoAssets,
  previewTimelineInstruction,
  renderDownloadUrl,
  resolveVideoResourceUrl,
  reviseTimeline,
  searchMixedAssets,
  startRender,
  validateTimeline,
} from "../src/video/api.ts";

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function withMockTransport(responses, callback) {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const requests = [];
  const timeouts = [];
  let responseIndex = 0;

  globalThis.window = globalThis;
  globalThis.setTimeout = (_callback, delay) => {
    timeouts.push(delay);
    return timeouts.length;
  };
  globalThis.clearTimeout = () => {};
  globalThis.fetch = async (url, init = {}) => {
    requests.push({ url: String(url), init });
    const response = responses[responseIndex];
    responseIndex += 1;
    if (response instanceof Error) {
      throw response;
    }
    if (!response) {
      throw new Error(`Unexpected fetch #${responseIndex}: ${String(url)}`);
    }
    return typeof response === "function" ? response(url, init) : response;
  };

  try {
    return await callback({ requests, timeouts });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
    if (originalWindow === undefined) {
      delete globalThis.window;
    } else {
      globalThis.window = originalWindow;
    }
  }
}

function requestBody(request) {
  return JSON.parse(request.init.body);
}

function minimalTimeline(revision = 1) {
  return {
    object: "creative.timeline",
    schema_version: "1",
    id: "timeline-1",
    project_id: "project-1",
    revision,
    format: { width: 1080, height: 1920, fps: 30, duration_ms: 2000 },
    tracks: [
      {
        id: "track-1",
        type: "video",
        clips: [
          {
            id: "clip-1",
            source_id: "source-legacy",
            timeline_start_ms: 0,
            timeline_duration_ms: 2000,
          },
        ],
      },
    ],
    transitions: [
      { from_clip_id: "clip-1", to_clip_id: null, type: "none", duration_ms: 0 },
    ],
  };
}

test("resource URLs stay pinned to the configured backend origin", () => {
  assert.equal(
    resolveVideoResourceUrl("http://127.0.0.1:5519/", "/v1/media/asset.mp4"),
    "http://127.0.0.1:5519/v1/media/asset.mp4",
  );
  assert.equal(
    resolveVideoResourceUrl("http://127.0.0.1:5519", "http://127.0.0.1:5519/proxy/thumb.jpg"),
    "http://127.0.0.1:5519/proxy/thumb.jpg",
  );
  assert.equal(
    resolveVideoResourceUrl("http://127.0.0.1:5519", "https://example.com/leak.mp4"),
    null,
  );
  assert.equal(resolveVideoResourceUrl("not a base", "/asset.mp4"), null);
  assert.equal(resolveVideoResourceUrl("http://127.0.0.1:5519", null), null);
});

test("capability lookup preserves the legacy 404 fallback and compatibility normalization", async () => {
  await withMockTransport(
    [
      jsonResponse({ message: "new endpoint absent" }, 404),
      jsonResponse({
        capabilities: {
          ffmpeg: { available: true, version: "6.1" },
          ffprobe: { available: true, version: "6.1" },
          encoder_probe: {
            available: true,
            code: "ok",
            message: "verified",
            profiles: ["preview-low"],
            duration_ms: 321,
          },
          transcription: { available: false, mode: "metadata" },
          vision: { available: true, mode: "remote-provider" },
          external_video_analysis: true,
          supported_inputs: [".raw"],
          supported: {
            image_extensions: [".jpg"],
            video_extensions: [".mp4", ".jpg"],
            render_profiles: ["preview-low"],
            preview_root_id: "preview-root",
            high_resolution_artifact_via_electron_save_as: true,
          },
        },
      }),
    ],
    async ({ requests, timeouts }) => {
      const capabilities = await fetchVideoCapabilities("http://localhost:5519/", new AbortController().signal);

      assert.deepEqual(requests.map((request) => request.url), [
        "http://localhost:5519/v1/media/capabilities",
        "http://localhost:5519/v1/capabilities",
      ]);
      assert.deepEqual(timeouts, [10_000, 10_000]);
      assert.deepEqual(capabilities.supported_inputs, [".raw", ".jpg", ".mp4"]);
      assert.equal(capabilities.vision.available, true);
      assert.equal(capabilities.vision.mode, "remote-provider");
      assert.equal(capabilities.preview_root_id, "preview-root");
      assert.equal(capabilities.verified_preview_save_as, true);
      assert.equal(capabilities.encoder_probe.duration_ms, 321);
    },
  );
});

test("media import, job, search, and segment adapters preserve wire contracts and aliases", async () => {
  await withMockTransport(
    [
      jsonResponse({
        result: {
          id: "import-1",
          status: "queued",
          jobs: [{
            id: "job-1",
            status: "running",
            stage: "probe_media",
            progress: 0.42,
            completed: 2,
            total: 5,
            error: { message: "one warning", retryable: true },
          }],
          imported_count: 3,
          skipped_count: 2,
          rejected: [{ message: "bad codec", relative_path: "bad.mov" }],
        },
      }),
      jsonResponse({ job: { job_id: "job-1", status: "completed", progress: 1 } }),
      jsonResponse({ jobs: [{ id: "job-2", status: "queued", progress: 25 }] }),
      jsonResponse({ job: { id: "job-2", status: "interrupted", progress: 25 } }),
      jsonResponse({
        result: {
          id: "search-1",
          data: [{
            type: "video_segment",
            segment_id: "segment-1",
            asset_id: "asset-1",
            source_id: "source-1",
            poster_url: "/thumb.jpg",
            stream_url: "/media.mp4",
            description: "Legacy description",
            score: 0.73,
            matched_terms: ["walk"],
          }],
          refinement_job: { id: "refine-1", status: "queued", progress: 0 },
        },
      }),
      jsonResponse({
        segment: {
          id: "segment-1",
          asset_id: "asset-1",
          start_ms: 1000,
          end_ms: 2500,
          stream_url: "/media.mp4",
          poster_url: "/thumb.jpg",
          keyframes: [{ id: "frame-1", timestamp_ms: 1250, url: "/frame.jpg" }],
          transcript_segments: [{ id: "line-1", start_ms: 1000, end_ms: 1500, text: "hello" }],
        },
      }),
    ],
    async ({ requests, timeouts }) => {
      const imported = await importVideoAssets({
        apiBase: "http://localhost:5519/",
        imageLibraryDir: "/photos",
        dbPath: "/db/library.sqlite",
        dryRun: true,
        idempotencyKey: "idem-import",
      });
      const fetchedJob = await fetchMediaJob("http://localhost:5519", "job/1", "/db/library.sqlite");
      const jobs = await fetchRecentMediaJobs("http://localhost:5519", "/db/library.sqlite");
      const changedJob = await changeMediaJob(
        "http://localhost:5519",
        "job-2",
        "resume",
        "/db/library.sqlite",
        undefined,
        "idem-resume",
      );
      const search = await searchMixedAssets({
        apiBase: "http://localhost:5519",
        query: "walking",
        dbPath: "/db/library.sqlite",
        topK: 12,
        orientation: "portrait",
        excludedTerms: ["blur"],
      });
      const segment = await fetchVideoSegment(
        "http://localhost:5519",
        "segment/1",
        "/db/library.sqlite",
      );

      assert.deepEqual(requestBody(requests[0]), {
        root_path: "/photos",
        db_path: "/db/library.sqlite",
        recursive: true,
        kinds: ["video"],
        dry_run: true,
      });
      assert.equal(requests[0].init.headers["Idempotency-Key"], "idem-import");
      assert.equal(imported.jobs[0].progress, 42);
      assert.equal(imported.jobs[0].errors[0].message, "one warning");
      assert.equal(imported.imported, 3);
      assert.equal(imported.skipped, 2);
      assert.equal(fetchedJob.id, "job-1");
      assert.equal(jobs[0].id, "job-2");
      assert.equal(changedJob.status, "interrupted");
      assert.equal(requests[3].init.headers["Idempotency-Key"], "idem-resume");
      assert.deepEqual(requestBody(requests[4]), {
        query: "walking",
        db_path: "/db/library.sqlite",
        types: ["image", "video_segment"],
        top_k: 12,
        filters: { orientation: "portrait", excluded_terms: ["blur"] },
        refinement: { mode: "auto", max_segments: 3, budget_frames: 300 },
      });
      assert.equal(search.results[0].result_type, "video_segment");
      assert.equal(search.results[0].id, "segment-1");
      assert.equal(search.results[0].thumbnail_url, "/thumb.jpg");
      assert.equal(search.refinement_job.id, "refine-1");
      assert.equal(segment.media_url, "/media.mp4");
      assert.equal(segment.keyframes[0].thumbnail_url, "/frame.jpg");
      assert.equal(segment.transcript[0].text, "hello");
      assert.deepEqual(timeouts, [45_000, 10_000, 10_000, 10_000, 45_000, 15_000]);
    },
  );
});

test("creative project adapters preserve candidate fallbacks and request bodies", async () => {
  const projectPayload = {
    project: {
      project_id: "project-1",
      brief_revision: 2,
      brief: {
        brief_id: "brief-1",
        goal: "Make a film",
        duration_ms: 15_000,
        candidates: [{ id: "image-1", asset_id: "image-1", description: "Still" }],
      },
      latest_timeline: { id: "timeline-1", revision: 3 },
    },
  };

  await withMockTransport(
    [jsonResponse(projectPayload), jsonResponse(projectPayload)],
    async ({ requests, timeouts }) => {
      const project = await createCreativeBrief({
        apiBase: "http://localhost:5519",
        dbPath: "/db/library.sqlite",
        brief: {
          goal: "Make a film",
          audience: "Family",
          platform: "Social video",
          duration_ms: 15_000,
          aspect_ratio: "9:16",
          tone: "warm",
          pace: "calm",
          must_include: ["people"],
          must_exclude: ["blur"],
          narrative_arc: "begin middle end",
          candidate_refs: ["old-ref"],
        },
        selectedRefs: ["selected-ref"],
        creatorProfileRef: {
          profile_id: "default",
          revision: 3,
          content_sha256: "a".repeat(64),
        },
        appliedProfileFields: ["platform", "tone"],
        idempotencyKey: "idem-brief",
      });
      const restored = await fetchCreativeProject(
        "http://localhost:5519",
        "project/1",
        "/db/library.sqlite",
      );

      assert.equal(requests[0].url, "http://localhost:5519/v1/creative/briefs");
      assert.equal(requests[0].init.headers["Idempotency-Key"], "idem-brief");
      assert.equal(requestBody(requests[0]).candidate_refs[0], "selected-ref");
      assert.equal(requestBody(requests[0]).db_path, "/db/library.sqlite");
      assert.deepEqual(requestBody(requests[0]).creator_profile_ref, {
        profile_id: "default",
        revision: 3,
        content_sha256: "a".repeat(64),
      });
      assert.deepEqual(requestBody(requests[0]).applied_profile_fields, ["platform", "tone"]);
      assert.equal(project.id, "project-1");
      assert.equal(project.brief.id, "brief-1");
      assert.equal(project.brief.revision, 2);
      assert.equal(project.candidates[0].summary, "Still");
      assert.equal(project.latest_timeline_id, "timeline-1");
      assert.equal(restored.id, "project-1");
      assert.equal(
        requests[1].url,
        "http://localhost:5519/v1/creative/projects/project%2F1?db_path=%2Fdb%2Flibrary.sqlite",
      );
      assert.deepEqual(timeouts, [60_000, 15_000]);
    },
  );
});

test("timeline adapters preserve revision bodies, preview/apply distinction, and legacy transitions", async () => {
  const timeline1 = minimalTimeline(1);
  const timeline2 = minimalTimeline(2);
  await withMockTransport(
    [
      jsonResponse({ timeline: timeline1, content_sha256: "hash-1", diff: [] }),
      jsonResponse({ timeline: timeline1, content_sha256: "hash-1" }),
      jsonResponse({ timeline: timeline2, content_sha256: "hash-2", diff: [{ op: "move_clip" }] }),
      jsonResponse({ preview: { operations: [{ op: "delete_clip", clip_id: "clip-1" }], diff: [{ op: "delete_clip" }] } }),
      jsonResponse({ timeline: timeline2, content_sha256: "hash-2", diff: [{ op: "delete_clip" }] }),
      jsonResponse({ validation: {
        id: "validation-1",
        status: "invalid",
        errors: [{ code: "bad_clip", field: "tracks.0", message: "Bad clip" }],
        warnings: [{ code: "short", message: "Very short", severity: "warning" }],
      } }),
    ],
    async ({ requests, timeouts }) => {
      const created = await createTimeline({
        apiBase: "http://localhost:5519",
        projectId: "project/1",
        briefRevision: 2,
        dbPath: "/db/library.sqlite",
        idempotencyKey: "idem-create-timeline",
      });
      const fetched = await fetchTimeline(
        "http://localhost:5519",
        "timeline/1",
        1,
        "/db/library.sqlite",
      );
      const revised = await reviseTimeline({
        apiBase: "http://localhost:5519",
        timelineId: "timeline/1",
        baseRevision: 1,
        dbPath: "/db/library.sqlite",
        operations: [{ op: "move_clip", clip_id: "clip-1", to_index: 0 }],
        idempotencyKey: "idem-revise",
      });
      const preview = await previewTimelineInstruction({
        apiBase: "http://localhost:5519",
        timelineId: "timeline/1",
        baseRevision: 1,
        dbPath: "/db/library.sqlite",
        instruction: " delete first ",
      });
      const applied = await applyTimelineInstruction({
        apiBase: "http://localhost:5519",
        timelineId: "timeline/1",
        baseRevision: 1,
        dbPath: "/db/library.sqlite",
        instruction: " delete first ",
        idempotencyKey: "idem-instruction",
      });
      const validation = await validateTimeline({
        apiBase: "http://localhost:5519",
        timelineId: "timeline/1",
        revision: 2,
        dbPath: "/db/library.sqlite",
      });

      assert.deepEqual(requestBody(requests[0]), {
        db_path: "/db/library.sqlite",
        brief_revision: 2,
      });
      assert.equal(requests[0].init.headers["Idempotency-Key"], "idem-create-timeline");
      assert.match(requests[1].url, /revision=1&db_path=%2Fdb%2Flibrary.sqlite$/);
      assert.deepEqual(requestBody(requests[2]), {
        db_path: "/db/library.sqlite",
        base_revision: 1,
        operations: [{ op: "move_clip", clip_id: "clip-1", to_index: 0 }],
      });
      assert.deepEqual(requestBody(requests[3]), {
        db_path: "/db/library.sqlite",
        base_revision: 1,
        instruction: "delete first",
        apply: false,
      });
      assert.deepEqual(requestBody(requests[4]), {
        db_path: "/db/library.sqlite",
        base_revision: 1,
        instruction: "delete first",
        apply: true,
      });
      assert.deepEqual(requestBody(requests[5]), {
        revision: 2,
        db_path: "/db/library.sqlite",
      });
      assert.equal(created.timeline.content_sha256, "hash-1");
      assert.equal(created.timeline.tracks[0].clips[0].kind, "video");
      assert.equal(created.timeline.tracks[0].clips[0].asset_source_id, "source-legacy");
      assert.equal(created.timeline.tracks[0].clips[0].transition_out.type, "none");
      assert.equal(fetched.content_sha256, "hash-1");
      assert.equal(revised.timeline.revision, 2);
      assert.equal(preview.operations[0].op, "delete_clip");
      assert.equal(applied.timeline.revision, 2);
      assert.equal(validation.valid, false);
      assert.equal(validation.errors[0].field, "tracks.0");
      assert.equal(validation.warnings[0].severity, "warning");
      assert.deepEqual(timeouts, [60_000, 15_000, 60_000, 45_000, 60_000, 20_000]);
    },
  );
});

test("render adapters preserve immutable preview contract, aliases, and job endpoints", async () => {
  const renderPayload = {
    job: {
      job_id: "render-1",
      profile: "preview-low",
      status: "completed",
      progress: 1,
      timeline_id: "timeline-1",
      timeline_revision: 2,
      artifact_url: "/artifacts/preview.mp4",
      output: {
        filename: "preview.mp4",
        output_sha256: "abc123",
        size_bytes: 512,
      },
    },
  };
  await withMockTransport(
    [
      jsonResponse(renderPayload),
      jsonResponse(renderPayload),
      jsonResponse({ jobs: [renderPayload.job] }),
      jsonResponse({ job: { ...renderPayload.job, status: "cancelling", progress: 55 } }),
    ],
    async ({ requests, timeouts }) => {
      const started = await startRender({
        apiBase: "http://localhost:5519/",
        timelineId: "timeline-1",
        revision: 2,
        timelineSha256: "timeline-hash",
        previewRootId: "preview-root",
        kind: "preview",
        dbPath: "/db/library.sqlite",
        idempotencyKey: "idem-render",
      });
      const fetched = await fetchRenderJob(
        "http://localhost:5519",
        "render/1",
        "/db/library.sqlite",
      );
      const recent = await fetchRecentRenderJobs("http://localhost:5519", "/db/library.sqlite");
      const cancelled = await cancelRenderJob(
        "http://localhost:5519",
        "render/1",
        "/db/library.sqlite",
        undefined,
        "idem-cancel",
      );

      assert.deepEqual(requestBody(requests[0]), {
        timeline_id: "timeline-1",
        timeline_revision: 2,
        expected_timeline_sha256: "timeline-hash",
        output: { root_id: "preview-root" },
        profile: "preview-low",
        db_path: "/db/library.sqlite",
      });
      assert.equal(requests[0].init.headers["Idempotency-Key"], "idem-render");
      assert.equal(started.id, "render-1");
      assert.equal(started.kind, "preview");
      assert.equal(started.progress, 100);
      assert.equal(started.media_url, "/artifacts/preview.mp4");
      assert.equal(fetched.output.output_sha256, "abc123");
      assert.equal(recent[0].filename, "preview.mp4");
      assert.equal(cancelled.status, "cancelling");
      assert.equal(requests[3].init.headers["Idempotency-Key"], "idem-cancel");
      assert.equal(
        renderDownloadUrl("http://localhost:5519", { ...started, download_url: null, media_url: null }),
        "http://localhost:5519/v1/renders/render-1/download",
      );
      assert.deepEqual(timeouts, [20_000, 10_000, 10_000, 10_000]);
    },
  );

  await assert.rejects(
    () => startRender({
      apiBase: "http://localhost:5519",
      timelineId: "timeline-1",
      revision: 2,
      timelineSha256: null,
      previewRootId: "preview-root",
      kind: "preview",
      idempotencyKey: "invalid-render",
    }),
    (error) => error instanceof VideoApiError
      && error.status === 400
      && error.code === "render_contract_incomplete",
  );
});

test("transport preserves structured API errors and timeout classification", async () => {
  await withMockTransport(
    [jsonResponse({ error: { message: "Bad revision", code: "revision_conflict", retryable: true, field: "base_revision" } }, 409)],
    async () => {
      await assert.rejects(
        () => fetchTimeline("http://localhost:5519", "timeline-1"),
        (error) => error instanceof VideoApiError
          && error.status === 409
          && error.message === "Bad revision"
          && error.code === "revision_conflict"
          && error.retryable === true
          && error.field === "base_revision",
      );
    },
  );
});
