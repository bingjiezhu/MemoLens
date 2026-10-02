import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  CanonicalExportCoordinator,
  canonicalExportPresentationSha256,
} from "../electron-dist/electron/canonicalExportCoordinator.js";
import {
  canonicalExportJobPresentation,
  canonicalExportSourceBindingsSha256,
  normalizeCanonicalExportCommandResult,
  normalizeCanonicalExportPackageBasename,
  normalizeDesktopCanonicalExportApprovalResult,
  observeCanonicalExportPoll,
} from "../electron-dist/src/blueprint/exportModel.js";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);
const SHA_E = "e".repeat(64);
const PROJECT_ID = "project-1";
const DATABASE_UUID = "database-export-1";
const UUID_A = "11111111-1111-4111-8111-111111111111";
const UUID_B = "22222222-2222-4222-8222-222222222222";

function presentation() {
  return {
    object: "canonical_export.presentation",
    schema_version: "1",
    database_uuid: DATABASE_UUID,
    project_id: PROJECT_ID,
    timeline_binding: {
      revision: 3,
      revision_sha256: SHA_A,
      timeline_id: "timeline-1",
      timeline_content_sha256: SHA_B,
      source_bindings_sha256: SHA_C,
      blueprint_binding: {
        revision: 4,
        content_sha256: SHA_D,
        semantic_sha256: SHA_E,
        operation_id: "blueprint-op-4",
      },
      coverage_binding: {
        revision: 2,
        content_sha256: SHA_A,
        evidence_manifest_sha256: SHA_B,
        operation_id: "coverage-op-2",
      },
    },
    profile: "export-1080p",
    package_roles: [
      "final_video",
      "script",
      "package_manifest",
      "human_usage_list",
      "completion_marker",
    ],
    duration_ms: 12_000,
    aspect_ratio: "9:16",
    clip_count: 2,
  };
}

function presentationEnvelope(overrides = {}) {
  const value = presentation();
  return {
    object: "canonical_export.presentation_envelope",
    schema_version: "1",
    database_uuid: DATABASE_UUID,
    presentation: value,
    presentation_sha256: canonicalExportPresentationSha256(value),
    ...overrides,
  };
}

function approvalRequest(overrides = {}) {
  return {
    projectId: PROJECT_ID,
    databaseUuid: DATABASE_UUID,
    expectedTimelineBinding: {
      revision: 3,
      revisionSha256: SHA_A,
      timelineId: "timeline-1",
      timelineContentSha256: SHA_B,
      sourceBindingsSha256: SHA_C,
    },
    suggestedPackageName: "Grounded-film-r3",
    ...overrides,
  };
}

function commandResult(envelope = presentationEnvelope()) {
  return {
    object: "canonical_export.command_result",
    schema_version: "1",
    project_id: PROJECT_ID,
    operation_id: "export-operation-1",
    job: {
      id: "export-job-1",
      project_id: PROJECT_ID,
      operation_id: "export-operation-1",
      presentation_sha256: envelope.presentation_sha256,
      request_sha256: SHA_D,
      timeline_binding: {
        revision: envelope.presentation.timeline_binding.revision,
        revision_sha256: envelope.presentation.timeline_binding.revision_sha256,
        timeline_content_sha256: envelope.presentation.timeline_binding.timeline_content_sha256,
        source_bindings_sha256: envelope.presentation.timeline_binding.source_bindings_sha256,
      },
      output_binding: {
        output_root_id: "output-root-1",
        permission_fingerprint: SHA_E,
        package_basename: "Grounded-film-r3",
        profile: "export-1080p",
      },
      status: "requested",
      stage: "requested",
      progress: 0,
      attempt: 0,
      cancel_requested: false,
      error: null,
      created_at: "2026-08-23T12:00:00Z",
      started_at: null,
      heartbeat_at: null,
      finished_at: null,
      result: null,
    },
  };
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function harness({
  chooseDestination,
  responseForCall,
  ensureBackendTrusted,
} = {}) {
  const calls = [];
  const presentations = [];
  const trustChecks = [];
  const nonces = [UUID_A, UUID_B];
  const selectionReleases = [];
  let nonceIndex = 0;
  let trustGeneration = 0;
  const envelope = presentationEnvelope();
  const coordinator = new CanonicalExportCoordinator({
    apiBase: "http://127.0.0.1:5519",
    ensureBackendTrusted: async (force) => {
      trustChecks.push(force);
      trustGeneration += 1;
      if (ensureBackendTrusted) await ensureBackendTrusted(force, trustGeneration);
    },
    getMainAuthorityToken: () => `main-policy-secret-${trustGeneration}`,
    presenter: {
      chooseDestination: async (value, digest, suggested) => {
        presentations.push({ value, digest, suggested });
        if (chooseDestination) return chooseDestination(value, digest, suggested);
        return {
          cancelled: false,
          canonicalPath: "/tmp/canonical-exports",
          selectionIdentity: { device: "16777234", inode: "987654321" },
          release: async () => { selectionReleases.push("released"); },
        };
      },
    },
    createNonce: () => nonces[nonceIndex++],
    fetchImpl: async (url, init = {}) => {
      calls.push({ url: String(url), init });
      if (responseForCall) return responseForCall(calls.length, envelope, init);
      return calls.length === 1
        ? jsonResponse(envelope)
        : jsonResponse(commandResult(envelope), 201);
    },
  });
  return {
    coordinator,
    calls,
    presentations,
    trustChecks,
    envelope,
    selectionReleases,
  };
}

test("native coordinator carries one exact presentation digest into an originless main-only export", async () => {
  const state = harness();
  const result = await state.coordinator.approveAndExport(approvalRequest());

  assert.deepEqual(result, {
    status: "submitted",
    message: "The exact 1080p silent hard-cut export was approved and queued.",
    job: {
      jobId: "export-job-1",
      status: "requested",
      packageBasename: "Grounded-film-r3",
    },
  });
  assert.deepEqual(state.trustChecks, [true, true]);
  assert.equal(state.presentations.length, 1);
  assert.equal(state.presentations[0].value.timeline_binding.revision, 3);
  assert.equal(state.presentations[0].value.profile, "export-1080p");
  assert.equal(state.presentations[0].digest, state.envelope.presentation_sha256);
  assert.deepEqual(state.presentations[0].value.package_roles, [
    "final_video",
    "script",
    "package_manifest",
    "human_usage_list",
    "completion_marker",
  ]);

  assert.equal(state.calls.length, 2);
  assert.equal(
    state.calls[0].url,
    `http://127.0.0.1:5519/v1/main/creative/projects/${PROJECT_ID}/exports/presentation`,
  );
  assert.deepEqual(JSON.parse(state.calls[0].init.body), {});
  const presentationHeaders = new Headers(state.calls[0].init.headers);
  assert.equal(presentationHeaders.get("X-MemoLens-Main-Authority"), "main-policy-secret-1");
  assert.equal(presentationHeaders.has("Origin"), false);
  assert.equal(presentationHeaders.has("X-MemoLens-Desktop-Token"), false);

  const executionHeaders = new Headers(state.calls[1].init.headers);
  assert.equal(executionHeaders.get("X-MemoLens-Main-Authority"), "main-policy-secret-2");
  assert.equal(executionHeaders.get("Idempotency-Key"), `canonical-export-${UUID_B}`);
  assert.equal(executionHeaders.has("Origin"), false);
  assert.equal(executionHeaders.has("X-MemoLens-Desktop-Token"), false);
  const command = JSON.parse(state.calls[1].init.body);
  assert.deepEqual(command.presentation, state.envelope.presentation);
  assert.equal(command.presentation_sha256, state.envelope.presentation_sha256);
  assert.equal(command.native_gesture_nonce, UUID_A);
  assert.deepEqual(command.destination, {
    canonical_path: "/tmp/canonical-exports",
    package_basename: "Grounded-film-r3",
    selection_identity: { device: "16777234", inode: "987654321" },
  });
  assert.deepEqual(state.selectionReleases, ["released"]);

  const rendererProjection = JSON.stringify(result);
  for (const secret of [
    "main-policy-secret",
    "native_gesture_nonce",
    UUID_A,
    "/tmp/canonical-exports",
    "output-root-1",
    "permission_fingerprint",
  ]) {
    assert.equal(rendererProjection.includes(secret), false);
  }
});

test("native cancellation performs zero export execution and creates no nonce", async () => {
  const state = harness({
    chooseDestination: async () => ({ cancelled: true }),
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "cancelled");
  assert.equal(result.job, null);
  assert.equal(state.calls.length, 1);
  assert.deepEqual(state.trustChecks, [true]);
});

test("captured database and complete Timeline binding are checked before native presentation", async () => {
  const staleRequests = [
    approvalRequest({ databaseUuid: "database-swapped" }),
    approvalRequest({
      expectedTimelineBinding: {
        ...approvalRequest().expectedTimelineBinding,
        revision: 4,
      },
    }),
    approvalRequest({
      expectedTimelineBinding: {
        ...approvalRequest().expectedTimelineBinding,
        revisionSha256: SHA_D,
      },
    }),
    approvalRequest({
      expectedTimelineBinding: {
        ...approvalRequest().expectedTimelineBinding,
        timelineId: "timeline-same-project-different-head",
      },
    }),
    approvalRequest({
      expectedTimelineBinding: {
        ...approvalRequest().expectedTimelineBinding,
        timelineContentSha256: SHA_D,
      },
    }),
    approvalRequest({
      expectedTimelineBinding: {
        ...approvalRequest().expectedTimelineBinding,
        sourceBindingsSha256: SHA_D,
      },
    }),
  ];
  for (const request of staleRequests) {
    const state = harness();
    const result = await state.coordinator.approveAndExport(request);
    assert.equal(result.status, "failed");
    assert.equal(result.job, null);
    assert.equal(state.presentations.length, 0);
    assert.equal(state.calls.length, 1);
  }
});

test("cross-project canonical export presentation is rejected before native review or export command", async () => {
  const state = harness({
    responseForCall: (_index, envelope) => {
      const crossProject = structuredClone(envelope);
      crossProject.presentation.project_id = "project-other";
      crossProject.presentation_sha256 = canonicalExportPresentationSha256(
        crossProject.presentation,
      );
      return jsonResponse(crossProject);
    },
  });

  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "failed");
  assert.equal(result.job, null);
  assert.deepEqual(state.trustChecks, [true]);
  assert.equal(state.presentations.length, 0);
  assert.equal(state.calls.length, 1);
});

test("renderer cannot inject a native export root before native selection", async () => {
  const injectedRequests = [
    approvalRequest({
      destination: {
        canonical_path: "/private/injected-root",
        package_basename: "injected",
      },
    }),
    approvalRequest({ canonicalPath: "/private/injected-root" }),
    approvalRequest({ nativeExportRoot: "/private/injected-root" }),
  ];

  for (const request of injectedRequests) {
    const state = harness();
    const result = await state.coordinator.approveAndExport(request);
    assert.equal(result.status, "failed");
    assert.equal(result.job, null);
    assert.deepEqual(state.trustChecks, []);
    assert.equal(state.calls.length, 0);
    assert.equal(state.presentations.length, 0);
  }
});

test("desktop captured-intent request rejects missing and extra keys before any backend call", async () => {
  const missingDatabase = approvalRequest();
  delete missingDatabase.databaseUuid;
  const extraRoot = { ...approvalRequest(), presentation_sha256: SHA_A };
  const missingTimelineDigest = approvalRequest();
  delete missingTimelineDigest.expectedTimelineBinding.sourceBindingsSha256;
  const extraTimelineKey = approvalRequest();
  extraTimelineKey.expectedTimelineBinding.canonical_path = "/private/leak";

  for (const malformed of [
    missingDatabase,
    extraRoot,
    missingTimelineDigest,
    extraTimelineKey,
  ]) {
    const state = harness();
    const result = await state.coordinator.approveAndExport(malformed);
    assert.equal(result.status, "failed");
    assert.equal(result.job, null);
    assert.equal(state.calls.length, 0);
    assert.equal(state.presentations.length, 0);
  }
});

test("presentation envelope database mismatch and closed-shape violations never open native UI", async () => {
  for (const mutate of [
    (envelope) => { envelope.database_uuid = "database-swapped"; },
    (envelope) => { delete envelope.presentation.database_uuid; },
    (envelope) => { envelope.presentation.unexpected = true; },
    (envelope) => { delete envelope.presentation.timeline_binding.source_bindings_sha256; },
  ]) {
    const state = harness({
      responseForCall: (_index, envelope) => {
        const invalid = structuredClone(envelope);
        mutate(invalid);
        return jsonResponse(invalid);
      },
    });
    const result = await state.coordinator.approveAndExport(approvalRequest());
    assert.equal(result.status, "failed");
    assert.equal(result.job, null);
    assert.equal(state.presentations.length, 0);
    assert.equal(state.calls.length, 1);
  }
});

test("renderer canonical source-binding SHA-256 matches the Core known vector", async () => {
  const vector = [
    {
      media_kind: "image",
      asset_source_id: "source_1",
      asset_sha256: SHA_A,
      asset_id: "asset_1",
      evidence_ref: "memolens://evidence/asset/asset_1",
      clip_id: "clip_1",
      coverage_proof_sha256: SHA_B,
    },
    {
      source_out_ms: 9000,
      analysis_revision: 7,
      clip_id: "clip_2",
      asset_id: "asset_2",
      asset_sha256: SHA_C,
      asset_source_id: "source_2",
      coverage_proof_sha256: SHA_D,
      evidence_ref: "memolens://evidence/span/seg_2",
      media_kind: "video",
      span_id: "seg_2",
      analysis_run_id: "run_2",
      input_asset_sha256: SHA_E,
      source_in_ms: 1000,
    },
  ];
  assert.equal(
    await canonicalExportSourceBindingsSha256(vector),
    "d5b188d3115d68dc2d4e3489045e501a46cedaeab62065cb3147dcf495721959",
  );
});

test("unsafe or digest-mismatched presentation fails before native approval", async () => {
  for (const mutate of [
    (envelope) => { envelope.presentation.canonical_path = "/private/leak"; },
    (envelope) => { envelope.presentation_sha256 = SHA_A; },
    (envelope) => { envelope.presentation.package_roles.splice(1, 1); },
  ]) {
    const state = harness({
      responseForCall: (_index, envelope) => {
        const unsafe = structuredClone(envelope);
        mutate(unsafe);
        return jsonResponse(unsafe);
      },
    });
    const result = await state.coordinator.approveAndExport(approvalRequest());
    assert.equal(result.status, "failed");
    assert.equal(result.job, null);
    assert.equal(state.presentations.length, 0);
    assert.equal(state.calls.length, 1);
  }
});

test("presentation preflight failure is known failed before any export command", async () => {
  const state = harness({
    responseForCall: () => jsonResponse({ object: "error" }, 500),
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "failed");
  assert.equal(result.job, null);
  assert.equal(state.calls.length, 1);
  assert.equal(state.presentations.length, 0);
  assert.doesNotMatch(result.message, /outcome is unknown/i);
});

test("unsafe post-command job response is unknown and never projected to the renderer", async () => {
  const state = harness({
    responseForCall: (index, envelope) => {
      if (index === 1) return jsonResponse(envelope);
      const result = commandResult(envelope);
      result.job.native_nonce = UUID_A;
      result.job.output_binding.canonical_path = "/private/export";
      return jsonResponse(result, 201);
    },
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "unknown");
  assert.equal(result.job, null);
  assert.equal(JSON.stringify(result).includes("/private/export"), false);
  assert.equal(JSON.stringify(result).includes(UUID_A), false);
});

test("lost execution response is unknown rather than a confirmed zero-write failure", async () => {
  const state = harness({
    responseForCall: (index, envelope) => {
      if (index === 1) return jsonResponse(envelope);
      throw new TypeError("socket closed after server commit");
    },
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "unknown");
  assert.equal(result.job, null);
  assert.match(result.message, /outcome is unknown/i);
  assert.doesNotThrow(() => normalizeDesktopCanonicalExportApprovalResult(result));
  assert.equal(JSON.stringify(result).includes("/tmp/canonical-exports"), false);
  assert.equal(JSON.stringify(result).includes(UUID_A), false);
});

test("known HTTP command rejection remains failed rather than unknown", async () => {
  const state = harness({
    responseForCall: (index, envelope) => index === 1
      ? jsonResponse(envelope)
      : jsonResponse({ object: "error" }, 409),
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "failed");
  assert.equal(result.job, null);
});

test("server error after command dispatch is unknown because commit may have happened", async () => {
  const state = harness({
    responseForCall: (index, envelope) => index === 1
      ? jsonResponse(envelope)
      : jsonResponse({ object: "error" }, 500),
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "unknown");
  assert.equal(result.job, null);
});

test("trust loss after the native dialog stops execution before a rotated token is used", async () => {
  const state = harness({
    ensureBackendTrusted: async (_force, generation) => {
      if (generation === 2) throw new Error("backend restarted without proof");
    },
  });
  const result = await state.coordinator.approveAndExport(approvalRequest());
  assert.equal(result.status, "failed");
  assert.equal(result.job, null);
  assert.equal(state.calls.length, 1);
  assert.deepEqual(state.trustChecks, [true, true]);
  assert.deepEqual(state.selectionReleases, ["released"]);
});

test("preload exposes only the high-level export approval and no authority primitives", () => {
  const preload = readFileSync("electron/preload.cts", "utf8");
  assert.match(preload, /approveAndExportCanonicalTimeline/);
  assert.equal(preload.includes("X-MemoLens-Main-Authority"), false);
  assert.equal(preload.includes("getMainAuthorityToken"), false);
  assert.equal(preload.includes("native_gesture_nonce"), false);
  assert.equal(preload.includes("presentation_sha256"), false);
  assert.equal(preload.includes("canonical_path"), false);
  assert.equal(preload.includes("Idempotency-Key"), false);

  const declaration = readFileSync("src/electron.d.ts", "utf8");
  assert.match(declaration, /approveAndExportCanonicalTimeline/);
  assert.equal(declaration.includes("native_gesture_nonce"), false);
  assert.equal(declaration.includes("canonical_path"), false);

  const mainSource = readFileSync("electron/main.ts", "utf8");
  assert.match(
    mainSource,
    /"memolens:approve-and-export-canonical-timeline",[\s\S]*?assertTrustedIpcSender\(event, trustedRendererEntries\);[\s\S]*?canonicalExportCoordinator\.approveAndExport\(request\)/,
  );
});

test("package basename is ASCII, path-free, space-free, and bounded to 120 characters", () => {
  assert.equal(
    normalizeCanonicalExportPackageBasename("  Grounded film / revision 3  "),
    "Grounded-film-revision-3-",
  );
  assert.equal(normalizeCanonicalExportPackageBasename("回忆 短片"), "memolens-export");
  const bounded = normalizeCanonicalExportPackageBasename(`A${"b".repeat(200)}`);
  assert.equal(bounded.length, 120);
  assert.match(bounded, /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/);
});

test("renderer export status presentation keeps active and terminal claims exact", () => {
  for (const status of ["requested", "rendering", "packaging", "commit_pending", "cancelling"]) {
    const presentation = canonicalExportJobPresentation(status);
    assert.equal(presentation.phase, "active");
    assert.equal(presentation.assertive, false);
    assert.doesNotMatch(presentation.detail, /committed successfully/i);
  }

  assert.deepEqual(canonicalExportJobPresentation("succeeded"), {
    phase: "succeeded",
    label: "Export succeeded",
    detail: "The canonical package committed successfully and its usage ledger is final.",
    assertive: false,
  });
  assert.equal(canonicalExportJobPresentation("failed").phase, "failed");
  assert.equal(canonicalExportJobPresentation("failed").assertive, true);
  assert.equal(canonicalExportJobPresentation("cancelled").phase, "cancelled");
  assert.equal(canonicalExportJobPresentation("interrupted").phase, "interrupted");
  assert.equal(canonicalExportJobPresentation("interrupted").assertive, true);
});

test("renderer export poll observes only the exact submitted job and bounds unknown recovery", () => {
  const job = (job_id, status) => ({
    job_id,
    status,
    package_basename: "grounded-short-film",
  });
  const workspace = (state, latest_job) => ({
    state,
    reason_code: state === "in_progress" ? "export_in_progress" : "requires_native_confirmation",
    latest_job,
  });

  assert.equal(observeCanonicalExportPoll({
    workspace: workspace("in_progress", job("export_job_2", "rendering")),
    expectedJobId: "export_job_2",
    initialLatestJobId: "export_job_1",
    unknownGraceReached: false,
  }).state, "active");
  assert.deepEqual(observeCanonicalExportPoll({
    workspace: workspace("available", job("export_job_2", "succeeded")),
    expectedJobId: "export_job_2",
    initialLatestJobId: "export_job_1",
    unknownGraceReached: true,
  }), {
    state: "terminal",
    job: job("export_job_2", "succeeded"),
  });

  const unrelatedTerminal = observeCanonicalExportPoll({
    workspace: workspace("available", job("export_job_1", "succeeded")),
    expectedJobId: "export_job_2",
    initialLatestJobId: "export_job_1",
    unknownGraceReached: true,
  });
  assert.deepEqual(unrelatedTerminal, {
    state: "unknown",
    job: null,
    reason: "expected_job_missing",
  });

  assert.equal(observeCanonicalExportPoll({
    workspace: workspace("available", job("export_job_1", "failed")),
    expectedJobId: null,
    initialLatestJobId: "export_job_1",
    unknownGraceReached: false,
  }).state, "pending");
  assert.deepEqual(observeCanonicalExportPoll({
    workspace: workspace("available", job("export_job_1", "failed")),
    expectedJobId: null,
    initialLatestJobId: "export_job_1",
    unknownGraceReached: true,
  }), {
    state: "unknown",
    job: null,
    reason: "no_new_job",
  });
  assert.equal(observeCanonicalExportPoll({
    workspace: workspace("in_progress", job("export_job_3", "requested")),
    expectedJobId: null,
    initialLatestJobId: "export_job_3",
    unknownGraceReached: false,
  }).state, "active");
});

test("completed job proof is closed over every physical package digest", () => {
  const payload = commandResult();
  payload.job.status = "succeeded";
  payload.job.stage = "completed";
  payload.job.progress = 1;
  payload.job.started_at = "2026-08-23T12:00:01Z";
  payload.job.heartbeat_at = "2026-08-23T12:00:02Z";
  payload.job.finished_at = "2026-08-23T12:00:03Z";
  payload.job.result = {
    revision: 1,
    revision_sha256: SHA_A,
    video_sha256: SHA_B,
    video_size_bytes: 1234,
    video_duration_ms: 12_000,
    package_manifest_sha256: SHA_C,
    usage_sha256: SHA_D,
    runtime_manifest_sha256: SHA_E,
    human_usage_sha256: SHA_A,
    completion_marker_sha256: SHA_B,
    completed_at: "2026-08-23T12:00:03Z",
  };

  const normalized = normalizeCanonicalExportCommandResult(payload);
  assert.equal(normalized.job.result.human_usage_sha256, SHA_A);
  assert.equal(normalized.job.result.completion_marker_sha256, SHA_B);

  const inflated = structuredClone(payload);
  inflated.job.result.package_path = "/private/export";
  assert.throws(
    () => normalizeCanonicalExportCommandResult(inflated),
    /export\.command\.job\.result/,
  );
});
